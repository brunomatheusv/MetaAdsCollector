"""Controlled, resumable keyword collection."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass

from meta_ads_collector import Ad, MetaAdsCollector
from meta_ads_collector.exceptions import AuthenticationError, RateLimitError, SessionExpiredError

from .models import EvidenceGroup, RadarConfig, RadarReport
from .operations import checkpoint_ads, collect_keyword, collection_lock, diagnostic, failure_kind, search_with_retry
from .pipeline import analyze_ads
from .registry import KeywordEntry
from .storage import RadarStore


@dataclass
class BatchMetrics:
    duration_seconds: float = 0.0
    requests: int = 0
    raw_ads: int = 0
    unique_ads: int = 0
    errors: int = 0
    empty_results: int = 0
    groups: int = 0
    candidato_radar: int = 0
    investigar: int = 0
    rejeitado: int = 0
    evidencia_insuficiente: int = 0
    possible_rate_limits: int = 0


def run_batch(entries: list[KeywordEntry], store: RadarStore, config: RadarConfig | None = None, *,
              run_id: int | None = None,
              search: Callable[[KeywordEntry, RadarConfig], Iterable[Ad]] | None = None,
              sleep: Callable[[float], None] = time.sleep) -> tuple[int, RadarReport, BatchMetrics]:
    with collection_lock(store.path):
        return _run_batch(entries, store, config or RadarConfig(), run_id, search, sleep)


def _run_batch(entries, store, config, run_id, search, sleep):
    started = time.monotonic()
    plan = {entry.id: (entry.text, entry.source) for entry in entries}
    if len(plan) != len(entries):
        raise ValueError("batch requires distinct keyword IDs")
    stored_config = {**asdict(config), "keyword_plan": {key: list(value) for key, value in plan.items()}}
    run_id = run_id if run_id is not None else store.start_run(stored_config)
    run = store.run_row(run_id)
    old_config = json.loads(run["config_json"])
    if old_config not in (stored_config, asdict(config)):
        raise ValueError("batch checkpoint configuration or plan differs")
    metrics = BatchMetrics()
    existing = store.batch_keywords(run_id)
    for row in existing:
        if plan.get(row["keyword_id"]) != (row["keyword_text"], row["source"]):
            raise ValueError("batch checkpoint keywords differ")
        checkpoint_ads(row)
    finalized = run["status"] in {"COMPLETED", "COMPLETED_WITH_ERRORS"}
    if finalized and len(existing) != len(entries):
        raise ValueError("batch already finalized; use cycles to retry failed keywords")

    def default_search(entry, cfg):
        return collect_keyword(entry, cfg, collector_factory=MetaAdsCollector)

    for index, entry in enumerate(entries):
        if store.keyword_done(run_id, entry.id):
            continue
        metrics.requests += 1
        try:
            ads = search_with_retry(search or default_search, entry, config, sleep, run_id=run_id)
        except Exception as exc:
            kind = failure_kind(exc)
            metrics.errors += 1
            metrics.possible_rate_limits += kind in {"rate_limit", "access_blocked"}
            store.checkpoint_search(run_id, entry, [], error=kind)
            if isinstance(exc, (RateLimitError, AuthenticationError, SessionExpiredError)):
                break
            if config.backoff_seconds:
                sleep(config.backoff_seconds)
        else:
            store.checkpoint_search(run_id, entry, ads)
            diagnostic("keyword_checkpointed", run_id=run_id, keyword_id=entry.id, observations=len(ads))
        if (index + 1) % config.batch_size == 0 and index + 1 < len(entries) and config.interval_seconds:
            sleep(config.interval_seconds)

    rows = store.batch_keywords(run_id)
    records = [(ad, {row["keyword_text"]}) for row in rows for ad in checkpoint_ads(row)]
    metrics.raw_ads = len(records)
    metrics.empty_results = sum(row["raw_count"] == 0 for row in rows)
    report = analyze_ads(records, config)
    if finalized:
        report.groups = [EvidenceGroup(**row) for row in store.run_group_snapshots(run_id)]
        report.collection["groups"] = len(report.groups)
    if len(rows) != len(entries):
        for group in report.groups:
            group.coverage_context = {"kind": "PARTIAL_COVERAGE"}
    metrics.unique_ads = report.collection["unique_ad_ids"]
    metrics.groups = len(report.groups)
    for group in report.groups:
        field = group.final_status.casefold()
        if hasattr(metrics, field):
            setattr(metrics, field, getattr(metrics, field) + 1)
    metrics.duration_seconds = round(time.monotonic() - started, 3)
    report.collection.update(asdict(metrics))
    if not finalized:
        store.finalize_batch(run_id, report, incomplete=len(rows) != len(entries))
    return run_id, report, metrics
