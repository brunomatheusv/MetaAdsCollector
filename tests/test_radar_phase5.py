from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from meta_ads_collector.models import Ad, AdCreative, PageInfo
from radar import mining
from radar.pipeline import analyze_ads
from radar.registry import load_fernando_registry
from radar.shortlist import build_shortlist, render_shortlist
from radar.storage import RadarStore


def _ads(first, count, *, path="curso", age=1):
    return [
        Ad(
            id=str(first + i),
            page=PageInfo(id="12345", name="Loja"),
            is_active=True,
            delivery_start_time=datetime.now(timezone.utc) - timedelta(days=age),
            creatives=[AdCreative(title="Curso", body=str(i), link_url=f"https://loja.example/{path}")],
        )
        for i in range(count)
    ]


def _payload(path):
    return json.loads(path.with_name("relatorio.json").read_text(encoding="utf-8"))


def test_106_keyword_automation_resumes_and_tracks_incremental_changes(tmp_path):
    store = RadarStore(tmp_path / "radar.sqlite3")
    entries = load_fernando_registry(mining.REGISTRY)
    calls = []

    def interrupted(entry, config):
        assert (config.country, config.status, config.max_results_per_keyword, config.max_age_days) == (
            "BR",
            "active",
            20,
            60,
        )
        calls.append(entry.id)
        if entry == entries[1]:
            raise KeyboardInterrupt
        return _ads(100, 5)

    with pytest.raises(KeyboardInterrupt):
        mining.mine_once(tmp_path, store, search=interrupted, sleep=lambda _: None)
    interrupted_cycle = store.latest_managed_cycle()

    def resumed(entry, _):
        calls.append(entry.id)
        return []

    cycle, path, count = mining.mine_once(tmp_path, store, search=resumed, sleep=lambda _: None)
    assert cycle.cycle_id == interrupted_cycle
    assert cycle.coverage_kind == "FULL_COVERAGE"
    assert count == 1
    assert calls == [entries[0].id, entries[1].id, *(entry.id for entry in entries[1:])]
    report = _payload(path)
    assert len(report["keywords"]) == 106
    assert len(report["shortlist"][0]["ad_links"]) == 5
    assert set(report["shortlist"][0]["event_types"]) >= {"NEW_GROUP"}

    def next_search(entry, _):
        return _ads(100, 6) if entry == entries[0] else []

    second, path, _ = mining.mine_once(tmp_path, store, search=next_search, sleep=lambda _: None)
    assert second.cycle_id != cycle.cycle_id
    report = _payload(path)
    assert report["shortlist"][0]["new_ads_vs_history"] == 1
    assert any(event["event_type"] == "NEW_ADS" for event in report["incremental_events"])
    assert "lp_enrichment" not in report["collection"]


def test_finalized_cycle_recovers_failed_export_over_previous_partial(tmp_path, monkeypatch):
    store = RadarStore(tmp_path / "radar.sqlite3")

    def partial(entry, config):
        if entry.text == "apostila":
            raise RuntimeError("rate limit")
        return []

    cycle, path, _ = mining.mine_once(tmp_path, store, search=partial, sleep=lambda _: None)
    assert cycle.coverage_kind == "PARTIAL_COVERAGE"
    assert _payload(path)["coverage_kind"] == "PARTIAL_COVERAGE"
    original = mining.write_reports

    def export_failed(paths):
        raise OSError("simulated write failure")

    monkeypatch.setattr(mining, "write_reports", export_failed)
    with pytest.raises(OSError, match="simulated"):
        mining.mine_once(tmp_path, store, search=lambda *_: [], sleep=lambda _: None)
    monkeypatch.setattr(mining, "write_reports", original)

    def no_search(*_):
        pytest.fail("the completed cycle must be exported without any network access")

    recovered, path, _ = mining.mine_once(tmp_path, store, search=no_search, sleep=lambda _: None)
    assert recovered.cycle_id == cycle.cycle_id
    assert _payload(path)["coverage_kind"] == "FULL_COVERAGE"


def test_shortlist_ranks_only_current_high_confidence_evidence_and_respects_review():
    ads = _ads(100, 5, path="cinco") + _ads(200, 6, path="seis") + _ads(300, 7, path="sete")
    report = analyze_ads([(item, {"ebook"}) for item in ads])
    assert {group.final_status for group in report.groups} == {"CANDIDATO_RADAR"}
    top = build_shortlist(report, [], limit=2)
    assert [row["observed_ads"] for row in top] == [7, 6]
    groups = {g.related_ads_count: g for g in report.groups}
    groups[6].coverage_context = {"kind": "PARTIAL_COVERAGE", "historical_only_ad_ids": ["200", "201"]}
    shortlist = build_shortlist(report, [], limit=5, review_status={groups[7].group_id: "DESCARTADO"})
    assert [row["observed_ads"] for row in shortlist] == [5]
    assert all(group.review_status == "NOVO" for group in report.groups)


def test_offline_reexport_uses_latest_review_and_keeps_partial_label(tmp_path):
    store = RadarStore(tmp_path / "radar.sqlite3")

    def partial(entry, _):
        if entry.text == "apostila":
            raise RuntimeError("rate limit")
        return _ads(100, 5)

    cycle, path, count = mining.mine_once(tmp_path, store, search=partial, sleep=lambda _: None)
    assert count == 1
    assert "Lista provisória" in path.read_text(encoding="utf-8")
    group = _payload(path)["shortlist"][0]["group_id"]
    store.set_review_status(group, "DESCARTADO")
    _, path, count = mining.mine_once(tmp_path, store, report_cycle=cycle.cycle_id)
    assert count == 0
    assert "Lista provisória" in render_shortlist(cycle, [], limit=5)
    assert store.review_statuses()[group] == "DESCARTADO"
