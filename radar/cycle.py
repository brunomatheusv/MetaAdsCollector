"""Recurring collection-cycle contract with durable checkpoint and conservative reconciliation."""

from __future__ import annotations

import hashlib
import json
import pickle
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import asdict
from datetime import datetime, timezone

from meta_ads_collector import Ad, MetaAdsCollector

from .models import CollectionCycle, RadarConfig, RadarReport
from .pipeline import analyze_ads
from .registry import FERNANDO_EXPECTED_COUNT, FERNANDO_SOURCE, KeywordEntry
from .storage import RadarStore

REGISTRY_VERSION = "fernando-106-v1"


def _digest(value: object) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _registry_identity(entries: list[KeywordEntry]) -> tuple[str, str]:
    registry = [(entry.id, entry.text, entry.source, entry.category) for entry in entries]
    plan = [(entry.id, entry.text) for entry in entries]
    return _digest(registry), _digest(plan)


def start_cycle(entries: list[KeywordEntry], store: RadarStore, config: RadarConfig | None = None) -> CollectionCycle:
    if not entries:
        raise ValueError("cycle plan requires at least one keyword")
    config = config or RadarConfig()
    registry_hash, plan_hash = _registry_identity(entries)
    official = len(entries) == FERNANDO_EXPECTED_COUNT and all(entry.source == FERNANDO_SOURCE for entry in entries)
    expected = FERNANDO_EXPECTED_COUNT if all(entry.source == FERNANDO_SOURCE for entry in entries) else len(entries)
    cycle = CollectionCycle(
        cycle_id=uuid.uuid4().hex,
        started_at=datetime.now(timezone.utc).isoformat(),
        finished_at=None,
        status="PLANNED",
        registry_source=entries[0].source,
        registry_version=REGISTRY_VERSION,
        registry_hash=registry_hash,
        plan_hash=plan_hash,
        config=asdict(config),
        total_keywords_expected=expected,
        attempted=0,
        succeeded=0,
        failed=0,
        empty=0,
        pending=expected,
        coverage_ratio=0.0,
        coverage_kind="PARTIAL_COVERAGE",
        official_plan=official,
        run_ids=[],
        duration_seconds=None,
    )
    store.create_cycle(cycle, [(entry.id, entry.text, entry.source) for entry in entries])
    return cycle


def get_cycle(store: RadarStore, cycle_id: str) -> CollectionCycle:
    row = store.cycle_row(cycle_id)
    keywords = store.cycle_keywords(cycle_id)
    succeeded = sum(item["status"] == "OK" for item in keywords)
    failed = sum(item["status"] == "ERROR" for item in keywords)
    empty = sum(item["status"] == "OK" and item["raw_count"] == 0 for item in keywords)
    attempted = sum(item["attempts"] > 0 for item in keywords)
    expected = int(row["total_keywords_expected"])
    pending = expected - attempted
    full = bool(row["official_plan"]) and succeeded == expected and failed == 0 and pending == 0
    started_at, finished_at = row["started_at"], row["finished_at"]
    duration = None
    if finished_at:
        duration = (datetime.fromisoformat(finished_at) - datetime.fromisoformat(started_at)).total_seconds()
    return CollectionCycle(
        cycle_id=row["cycle_id"], started_at=started_at, finished_at=finished_at,
        status=row["status"], registry_source=row["registry_source"], registry_version=row["registry_version"],
        registry_hash=row["registry_hash"], plan_hash=row["plan_hash"], config=json.loads(row["config_json"]),
        total_keywords_expected=expected, attempted=attempted, succeeded=succeeded, failed=failed, empty=empty,
        pending=max(pending, 0), coverage_ratio=succeeded / expected,
        coverage_kind="FULL_COVERAGE" if full else "PARTIAL_COVERAGE", official_plan=bool(row["official_plan"]),
        run_ids=store.cycle_run_ids(cycle_id), reason=row["reason"],
        duration_seconds=duration,
    )


def _merge_partial_with_known(store: RadarStore, report: RadarReport, cycle_id: str) -> None:
    current_owners = {ad_id: group.group_id for group in report.groups for ad_id in group.ad_ids}
    for group in report.groups:
        previous = store.latest_group_evidence(group.group_id)
        group.coverage_context = {"kind": "PARTIAL_COVERAGE", "cycle_id": cycle_id}
        if not previous or group.evidence_level != "STRONG_SAME_OFFER":
            continue
        old_ids = set(previous.get("ad_ids", []))
        if any(current_owners.get(ad_id, group.group_id) != group.group_id for ad_id in old_ids):
            group.conflicting_evidence.append("ad_id_historico_observado_em_outra_identidade")
            group.noise_flags = sorted(set(group.noise_flags) | {"sinais_de_identidade_conflitantes"})
            group.confidence_reasons.extend(group.conflicting_evidence)
            group.grouping_confidence = "BAIXA"
            group.evidence_level = "AMBIGUOUS"
            if group.final_status != "EVIDENCIA_INSUFICIENTE":
                group.final_status = "INVESTIGAR"
            group.reasons.append("identidade atual diverge da atribuicao historica")
            continue
        current_ids = set(group.ad_ids)
        group.coverage_context["current_related_ads_count"] = group.related_ads_count
        group.coverage_context["historical_only_ad_ids"] = sorted(old_ids - current_ids)
        group.ad_ids = sorted(old_ids | current_ids)
        group.related_ads_count = len(group.ad_ids)
        group.keywords = sorted(set(previous.get("keywords", [])) | set(group.keywords))
        group.destination_urls = sorted(set(previous.get("destination_urls", [])) | set(group.destination_urls))
        group.destination_domains = sorted(
            set(previous.get("destination_domains", [])) | set(group.destination_domains)
        )
        if (
            previous.get("final_status") == "CANDIDATO_RADAR"
            and group.final_status == "REJEITADO"
            and group.criterion_max_age == "PASSA"
            and group.criterion_5_plus == "FALHA"
        ):
            group.final_status = "CANDIDATO_RADAR"
            group.criterion_5_plus = "PASSA"
            group.reasons.append("status anterior preservado porque a cobertura deste ciclo e parcial")


def run_cycle(
    store: RadarStore,
    cycle_id: str,
    entries: list[KeywordEntry],
    config: RadarConfig | None = None,
    *,
    search: Callable[[KeywordEntry, RadarConfig], Iterable[Ad]] | None = None,
    max_keywords: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[CollectionCycle, RadarReport]:
    """Run pending/failed keywords only. ``max_keywords`` provides a controlled interruption point."""
    config = config or RadarConfig()
    row = store.cycle_row(cycle_id)
    registry_hash, plan_hash = _registry_identity(entries)
    if registry_hash != row["registry_hash"] or plan_hash != row["plan_hash"]:
        raise ValueError("resume blocked: registry/plan is incompatible with the cycle checkpoint")
    if asdict(config) != json.loads(row["config_json"]):
        raise ValueError("resume blocked: configuration is incompatible with the cycle checkpoint")
    if row["status"] == "COMPLETED":
        raise ValueError("cycle is already completed; start the next cycle instead of re-executing it")

    by_id = {entry.id: entry for entry in entries}
    work = [item for item in store.cycle_keywords(cycle_id) if item["status"] in {"PENDING", "ERROR"}]
    if max_keywords is not None:
        work = work[:max_keywords]
    run_id = store.start_run({**asdict(config), "cycle_id": cycle_id})
    store.attach_cycle_run(cycle_id, run_id)
    store.update_cycle(cycle_id, "RUNNING", finished=False)

    def default_search(entry: KeywordEntry, cfg: RadarConfig) -> Iterable[Ad]:
        with MetaAdsCollector() as collector:
            return list(collector.search(
                query=entry.text, country=cfg.country, status=cfg.status.upper(),
                max_results=cfg.max_results_per_keyword,
            ))

    search_fn = search or default_search
    for index, item in enumerate(work):
        entry = by_id[item["keyword_id"]]
        try:
            ads = list(search_fn(entry, config))
            store.record_cycle_keyword(cycle_id, entry.id, "OK", len(ads), payload=pickle.dumps(ads))
            store.record_keyword(run_id, entry.id, entry.text, entry.source, "OK", len(ads))
            for ad in ads:
                store.record_ad_origin(run_id, str(ad.id), entry.id)
        except Exception as exc:
            store.record_cycle_keyword(cycle_id, entry.id, "ERROR", error=str(exc))
            store.record_keyword(run_id, entry.id, entry.text, entry.source, "ERROR", error=str(exc))
            if config.backoff_seconds:
                sleep(config.backoff_seconds)
        if (index + 1) % config.batch_size == 0 and index + 1 < len(work) and config.interval_seconds:
            sleep(config.interval_seconds)

    records: list[tuple[Ad, set[str]]] = []
    rows = store.cycle_keywords(cycle_id)
    for item in rows:
        if item["status"] != "OK" or item["payload"] is None:
            continue
        for ad in pickle.loads(item["payload"]):  # noqa: S301 - private, trusted local checkpoint DB
            records.append((ad, {item["keyword_text"]}))
    report = analyze_ads(records, config)
    cycle = get_cycle(store, cycle_id)
    full = cycle.coverage_kind == "FULL_COVERAGE"
    if not full:
        _merge_partial_with_known(store, report, cycle_id)
    else:
        for group in report.groups:
            group.coverage_context = {"kind": "FULL_COVERAGE", "cycle_id": cycle_id}
    suppressed = len(store.known_group_ids() - {group.group_id for group in report.groups}) if not full else 0
    report.collection.update({
        "cycle": cycle.to_dict(),
        "coverage": cycle.coverage_kind,
        "suppressed_absence_events": suppressed,
        "depth_limit_notice": (
            f"FULL cobre o plano oficial de {FERNANDO_EXPECTED_COUNT} keywords; limit={config.max_results_per_keyword} "
            "nao representa cobertura total da Meta Ads Library."
        ),
    })
    store.persist_report(run_id, report)
    store.reconcile_cycle(cycle_id, run_id, {group.group_id for group in report.groups}, full=full)
    unfinished = any(item["status"] in {"PENDING", "ERROR"} for item in rows)
    if full:
        status, reason = "COMPLETED", None
    elif unfinished:
        status, reason = "PARTIAL", "keywords pendentes, com erro ou plano menor que o oficial"
    else:
        status, reason = "PARTIAL", "subset/gate nao equivale ao plano oficial de 106 keywords"
    store.finish_run(run_id, "COMPLETED" if not unfinished else "COMPLETED_WITH_ERRORS")
    store.update_cycle(cycle_id, status, finished=not unfinished, reason=reason)
    final_cycle = get_cycle(store, cycle_id)
    report.collection["cycle"] = final_cycle.to_dict()
    return final_cycle, report
