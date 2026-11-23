"""Controlled, resumable keyword collection."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass

from meta_ads_collector import Ad, MetaAdsCollector

from .models import RadarConfig, RadarReport
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
    config = config or RadarConfig()
    started = time.monotonic()
    run_id = run_id or store.start_run(asdict(config))
    metrics = BatchMetrics()
    records: list[tuple[Ad, set[str]]] = []

    def default_search(entry: KeywordEntry, cfg: RadarConfig) -> Iterable[Ad]:
        with MetaAdsCollector() as collector:
            return list(collector.search(query=entry.text, country=cfg.country, status=cfg.status.upper(),
                                         max_results=cfg.max_results_per_keyword))

    search_fn = search or default_search
    for index, entry in enumerate(entries):
        if store.keyword_done(run_id, entry.id):
            continue
        metrics.requests += 1
        try:
            ads = list(search_fn(entry, config))
            metrics.raw_ads += len(ads)
            metrics.empty_results += not ads
            for ad in ads:
                records.append((ad, {entry.text}))
                store.record_ad_origin(run_id, str(ad.id), entry.id)
            store.record_keyword(run_id, entry.id, entry.text, entry.source, "OK", len(ads))
        except Exception as exc:  # one failed query must not abort the run
            metrics.errors += 1
            message = str(exc).casefold()
            metrics.possible_rate_limits += any(
                signal in message for signal in ("rate limit", "429", "verification", "checkpoint", "blocked")
            )
            store.record_keyword(run_id, entry.id, entry.text, entry.source, "ERROR", error=str(exc))
            if config.backoff_seconds:
                sleep(config.backoff_seconds)
        if (index + 1) % config.batch_size == 0 and index + 1 < len(entries) and config.interval_seconds:
            sleep(config.interval_seconds)

    report = analyze_ads(records, config)
    metrics.unique_ads = report.collection["unique_ad_ids"]
    metrics.groups = len(report.groups)
    for group in report.groups:
        field = group.final_status.casefold()
        if hasattr(metrics, field):
            setattr(metrics, field, getattr(metrics, field) + 1)
    metrics.duration_seconds = round(time.monotonic() - started, 3)
    report.collection.update(asdict(metrics))
    store.persist_report(run_id, report)
    store.finish_run(run_id, "COMPLETED_WITH_ERRORS" if metrics.errors else "COMPLETED")
    return run_id, report, metrics
