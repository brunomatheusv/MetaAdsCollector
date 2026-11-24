from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from meta_ads_collector import Ad, AdCreative
from meta_ads_collector.models import PageInfo
from radar.cycle import get_cycle, run_cycle, start_cycle
from radar.models import RadarConfig
from radar.registry import FERNANDO_SOURCE, KeywordEntry
from radar.reporting import render_cycle_json, render_cycle_markdown
from radar.storage import RadarStore

NOW = datetime(2026, 11, 24, tzinfo=timezone.utc)
CONFIG = RadarConfig(interval_seconds=0, backoff_seconds=0)


def entries(count: int = 106) -> list[KeywordEntry]:
    return [KeywordEntry(f"kw-{i:03}", f"keyword {i}", FERNANDO_SOURCE, "test") for i in range(count)]


def ad(number: int) -> Ad:
    return Ad(
        id=f"ad-{number}", page=PageInfo(id="page-1", name="Loja"), is_active=True, ad_status="ACTIVE",
        delivery_start_time=NOW - timedelta(days=number),
        creatives=[AdCreative(title="Oferta", body=f"Criativo {number}", link_url="https://loja.example/oferta")],
    )


def execute(tmp_path, search, count: int = 106, **kwargs):
    store = RadarStore(tmp_path / "radar.sqlite3")
    plan = entries(count)
    cycle = start_cycle(plan, store, CONFIG)
    cycle, report = run_cycle(store, cycle.cycle_id, plan, CONFIG, search=search, sleep=lambda _: None, **kwargs)
    return store, plan, cycle, report


def test_a_106_success_is_full(tmp_path):
    _, _, cycle, _ = execute(tmp_path, lambda *_: [])
    assert (cycle.status, cycle.coverage_kind, cycle.succeeded, cycle.empty) == (
        "COMPLETED", "FULL_COVERAGE", 106, 106,
    )


def test_b_one_error_is_partial_and_retry_eligible(tmp_path):
    def search(entry, _config):
        if entry.id == "kw-105":
            raise RuntimeError("429")
        return []

    _, _, cycle, _ = execute(tmp_path, search)
    assert cycle.coverage_kind == "PARTIAL_COVERAGE"
    assert (cycle.succeeded, cycle.failed, cycle.pending) == (105, 1, 0)


def test_c_empty_is_success_for_full_coverage(tmp_path):
    _, _, cycle, _ = execute(tmp_path, lambda entry, _: [] if entry.id == "kw-105" else [ad(1)])
    assert cycle.coverage_kind == "FULL_COVERAGE"
    assert cycle.empty == 1


def test_d_gate_ten_is_partial_relative_to_official_plan(tmp_path):
    _, _, cycle, _ = execute(tmp_path, lambda *_: [], count=10)
    assert cycle.coverage_kind == "PARTIAL_COVERAGE"
    assert cycle.total_keywords_expected == 106
    assert cycle.coverage_ratio == pytest.approx(10 / 106)


def test_e_f_interruption_resume_only_remaining_and_empty_not_repeated(tmp_path):
    calls: list[str] = []
    store = RadarStore(tmp_path / "resume.sqlite3")
    plan = entries()
    started = start_cycle(plan, store, CONFIG)
    partial, _ = run_cycle(
        store, started.cycle_id, plan, CONFIG, search=lambda entry, _: calls.append(entry.id) or [],
        max_keywords=5, sleep=lambda _: None,
    )
    assert partial.status == "PARTIAL"
    assert calls == [f"kw-{i:03}" for i in range(5)]
    complete, _ = run_cycle(
        store, started.cycle_id, plan, CONFIG, search=lambda entry, _: calls.append(entry.id) or [],
        sleep=lambda _: None,
    )
    assert complete.coverage_kind == "FULL_COVERAGE"
    assert len(calls) == len(set(calls)) == 106


def test_g_incompatible_config_or_registry_is_blocked(tmp_path):
    store = RadarStore(tmp_path / "blocked.sqlite3")
    plan = entries()
    cycle = start_cycle(plan, store, CONFIG)
    with pytest.raises(ValueError, match="registry/plan"):
        run_cycle(store, cycle.cycle_id, plan[:-1], CONFIG, search=lambda *_: [])
    with pytest.raises(ValueError, match="configuration"):
        run_cycle(store, cycle.cycle_id, plan, RadarConfig(max_results_per_keyword=10), search=lambda *_: [])


def test_h_i_partial_preserves_known_count_but_allows_positive_evidence(tmp_path):
    store = RadarStore(tmp_path / "partial.sqlite3")
    full_plan = entries()
    first = start_cycle(full_plan, store, CONFIG)
    _, first_report = run_cycle(
        store, first.cycle_id, full_plan, CONFIG,
        search=lambda entry, _: [ad(i) for i in range(1, 7)] if entry.id == "kw-000" else [], sleep=lambda _: None,
    )
    group_id = first_report.groups[0].group_id
    subset = entries(10)
    second = start_cycle(subset, store, CONFIG)
    second, report = run_cycle(
        store, second.cycle_id, subset, CONFIG,
        search=lambda entry, _: [ad(1), ad(7)] if entry.id == "kw-000" else [], sleep=lambda _: None,
    )
    group = report.groups[0]
    assert group.related_ads_count == 7
    assert group.final_status == "CANDIDATO_RADAR"
    assert group.coverage_context["kind"] == "PARTIAL_COVERAGE"
    assert "LEFT_RADAR" not in {event.event_type for event in store.events(second.run_ids[-1])}
    assert group_id == group.group_id


def test_j_k_l_two_full_absences_then_left_and_reappearance_resets(tmp_path):
    store = RadarStore(tmp_path / "absence.sqlite3")
    plan = entries()

    def cycle(search):
        item = start_cycle(plan, store, CONFIG)
        return run_cycle(store, item.cycle_id, plan, CONFIG, search=search, sleep=lambda _: None)

    first, report = cycle(lambda entry, _: [ad(i) for i in range(1, 7)] if entry.id == "kw-000" else [])
    group_id = report.groups[0].group_id
    second, _ = cycle(lambda *_: [])
    assert "LEFT_RADAR" not in {event.event_type for event in store.events(second.run_ids[-1])}
    assert store.freshness(group_id)["freshness_status"] == "STALE"
    third, _ = cycle(lambda *_: [])
    assert "LEFT_RADAR" in {event.event_type for event in store.events(third.run_ids[-1])}
    assert store.freshness(group_id)["freshness_status"] == "POSSIBLY_GONE"
    cycle(lambda entry, _: [ad(i) for i in range(1, 7)] if entry.id == "kw-000" else [])
    fresh = store.freshness(group_id)
    assert (fresh["freshness_status"], fresh["consecutive_full_absences"]) == ("ACTIVE_OBSERVED", 0)


def test_m_acknowledgement_and_review_status_survive(tmp_path):
    store, _, cycle, report = execute(
        tmp_path, lambda entry, _: [ad(1)] if entry.id == "kw-000" else []
    )
    group_id = report.groups[0].group_id
    run_id = cycle.run_ids[-1]
    store.set_review_status(group_id, "DESCARTADO")
    store.acknowledge(run_id, group_id)
    assert store.review_queue(run_id) == []
    assert store.latest_group_evidence(group_id)["review_status"] == "NOVO"
    assert store.review_queue(run_id, include_acknowledged=True)[0].review_status == "DESCARTADO"


def test_n_completed_cycle_cannot_duplicate_events(tmp_path):
    store, plan, cycle, _ = execute(tmp_path, lambda entry, _: [ad(1)] if entry.id == "kw-000" else [])
    count = len(store.events(cycle.run_ids[-1]))
    with pytest.raises(ValueError, match="already completed"):
        run_cycle(store, cycle.cycle_id, plan, CONFIG, search=lambda *_: [])
    assert len(store.events(cycle.run_ids[-1])) == count


def test_o_phase2_phase3_database_migrates_without_loss(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE runs (id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
          status TEXT NOT NULL, config_json TEXT NOT NULL);
        CREATE TABLE keyword_results (run_id INTEGER, keyword_id TEXT, keyword_text TEXT, source TEXT,
          status TEXT, raw_count INTEGER DEFAULT 0, error TEXT, PRIMARY KEY(run_id,keyword_id));
        CREATE TABLE ad_observations (run_id INTEGER, ad_id TEXT, keyword_id TEXT, observed_at TEXT,
          PRIMARY KEY(run_id,ad_id,keyword_id));
        CREATE TABLE groups (group_id TEXT PRIMARY KEY, review_status TEXT, first_seen TEXT, last_seen TEXT);
        CREATE TABLE group_history (run_id INTEGER, group_id TEXT, observed_at TEXT, related_ads_count INTEGER,
          final_status TEXT, evidence_json TEXT, PRIMARY KEY(run_id,group_id));
        CREATE TABLE incremental_events (run_id INTEGER,group_id TEXT,event_type TEXT,occurred_at TEXT,
          before_json TEXT,after_json TEXT,reasons_json TEXT,PRIMARY KEY(run_id,group_id,event_type));
        CREATE TABLE review_queue (run_id INTEGER,group_id TEXT,queue_type TEXT,priority INTEGER,created_at TEXT,
          reason TEXT,event_types_json TEXT,group_json TEXT,acknowledged_at TEXT,PRIMARY KEY(run_id,group_id));
    """)
    db.execute("INSERT INTO runs VALUES(1,'old','old','COMPLETED','{}')")
    db.execute("INSERT INTO groups VALUES('legacy','DESCARTADO','old','old')")
    db.commit()
    db.close()
    store = RadarStore(path)
    assert store.cycle_row if store else False
    with store.connect() as migrated:
        review = migrated.execute("SELECT review_status FROM groups WHERE group_id='legacy'").fetchone()[0]
        assert review == "DESCARTADO"
        tables = {row[0] for row in migrated.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"collection_cycles", "cycle_keywords", "cycle_runs", "group_freshness"} <= tables


def test_p_report_distinguishes_plan_coverage_from_limit(tmp_path):
    _, _, cycle, report = execute(tmp_path, lambda *_: [])
    raw = json.loads(render_cycle_json(cycle, report))
    markdown = render_cycle_markdown(cycle, report)
    assert raw["config"]["max_results_per_keyword"] == 20
    assert "nao cobre toda a Meta Ads Library" in raw["plan_scope_warning"]
    assert "nao significa cobertura total da Meta Ads Library" in markdown
    assert get_cycle(RadarStore(tmp_path / "radar.sqlite3"), cycle.cycle_id).coverage_kind == "FULL_COVERAGE"
