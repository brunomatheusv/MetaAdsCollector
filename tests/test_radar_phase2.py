from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from meta_ads_collector import Ad, AdCreative
from meta_ads_collector.models import PageInfo
from radar.batch import run_batch
from radar.models import RadarConfig
from radar.pipeline import analyze_ads
from radar.registry import (
    FERNANDO_EXPECTED_CATEGORIES,
    FERNANDO_SOURCE,
    KeywordEntry,
    load_fernando_registry,
    stable_keyword_id,
)
from radar.reporting import render_json, render_markdown
from radar.storage import RadarStore

NOW = datetime(2026, 11, 23, tzinfo=timezone.utc)


def make_ad(number: int) -> Ad:
    return Ad(
        id=f"ad-{number}",
        page=PageInfo(id="page-1", name="Anunciante"),
        is_active=True,
        ad_status="ACTIVE",
        delivery_start_time=NOW - timedelta(days=number),
        creatives=[AdCreative(title="Oferta", body=f"Criativo {number}", link_url="https://loja.example/oferta")],
    )


def entries(*texts: str) -> list[KeywordEntry]:
    return [KeywordEntry(stable_keyword_id(text), text, FERNANDO_SOURCE) for text in texts]


def test_registry_requires_106_exact_strings_and_preserves_provenance(tmp_path):
    source = [f"termo fornecido {index}" for index in range(106)]
    categories = []
    offset = 0
    for name, count in FERNANDO_EXPECTED_CATEGORIES.items():
        categories.append({"name": name, "terms": source[offset:offset + count]})
        offset += count
    path = tmp_path / "fernando.json"
    path.write_text(json.dumps({"source": FERNANDO_SOURCE, "categories": categories}), encoding="utf-8")
    registry = load_fernando_registry(path)
    assert [item.text for item in registry] == source
    assert {item.source for item in registry} == {"Fernando Oliveira PRO"}
    assert len({item.id for item in registry}) == 106


def test_registry_rejects_missing_source_terms(tmp_path):
    path = tmp_path / "incompleto.json"
    path.write_text(json.dumps({"source": FERNANDO_SOURCE, "categories": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="category counts"):
        load_fernando_registry(path)


def test_official_registry_matches_pdf_counts_and_has_no_duplicates():
    path = Path(__file__).parents[1] / "radar" / "data" / "fernando-oliveira-pro-106.json"
    registry = load_fernando_registry(path)
    assert len(registry) == 106
    assert len({item.text for item in registry}) == 106
    assert {item.source for item in registry} == {FERNANDO_SOURCE}
    assert {
        category: sum(item.category == category for item in registry)
        for category in FERNANDO_EXPECTED_CATEGORIES
    } == FERNANDO_EXPECTED_CATEGORIES


def test_batch_deduplicates_globally_and_keeps_all_keyword_origins(tmp_path):
    store = RadarStore(tmp_path / "radar.sqlite3")
    shared = make_ad(1)
    run_id, report, metrics = run_batch(
        entries("kw-a", "kw-b"), store, RadarConfig(interval_seconds=0, backoff_seconds=0),
        search=lambda entry, _config: [shared], sleep=lambda _seconds: None,
    )
    assert metrics.raw_ads == 2
    assert metrics.unique_ads == 1
    assert report.groups[0].keywords == ["kw-a", "kw-b"]
    with store.connect() as db:
        origins = db.execute("SELECT keyword_id FROM ad_observations WHERE run_id=?", (run_id,)).fetchall()
    assert len(origins) == 2


def test_keyword_failure_does_not_invalidate_batch_and_empty_is_recorded(tmp_path):
    store = RadarStore(tmp_path / "radar.sqlite3")

    def search(entry, _config):
        if entry.text == "falha":
            raise RuntimeError("rate limit simulated")
        return [] if entry.text == "vazia" else [make_ad(1)]

    run_id, report, metrics = run_batch(
        entries("falha", "vazia", "ok"), store, RadarConfig(interval_seconds=0, backoff_seconds=0),
        search=search, sleep=lambda _seconds: None,
    )
    assert metrics.errors == 1
    assert metrics.empty_results == 1
    assert report.collection["unique_ad_ids"] == 1
    with store.connect() as db:
        statuses = dict(db.execute(
            "SELECT keyword_text,status FROM keyword_results WHERE run_id=?", (run_id,)
        ).fetchall())
    assert statuses == {"falha": "ERROR", "vazia": "OK", "ok": "OK"}


def test_live_adapter_uses_collector_status_enum(monkeypatch, tmp_path):
    calls = []

    class FakeCollector:
        def __init__(self):
            from meta_ads_collector.events import EventEmitter

            self.event_emitter = EventEmitter()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def search(self, **kwargs):
            calls.append(kwargs)
            self.event_emitter.emit("collection_finished", {"total_pages": 1})
            return []

    monkeypatch.setattr("radar.batch.MetaAdsCollector", FakeCollector)
    run_batch(
        entries("kw"), RadarStore(tmp_path / "radar.sqlite3"),
        RadarConfig(interval_seconds=0, backoff_seconds=0), sleep=lambda _: None,
    )
    assert calls == [{"query": "kw", "country": "BR", "status": "ACTIVE", "max_results": 20}]


def test_resuming_same_run_skips_completed_keyword_without_duplicate_origin(tmp_path):
    store = RadarStore(tmp_path / "radar.sqlite3")
    config = RadarConfig(interval_seconds=0, backoff_seconds=0)
    run_id = store.start_run(asdict(config))
    entry = entries("kw-a")[0]
    run_batch([entry], store, config, run_id=run_id, search=lambda *_: [make_ad(1)], sleep=lambda _: None)
    calls = []
    run_batch([entry], store, config, run_id=run_id, search=lambda *_: calls.append(1), sleep=lambda _: None)
    assert calls == []
    with store.connect() as db:
        count = db.execute("SELECT COUNT(*) FROM ad_observations WHERE run_id=?", (run_id,)).fetchone()[0]
    assert count == 1


def test_history_3_4_7_and_human_review_are_preserved(tmp_path):
    store = RadarStore(tmp_path / "radar.sqlite3")
    group_id = None
    for count in (3, 4, 7):
        report = analyze_ads([(make_ad(i), {"kw"}) for i in range(1, count + 1)], now=NOW)
        run_id = store.start_run(report.config)
        store.persist_report(run_id, report)
        store.finish_run(run_id)
        group_id = report.groups[0].group_id
        if count == 3:
            assert report.groups[0].review_status == "NOVO"
            store.set_review_status(group_id, "APROVADO")
        else:
            assert report.groups[0].review_status == "APROVADO"
    assert store.group_history(group_id) == [3, 4, 7]


def test_new_candidate_is_never_automatically_approved_and_report_is_evidence_only(tmp_path):
    store = RadarStore(tmp_path / "radar.sqlite3")
    report = analyze_ads([(make_ad(i), {"kw"}) for i in range(1, 7)], now=NOW)
    run_id = store.start_run(report.config)
    store.persist_report(run_id, report)
    group = report.groups[0]
    assert group.final_status == "CANDIDATO_RADAR"
    assert group.review_status == "NOVO"
    assert "https://www.facebook.com/ads/library" not in render_json(report)
    markdown = render_markdown(report)
    assert "CANDIDATO_RADAR / NOVO" in markdown
    assert "IDs para revisao: ad-1" in markdown
