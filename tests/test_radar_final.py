from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from meta_ads_collector.models import Ad, AdCreative, PageInfo
from radar import mining, normalize
from radar.cycle import run_cycle, start_cycle
from radar.models import RadarConfig
from radar.registry import load_fernando_registry
from radar.storage import RadarStore

CDN = "https://scontent.example.fbcdn.net/a.jpg"
NOW = datetime.now(timezone.utc)


def _ad(id, asset, *, path="/", age=2):
    return Ad(
        id=str(id), page=PageInfo(id="12", name="Loja"), is_active=True,
        delivery_start_time=NOW - timedelta(days=age),
        creatives=[AdCreative(title="Curso", image_url=asset, link_url=f"https://loja.example{path}")],
    )


@pytest.mark.parametrize("url", [
    "https://cdn.other.example/a.jpg?oh=1&oe=2", "https://fbcdn.net.other.example/a.jpg?oh=1",
    "https://user@scontent.example.fbcdn.net/a.jpg?oh=1",
])
def test_media_allowlist_does_not_apply_to_other_urls(url):
    assert normalize._media_identity(url) == url


def test_cdn_keep_host_path_and_functional_parameters():
    assert normalize._media_identity(CDN + "?oh=1&oe=3&sku=A&efg=X&stp=Y&sku=B") == (
        CDN + "?sku=A&efg=X&stp=Y&sku=B"
    )
    assert normalize._media_identity(CDN + "?sku=A") != normalize._media_identity(CDN + "?sku=B&oh=1")
    assert normalize._media_identity(CDN + "?oh=1") != normalize._media_identity(CDN.replace("a.jpg", "b.jpg"))


def test_aging_alone_is_silent_and_crossing_window_has_events():
    snapshot = {"final_status": "CANDIDATO_RADAR", "ad_ids": ["1"], "ages_days": [59],
                "criterion_max_age": "PASSA", "start_dates": ["2026-09-01"]}
    assert not RadarStore._derive_events(1, "now", "a", snapshot, {**snapshot, "ages_days": [60]})
    aged = {**snapshot, "ages_days": [61], "criterion_max_age": "FALHA", "final_status": "REJEITADO"}
    assert {e.event_type for e in RadarStore._derive_events(1, "now", "a", snapshot, aged)} >= {
        "STATUS_CHANGED", "EVIDENCE_CHANGED", "LEFT_RADAR",
    }


def test_migration_keeps_review_history_and_reobservation_separate(tmp_path, monkeypatch):
    store = RadarStore(tmp_path / "radar.sqlite3")
    terms = load_fernando_registry(mining.REGISTRY)
    cfg = RadarConfig(interval_seconds=0, backoff_seconds=0)

    def cycle(asset):
        group = start_cycle(terms, store, cfg)
        return run_cycle(store, group.cycle_id, terms, cfg, sleep=lambda _: None,
                         search=lambda entry, _: [_ad(1, asset)] if asset and entry == terms[0] else [])

    with monkeypatch.context() as patch:
        patch.setattr(normalize, "_media_identity", lambda url: url)
        initial, report = cycle(CDN + "?oh=1&sku=A")
    old = report.groups[0].group_id
    store.set_review_status(old, "ACOMPANHAR")
    store.acknowledge(initial.run_ids[0], old)
    with store.connect() as db:
        db.execute("DELETE FROM radar_migrations")
    absent, report = cycle(None)
    assert absent.coverage_kind == "FULL_COVERAGE" and not report.groups
    assert not store.events(absent.run_ids[-1])
    assert store.freshness(old)["consecutive_full_absences"] == 0
    with store.connect() as db:
        target = db.execute("SELECT canonical_id FROM media_group_aliases WHERE legacy_id=?", (old,)).fetchone()[0]
        assert db.execute("SELECT group_id FROM groups WHERE group_id=?", (target,)).fetchone() is None
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert not db.execute("PRAGMA foreign_key_check").fetchall()

    stable, report = cycle(CDN + "?oh=2&sku=A")
    migrated = report.groups[0]
    assert migrated.group_id == target and migrated.group_id != old
    assert migrated.review_status == "ACOMPANHAR"
    assert store.group_history(migrated.group_id) == [1, 1]
    assert not store.events(stable.run_ids[0])
    assert not store.review_queue(initial.run_ids[0])

    changed, report = cycle(CDN + "?oh=3&sku=B")
    assert report.groups[0].group_id != migrated.group_id and report.groups[0].review_status == "NOVO"
    assert [e.event_type for e in store.events(changed.run_ids[0])] == ["CREATIVE_REOBSERVED"]
    assert store.freshness(migrated.group_id)["consecutive_full_absences"] == 1
    assert store.review_statuses()[old] == "ACOMPANHAR"


def test_commercial_shortlist_requires_bound_evidence_and_keeps_technical(tmp_path):
    store = RadarStore(tmp_path / "radar.sqlite3")
    terms = load_fernando_registry(mining.REGISTRY)
    ads = [_ad(i, CDN, path="/curso") for i in range(1, 6)]
    cycle, path, count = mining.mine_once(
        tmp_path, store, search=lambda entry, _: ads if entry == terms[0] else [], sleep=lambda _: None,
    )
    report_path = path.with_name("relatorio.json")

    def read():
        return json.loads(report_path.read_text(encoding="utf8"))

    technical = read()["technical_shortlist"][0]
    assert count == 0 and read()["commercial_assessments"][0]["category"] == "INDETERMINADO"
    evidence = {
        "cycle_id": cycle.cycle_id, "group_id": technical["group_id"], "page_id": "12", "name": "Curso",
        "lp": "https://loja.example/curso", "checkout": "https://checkout.example/curso",
        "category": "INFOPRODUTO_LOW_TICKET_BR", "market": "BR", "price": 37, "price_verified": True,
        "currency": "BRL", "niche": "Costura", "format": "Curso gravado", "promise": "Aprender costura",
        "sources": ["https://loja.example/curso", "https://checkout.example/curso"], "verified_at": "2026-11-25",
    }
    reviews = tmp_path / "qualificacao-comercial.json"
    reviews.write_text(json.dumps([{**evidence, "currency": "EUR"}]), encoding="utf8")
    assert mining.mine_once(tmp_path, store, report_cycle=cycle.cycle_id)[2] == 0
    reviews.write_text(json.dumps([evidence]), encoding="utf8")
    assert mining.mine_once(tmp_path, store, report_cycle=cycle.cycle_id)[2] == 1
    assert read()["shortlist"][0]["price"] == 37
    assert read()["commercial_assessments"][0]["verifiable_creative_fingerprints"] == 1
    assert store.review_statuses()[technical["group_id"]] == "NOVO"
