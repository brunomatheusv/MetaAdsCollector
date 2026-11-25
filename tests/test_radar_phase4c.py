from __future__ import annotations

from copy import deepcopy

import pytest

from radar.lp import LPConfig, investigate_groups
from radar.lp_evidence import extract_evidence
from radar.lp_http import LPFetchError, PageResponse
from radar.reporting import render_lp_markdown
from radar.storage import RadarStore

HOST = "https://offer.example.com"
HTML = b"<html><title>Produto exclusivo</title><h1>Curso de costura</h1></html>"
CONFIG = LPConfig(retries=0, global_interval=0, host_interval=0)


def _response(path, html=HTML):
    return PageResponse(HOST + path, 200, {"content-type": "text/html"}, html)


def _group(name, ids, url, *, page="123", conflict=(), age="PASSA", status="INVESTIGAR"):
    return {
        "group_id": name,
        "final_status": status,
        "criterion_max_age": age,
        "page_id": page,
        "conflicting_evidence": list(conflict),
        "destination_urls": [url],
        "destination_fingerprints": [url],
        "ad_ids": list(ids),
        "grouping_confidence": "MEDIA",
    }


def _redirect_fetch(targets, calls):
    def fetch(url, *, before_request, **_):
        calls.append(url)
        before_request("offer.example.com")
        response = _response(targets[url])
        return PageResponse(response.url, response.status, response.headers, response.body, (url, response.url))

    return fetch


@pytest.mark.parametrize("source", [HOST + "/", "https://bit.ly/aBc"])
def test_redirect_to_specific_destination_is_strong(source):
    result = extract_evidence(_response("/curso/costura"), source)
    assert result["level"] == "STRONG" and result["fingerprint"]


def test_canonical_and_structured_offer_are_deterministic():
    html = b'<title>Curso de costura</title><link rel="canonical" href="/curso/costura">'
    assert extract_evidence(_response("/", html), HOST + "/")["level"] == "STRONG"
    html = b'<script type="application/ld+json">{"@type":"Product","name":"Curso","sku":"A123"}</script>'
    assert extract_evidence(_response("/", html), HOST + "/")["level"] == "STRONG"


def test_tracking_removed_and_functional_parameters_distinguish_products():
    def key(query):
        return extract_evidence(_response("/checkout/produto" + query), HOST + "/")["fingerprint"]

    assert key("?sku=A&ref=v1&src=real&utm_source=one") == key("?sku=A&ref=v1&src=real&utm_source=two")
    assert key("?sku=A&ref=v1&src=real") != key("?sku=B&ref=v1&src=real")
    assert key("?sku=A&ref=v1&src=real") != key("?sku=A&ref=v2&src=real")
    assert key("?sku=A&ref=v1&src=real") != key("?sku=A&ref=v1&src=other")
    result = extract_evidence(_response("/produto"), HOST + "/?sku=A")
    assert result["fingerprint"] is None


@pytest.mark.parametrize(
    "html",
    [
        b"<title>Home</title><h1>Landing Page</h1>",
        b"<title>Curso de costura</title><h1>Aulas para iniciantes</h1>",
        b'<link rel="canonical" href="https://other.example.com/product"><h1>Curso de costura</h1>',
        b'<script type="application/ld+json">[{"@type":"Product","sku":"A"},{"@type":"Product","sku":"B"}]</script>',
    ],
)
def test_weak_or_conflicting_root_does_not_get_fingerprint(html):
    assert not extract_evidence(_response("/", html), HOST + "/")["fingerprint"]


def test_only_equivalent_strong_groups_propose_3_plus_2(tmp_path):
    groups = [_group("a", "123", HOST + "/go/a"), _group("b", "45", HOST + "/go/b")]
    baseline = deepcopy(groups)
    calls = []
    store = RadarStore(tmp_path / "radar.sqlite3")
    equivalent = {HOST + "/go/a": "/curso/a", HOST + "/go/b": "/curso/a"}
    report = investigate_groups(groups, store, config=CONFIG, fetcher=_redirect_fetch(equivalent, calls))
    assert report["metrics"]["eligible"] == 2
    assert report["metrics"]["promotion_proposals"] == 1
    assert report["metrics"]["merge_proposals"] == 1
    assert report["proposals"][0]["ad_ids"] == list("12345")
    assert groups == baseline
    markdown = render_lp_markdown(report)
    assert "INVESTIGAR → CANDIDATO_RADAR" in markdown
    assert "equivalent_strong_identity_same_page" in markdown

    equivalent[HOST + "/go/b"] = "/curso/b"
    store = RadarStore(tmp_path / "other.sqlite3")
    report = investigate_groups(groups, store, config=CONFIG, fetcher=_redirect_fetch(equivalent, []))
    assert report["metrics"]["promotion_proposals"] == 0
    assert report["metrics"]["merge_proposals"] == 0
    assert len(report["proposals"]) == 2


def test_gate_respects_multicard_conflicts_window_and_distinct_pages(tmp_path):
    store = RadarStore(tmp_path / "radar.sqlite3")
    groups = [
        _group("a", "123", HOST + "/", page="A"),
        _group("b", "45", HOST + "/", page="B"),
        _group("c", "67890", HOST + "/", conflict=("multiplos_destinos_no_mesmo_anuncio",)),
        _group("d", "67890", HOST + "/", age="FALHA"),
        _group("e", "67890", HOST + "/", status="CANDIDATO_RADAR"),
    ]
    calls = []
    report = investigate_groups(groups, store, config=CONFIG, fetcher=_redirect_fetch({HOST + "/": "/curso"}, calls))
    assert calls == [HOST + "/"]
    assert report["metrics"]["eligible"] == 1
    assert report["metrics"]["promotion_proposals"] == 0


@pytest.mark.parametrize("reason", ["timeout", "http_403", "http_429", "http_503"])
def test_http_failure_keeps_status_and_history(tmp_path, reason):
    store = RadarStore(tmp_path / "radar.sqlite3")
    group = _group("a", "12345", HOST + "/")
    with store.connect() as db:
        db.execute("INSERT INTO groups VALUES ('a', 'APROVADO', 'old', 'old')")

    def fetch(url, *, before_request, **_):
        before_request("offer.example.com")
        raise LPFetchError(reason, chain=(url,), status=int(reason[5:]) if reason.startswith("http_") else None)

    report = investigate_groups([group], store, config=CONFIG, fetcher=fetch)
    assert report["cards"][0]["status_after"] == "INVESTIGAR"
    assert report["cards"][0]["enrichment_status"] == "UNRESOLVED"
    assert report["metrics"]["promotion_proposals"] == 0
    with store.connect() as db:
        assert db.execute("SELECT review_status FROM groups").fetchone()[0] == "APROVADO"
        assert not db.execute("SELECT * FROM incremental_events").fetchall()


def test_cache_and_expiration(tmp_path):
    store = RadarStore(tmp_path / "radar.sqlite3")
    group = _group("a", "12345", HOST + "/")
    calls = []
    fetch = _redirect_fetch({HOST + "/": "/curso"}, calls)
    first = investigate_groups([group], store, config=CONFIG, fetcher=fetch)
    second = investigate_groups([group], store, config=CONFIG, fetcher=fetch)
    assert calls == [HOST + "/"]
    assert second["metrics"]["cache_hits"] == 1
    assert first["proposals"] == second["proposals"]
    assert store.read_lp_cache(HOST + "/curso", 0, 1)["redirect_chain"] == [HOST + "/curso"]
    with store.connect() as db:
        db.execute("UPDATE lp_fetch_cache SET fetched_epoch=0")
    investigate_groups([group], store, config=CONFIG, fetcher=fetch)
    assert len(calls) == 2


def test_circuit_breaker_skips_second_http_request(tmp_path):
    store = RadarStore(tmp_path / "radar.sqlite3")
    groups = [_group("a", "123", HOST + "/go/1"), _group("b", "456", HOST + "/go/2")]
    calls = []

    def fetch(url, *, before_request, **_):
        before_request("offer.example.com")
        calls.append(url)
        raise LPFetchError("http_429", chain=(url,), status=429)

    report = investigate_groups(groups, store, config=CONFIG, fetcher=fetch)
    assert len(calls) == 1
    assert report["metrics"]["hosts_blocked"] == ["offer.example.com"]
    assert report["metrics"]["errors_by_url"] == {"http_429": 1, "host_circuit_open": 1}


def test_informational_path_and_invalid_html_fail_conservatively():
    result = extract_evidence(_response("/privacy-policy"), HOST + "/")
    assert result["level"] == "SUPPORTING"
    with pytest.raises(LPFetchError, match="invalid_html"):
        extract_evidence(_response("/produto", b"garbage"), HOST + "/")


def test_expired_success_is_preserved_in_history_when_revalidation_fails(tmp_path):
    import json

    store = RadarStore(tmp_path / "radar.sqlite3")
    group = _group("a", "12345", HOST + "/")
    first = investigate_groups([group], store, config=CONFIG, fetcher=_redirect_fetch({HOST + "/": "/curso"}, []))
    assert first["metrics"]["promotion_proposals"] == 1
    with store.connect() as db:
        db.execute("UPDATE lp_fetch_cache SET fetched_epoch=0")

    def timeout(*args, **kwargs):
        raise LPFetchError("timeout")

    second = investigate_groups([group], store, config=CONFIG, fetcher=timeout)
    assert second["metrics"]["promotion_proposals"] == 0
    with store.connect() as db:
        history = db.execute(
            "SELECT result_json FROM lp_fetch_history WHERE requested_url=? ORDER BY fetched_epoch", (HOST + "/",)
        ).fetchall()
    assert [json.loads(r["result_json"])["level"] for r in history] == ["STRONG", "UNRESOLVED"]


@pytest.mark.parametrize("keyword_count", [10, 106])
def test_replay_cli_preserves_coverage_and_v2_review_state(tmp_path, monkeypatch, keyword_count):
    import json
    import sys
    from datetime import datetime, timezone

    from meta_ads_collector.models import Ad, AdCreative, PageInfo
    from radar import cli
    from radar.cycle import get_cycle, run_cycle, start_cycle
    from radar.models import RadarConfig
    from radar.registry import FERNANDO_SOURCE, KeywordEntry

    store = RadarStore(tmp_path / "radar.sqlite3")
    plan = [KeywordEntry(str(i), str(i), FERNANDO_SOURCE, "test") for i in range(keyword_count)]
    config = RadarConfig(interval_seconds=0, backoff_seconds=0)
    records = [
        Ad(
            id=str(i),
            page=PageInfo(id="1", name="Loja"),
            is_active=True,
            delivery_start_time=datetime.now(timezone.utc),
            creatives=[AdCreative(title="Oferta", link_url=HOST + "/")],
        )
        for i in range(5)
    ]
    created = start_cycle(plan, store, config)
    cycle, report = run_cycle(store, created.cycle_id, plan, config, search=lambda *_: records)
    assert "lp_enrichment" not in report.collection
    group_id = report.groups[0].group_id
    store.set_review_status(group_id, "ACOMPANHAR")
    store.acknowledge(cycle.run_ids[-1], group_id)

    def mock_investigate(groups, store, **options):
        def blocked(url, **kwargs):
            raise LPFetchError("http_403", chain=(url,), status=403)

        return investigate_groups(groups, store, **options, fetcher=blocked)

    monkeypatch.setattr(cli, "investigate_groups", mock_investigate)
    output = tmp_path / "report.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "meta-ads-radar",
            "--cycle-action",
            "replay",
            "--cycle-id",
            cycle.cycle_id,
            "--database",
            str(store.path),
            "--lp-enrichment",
            "--json-output",
            str(output),
        ],
    )
    assert cli.main() == 0
    exported = json.loads(output.read_text(encoding="utf-8"))
    assert exported["coverage"] == get_cycle(store, cycle.cycle_id).coverage_kind == cycle.coverage_kind
    assert exported["cards"][0]["status_after"] == "INVESTIGAR"
    assert not store.review_queue(cycle.run_ids[-1])
    assert all(event.event_type != "LEFT_RADAR" for event in store.events())
    with store.connect() as db:
        assert db.execute("SELECT review_status FROM groups").fetchone()[0] == "ACOMPANHAR"
