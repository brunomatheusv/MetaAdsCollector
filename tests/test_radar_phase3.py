from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from meta_ads_collector import Ad, AdCreative
from meta_ads_collector.models import PageInfo
from radar.models import RadarConfig
from radar.pipeline import analyze_ads
from radar.storage import RadarStore

NOW = datetime(2026, 11, 23, tzinfo=timezone.utc)


def make_ad(number: int, *, days: int | None = None, active: bool | None = True,
            multicard: bool = False) -> Ad:
    creatives = [AdCreative(title="Oferta", body=f"Criativo {number}", link_url="https://loja.example/oferta")]
    if multicard:
        creatives = [AdCreative(link_url="https://loja.example/a"), AdCreative(link_url="https://loja.example/b")]
    return Ad(
        id=f"ad-{number}", page=PageInfo(id="page-1", name="Anunciante"), is_active=active,
        ad_status="ACTIVE" if active else None,
        delivery_start_time=NOW - timedelta(days=days if days is not None else number), creatives=creatives,
    )


def persist(store: RadarStore, ads: list[Ad]):
    report = analyze_ads([(ad, {"kw"}) for ad in ads], RadarConfig(), now=NOW)
    run_id = store.start_run(report.config)
    events = store.persist_report(run_id, report)
    store.finish_run(run_id)
    return run_id, report.groups[0], events


def kinds(events):
    return {event.event_type for event in events}


def test_new_group_only_once_and_unchanged_reviewed_group_is_silent(tmp_path):
    store = RadarStore(tmp_path / "radar.sqlite3")
    _, group, first = persist(store, [make_ad(1)])
    assert kinds(first) == {"NEW_GROUP"}
    store.set_review_status(group.group_id, "APROVADO")
    run_id, group, second = persist(store, [make_ad(1)])
    assert second == []
    assert group.review_status == "APROVADO"
    assert store.review_queue(run_id) == []


def test_new_ids_produce_new_ads_and_count_increased(tmp_path):
    store = RadarStore(tmp_path / "radar.sqlite3")
    persist(store, [make_ad(1)])
    _, _, events = persist(store, [make_ad(1), make_ad(2)])
    assert {"NEW_ADS", "COUNT_INCREASED", "EVIDENCE_CHANGED"} <= kinds(events)
    new_ads = next(event for event in events if event.event_type == "NEW_ADS")
    assert new_ads.reasons == ["novos IDs: ad-2"]


def test_three_four_seven_enters_radar_only_on_third_run_and_tops_queue(tmp_path):
    store = RadarStore(tmp_path / "radar.sqlite3")
    outputs = [persist(store, [make_ad(i) for i in range(1, count + 1)]) for count in (3, 4, 7)]
    assert "ENTERED_RADAR" not in kinds(outputs[0][2])
    assert "COUNT_INCREASED" in kinds(outputs[1][2])
    assert "ENTERED_RADAR" not in kinds(outputs[1][2])
    assert {"COUNT_INCREASED", "ENTERED_RADAR"} <= kinds(outputs[2][2])
    queue = store.review_queue(outputs[2][0])
    assert queue[0].queue_type == "ENTROU_NO_RADAR"
    assert queue[0].priority == 10
    assert outputs[2][1].final_status == "CANDIDATO_RADAR"


def test_left_radar_is_conservative_status_transition(tmp_path):
    store = RadarStore(tmp_path / "radar.sqlite3")
    _, first, _ = persist(store, [make_ad(i) for i in range(1, 7)])
    run_id, second, events = persist(store, [make_ad(i, active=None) for i in range(1, 7)])
    assert first.final_status == "CANDIDATO_RADAR"
    assert second.final_status == "EVIDENCIA_INSUFICIENTE"
    assert "LEFT_RADAR" in kinds(events)
    assert store.review_queue(run_id)[0].queue_type == "SAIU_DO_RADAR"


def test_partial_collection_does_not_claim_left_radar(tmp_path):
    store = RadarStore(tmp_path / "radar.sqlite3")
    persist(store, [make_ad(i) for i in range(1, 7)])
    _, _, events = persist(store, [make_ad(i) for i in range(1, 4)])
    assert "LEFT_RADAR" not in kinds(events)


def test_human_statuses_survive_collection_and_discarded_change_is_recorded(tmp_path):
    for status in ("APROVADO", "DESCARTADO", "ACOMPANHAR"):
        store = RadarStore(tmp_path / f"{status}.sqlite3")
        _, group, _ = persist(store, [make_ad(1)])
        store.set_review_status(group.group_id, status)
        unchanged_run, unchanged, events = persist(store, [make_ad(1)])
        assert unchanged.review_status == status
        assert events == []
        assert store.review_queue(unchanged_run) == []
        changed_run, changed, events = persist(store, [make_ad(1), make_ad(2)])
        assert changed.review_status == status
        assert "NEW_ADS" in kinds(events)
        assert store.review_queue(changed_run)[0].review_status == status


def test_multicard_and_missing_date_never_promote(tmp_path):
    store = RadarStore(tmp_path / "radar.sqlite3")
    _, ambiguous, _ = persist(store, [make_ad(1, multicard=True)])
    assert ambiguous.final_status == "INVESTIGAR"
    missing = [make_ad(i) for i in range(1, 7)]
    for ad in missing:
        ad.delivery_start_time = None
    _, insufficient, events = persist(RadarStore(tmp_path / "missing.sqlite3"), missing)
    assert insufficient.final_status == "EVIDENCIA_INSUFICIENTE"
    assert "ENTERED_RADAR" not in kinds(events)


def test_phase2_database_is_migrated_additively(tmp_path):
    path = tmp_path / "phase2.sqlite3"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE runs (id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
          status TEXT NOT NULL, config_json TEXT NOT NULL);
        CREATE TABLE groups (group_id TEXT PRIMARY KEY, review_status TEXT NOT NULL DEFAULT 'NOVO',
          first_seen TEXT NOT NULL, last_seen TEXT NOT NULL);
        CREATE TABLE group_history (run_id INTEGER NOT NULL, group_id TEXT NOT NULL, observed_at TEXT NOT NULL,
          related_ads_count INTEGER NOT NULL, final_status TEXT NOT NULL, evidence_json TEXT NOT NULL,
          PRIMARY KEY(run_id, group_id));
        CREATE TABLE keyword_results (run_id INTEGER NOT NULL, keyword_id TEXT NOT NULL, keyword_text TEXT NOT NULL,
          source TEXT NOT NULL, status TEXT NOT NULL, raw_count INTEGER NOT NULL DEFAULT 0, error TEXT,
          PRIMARY KEY(run_id, keyword_id));
        CREATE TABLE ad_observations (run_id INTEGER NOT NULL, ad_id TEXT NOT NULL, keyword_id TEXT NOT NULL,
          observed_at TEXT NOT NULL, PRIMARY KEY(run_id, ad_id, keyword_id));
    """)
    db.execute("INSERT INTO runs VALUES(1, 'old', 'old', 'COMPLETED', '{}')")
    db.execute("INSERT INTO groups VALUES('legacy', 'DESCARTADO', 'old', 'old')")
    db.execute("INSERT INTO group_history VALUES(1, 'legacy', 'old', 3, 'REJEITADO', ?)",
               (json.dumps({"group_id": "legacy"}),))
    db.commit()
    db.close()
    store = RadarStore(path)
    with store.connect() as migrated:
        review = migrated.execute("SELECT review_status FROM groups WHERE group_id='legacy'").fetchone()[0]
        assert review == "DESCARTADO"
        tables = {row[0] for row in migrated.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"incremental_events", "review_queue"} <= tables


def test_event_persistence_is_idempotent_on_same_run(tmp_path):
    store = RadarStore(tmp_path / "radar.sqlite3")
    report = analyze_ads([(make_ad(1), {"kw"})], now=NOW)
    run_id = store.start_run(report.config)
    store.persist_report(run_id, report)
    store.persist_report(run_id, report)
    assert len(store.events(run_id)) == 1
    assert len(store.review_queue(run_id)) == 1
    store.acknowledge(run_id, report.groups[0].group_id)
    assert store.review_queue(run_id) == []
    assert report.groups[0].review_status == "NOVO"
    assert store.events_since(0) == store.events(run_id)
    assert store.review_queue_since(run_id) == []
