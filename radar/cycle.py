"""Recurring collection-cycle contract with durable checkpoint and conservative reconciliation."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import asdict
from datetime import datetime, timezone

from meta_ads_collector import Ad

from .models import CollectionCycle, EvidenceGroup, RadarConfig, RadarReport
from .operations import checkpoint_ads, collect_keyword, collection_lock, diagnostic, failure_kind, search_with_retry
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


def start_cycle(entries: list[KeywordEntry], store: RadarStore, config: RadarConfig | None = None, *,
                managed: bool = False) -> CollectionCycle:
    if not entries or len({entry.id for entry in entries}) != len(entries):
        raise ValueError("cycle plan requires nonempty, distinct keyword IDs")
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
    store.create_cycle(cycle, [(entry.id, entry.text, entry.source) for entry in entries], managed=managed)
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
    full = (
        row["status"] == "COMPLETED" and bool(row["official_plan"])
        and succeeded == expected and failed == 0 and pending == 0
    )
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
    with collection_lock(store.path):
        return _run_cycle(store, cycle_id, entries, config or RadarConfig(), search, max_keywords, sleep)


def _run_cycle(store, cycle_id, entries, config, search, max_keywords, sleep):
    row = store.cycle_row(cycle_id)
    registry_hash, plan_hash = _registry_identity(entries)
    if registry_hash != row["registry_hash"] or plan_hash != row["plan_hash"]:
        raise ValueError("resume blocked: registry/plan is incompatible with the cycle checkpoint")
    if asdict(config) != json.loads(row["config_json"]):
        raise ValueError("resume blocked: configuration is incompatible with the cycle checkpoint")
    if row["status"] == "COMPLETED":
        raise ValueError("cycle is already completed; start the next cycle instead of re-executing it")

    store.ensure_media_identity()
    by_id = {entry.id: entry for entry in entries}
    checkpoint = store.cycle_keywords(cycle_id)
    if [(r["keyword_id"], r["keyword_text"], r["source"]) for r in checkpoint] != [
        (entry.id, entry.text, entry.source) for entry in entries
    ] or any(r["status"] not in {"PENDING", "ERROR", "OK"} for r in checkpoint):
        raise ValueError("cycle checkpoint keywords differ from the saved plan")
    for item in checkpoint:
        if item["status"] == "OK":
            checkpoint_ads(item)
    work = [item for item in checkpoint if item["status"] != "OK"]
    if max_keywords is not None:
        work = work[:max_keywords]
    run_id, recovered = store.begin_cycle_run(cycle_id, asdict(config))
    diagnostic("cycle_run_started", cycle_id=cycle_id, run_id=run_id, recovered_runs=recovered, remaining=len(work))

    consecutive_transport_errors = 0
    pause_reason = None
    for index, item in enumerate(work):
        entry = by_id[item["keyword_id"]]
        try:
            ads = search_with_retry(search or collect_keyword, entry, config, sleep, run_id=run_id)
        except Exception as exc:
            kind = failure_kind(exc)
            store.checkpoint_search(run_id, entry, [], cycle_id=cycle_id, error=kind)
            consecutive_transport_errors = (
                consecutive_transport_errors + 1 if kind in {"transient_transport", "incomplete_search"} else 0
            )
            if kind in {"rate_limit", "access_blocked"} or consecutive_transport_errors >= 3:
                pause_reason = kind
                diagnostic("cycle_paused", cycle_id=cycle_id, run_id=run_id, reason=kind)
                break
            if config.backoff_seconds:
                sleep(config.backoff_seconds)
        else:
            consecutive_transport_errors = 0
            store.checkpoint_search(run_id, entry, ads, cycle_id=cycle_id)
            diagnostic("keyword_checkpointed", run_id=run_id, keyword_id=entry.id, observations=len(ads))
        if (index + 1) % config.batch_size == 0 and index + 1 < len(work) and config.interval_seconds:
            sleep(config.interval_seconds)

    records: list[tuple[Ad, set[str]]] = []
    rows = store.cycle_keywords(cycle_id)
    for item in rows:
        if item["status"] == "OK":
            records.extend((ad, {item["keyword_text"]}) for ad in checkpoint_ads(item))
    report = analyze_ads(records, config)
    cycle = get_cycle(store, cycle_id)
    full = cycle.official_plan and cycle.succeeded == cycle.total_keywords_expected
    if not full:
        _merge_partial_with_known(store, report, cycle_id)
    else:
        for group in report.groups:
            group.coverage_context = {"kind": "FULL_COVERAGE", "cycle_id": cycle_id}
    suppressed = len(store.known_group_ids() - {group.group_id for group in report.groups}) if not full else 0
    report.collection.update({
        "cycle": cycle.to_dict(),
        "coverage": "FULL_COVERAGE" if full else "PARTIAL_COVERAGE",
        "suppressed_absence_events": suppressed,
        "depth_limit_notice": (
            f"FULL cobre o plano oficial de {FERNANDO_EXPECTED_COUNT} keywords; limit={config.max_results_per_keyword} "
            "nao representa cobertura total da Meta Ads Library."
        ),
    })
    unfinished = any(item["status"] in {"PENDING", "ERROR"} for item in rows)
    if full:
        status, reason = "COMPLETED", None
    elif unfinished:
        status, reason = "PARTIAL", pause_reason or "keywords pendentes, com erro ou plano menor que o oficial"
    else:
        status, reason = "PARTIAL", "subset/gate nao equivale ao plano oficial de 106 keywords"
    store.finalize_cycle(cycle_id, run_id, report, full=full, unfinished=unfinished, status=status, reason=reason)
    diagnostic("cycle_run_finalized", cycle_id=cycle_id, run_id=run_id, status=status,
               succeeded=cycle.succeeded, failed=cycle.failed, pending=cycle.pending)
    final_cycle = get_cycle(store, cycle_id)
    report.collection["cycle"] = final_cycle.to_dict()
    return final_cycle, report


def saved_cycle_report(store: RadarStore, cycle: CollectionCycle) -> tuple[RadarReport, int]:
    """Re-export a finalized snapshot after a console or filesystem failure."""
    if cycle.status not in {"COMPLETED", "PARTIAL"} or not cycle.run_ids:
        raise ValueError("cycle has no finalized snapshot; resume it first")
    run_id = cycle.run_ids[-1]
    groups = [EvidenceGroup(**row) for row in store.run_group_snapshots(run_id)]
    rows = [row for row in store.cycle_keywords(cycle.cycle_id) if row["status"] == "OK"]
    ads = [ad for row in rows for ad in checkpoint_ads(row)]
    return RadarReport.create(RadarConfig(**cycle.config), {
        "cycle": cycle.to_dict(), "coverage": cycle.coverage_kind, "observations": len(ads),
        "unique_ad_ids": len({str(ad.id) for ad in ads}), "groups": len(groups),
    }, groups), run_id
