from __future__ import annotations

import hashlib
import json

from meta_ads_collector import AdCreative
from radar.normalize import canonical_url
from radar.pipeline import analyze_ads
from radar.reporting import render_markdown, render_queue_json, render_queue_markdown
from radar.storage import RadarStore
from tests.test_radar import NOW, make_ad, report_for


def test_specific_destinations_group_by_offer_and_deduplicate_keywords():
    ads = [
        make_ad(i, url=f"https://loja.example/curso?sku={1 if i < 3 else 2}&utm_source={i}")
        for i in range(5)
    ]
    records = [(ad, {"kw1"}) for ad in ads] + [(ads[0], {"kw2"})]
    report = analyze_ads(records, now=NOW)
    assert [group.related_ads_count for group in report.groups] == [3, 2]
    assert all(group.criterion_5_plus == "FALHA" for group in report.groups)
    assert all(group.separation_level == "STRONG_DIFFERENT_OFFER" for group in report.groups)
    assert report.groups[0].keywords == ["kw1", "kw2"]
    assert "destinos_especificos_distintos_mantidos_separados" in report.groups[0].confidence_reasons
    ads.extend(make_ad(i, url="http://www.loja.example/curso/?sku=1&gclid=test") for i in range(5, 7))
    group = report_for(ads).groups[0]
    assert group.related_ads_count == 5
    assert group.final_status == "CANDIDATO_RADAR"
    assert group.evidence_level == "STRONG_SAME_OFFER"


def test_url_keeps_functional_values_and_case():
    url, host, fingerprint = canonical_url(
        "HTTPS://WWW.Example.com:443/Produto/?utm_medium=x&fbclid=1&gclid=1&src=checkout&ref=&sku=B&sku=A"
    )
    assert url == "https://example.com/Produto?ref=&sku=B&sku=A&src=checkout"
    assert host == "example.com"
    assert fingerprint == "example.com/Produto?ref=&sku=B&sku=A&src=checkout"
    assert canonical_url("https://example.com:8443/Produto")[2] != fingerprint


def test_page_alone_and_generic_urls_cannot_promote():
    blank = [make_ad(i, url=None) for i in range(5)]
    for ad in blank:
        ad.creatives = []
    groups = report_for(blank).groups
    assert len(groups) == 5
    assert all("page_id_sem_corroboracao" in group.noise_flags for group in groups)
    for url in ("https://example.com/", "https://bit.ly/shared", "https://example.com/redirect/shared"):
        ads = [make_ad(i, url=url, body="Mesmo criativo") for i in range(5)]
        group = report_for(ads).groups[0]
        assert group.related_ads_count == 5
        assert group.grouping_confidence == "MEDIA"
        assert group.final_status == "INVESTIGAR"
        assert "destino_generico_ou_redirecionador" in group.noise_flags


def test_multicard_and_inconsistent_duplicate_keep_all_destinations():
    ad = make_ad(1, url="https://loja.example/a")
    ad.creatives.insert(0, AdCreative(title="Card com destino ausente"))
    assert report_for([ad]).groups[0].destination_urls == ["https://loja.example/a"]
    ad.creatives.append(AdCreative(link_url="https://loja.example/b"))
    group = report_for([ad]).groups[0]
    assert group.final_status == "INVESTIGAR"
    assert group.destination_urls == ["https://loja.example/a", "https://loja.example/b"]
    assert group.conflicting_evidence == ["multiplos_destinos_no_mesmo_anuncio"]

    duplicate = make_ad(1, url="https://loja.example/c")
    group = report_for([ad, duplicate]).groups[0]
    assert group.related_ads_count == 1
    assert "destinos_divergentes_para_o_mesmo_ad_id" in group.conflicting_evidence
    assert len(group.destination_urls) == 3


def test_collation_conflicts_reduce_confidence_without_bridging_paths():
    ads = [make_ad(i) for i in range(5)] + [make_ad(5, url="https://loja.example/other")]
    for ad in ads:
        ad.collation_id = "shared"
    forward = report_for(ads).groups
    backward = report_for(list(reversed(ads))).groups
    assert forward == backward
    assert [group.related_ads_count for group in forward] == [5, 1]
    assert all(group.grouping_confidence == "BAIXA" for group in forward)
    assert all(group.final_status == "INVESTIGAR" for group in forward)
    assert "collation_com_destinos_especificos_distintos" in forward[0].confidence_reasons


def _save_legacy(store, ads):
    report = report_for(ads)
    group = report.groups[0]
    group.group_id = hashlib.sha256(b"page-1|url:loja.example/produto").hexdigest()[:12]
    run_id = store.start_run({})
    store.persist_report(run_id, report)
    with store.connect() as db:
        db.execute(
            "UPDATE group_history SET evidence_json=json_remove(evidence_json, '$.grouping_version') WHERE run_id=?",
            (run_id,),
        )
    store.set_review_status(group.group_id, "ACOMPANHAR")
    store.acknowledge(run_id, group.group_id)
    return run_id, group.group_id


def test_one_to_one_migration_preserves_decision_ack_and_history(tmp_path):
    path = tmp_path / "radar.db"
    ads = [make_ad(i) for i in range(5)]
    old_store = RadarStore(path)
    legacy_run, legacy_id = _save_legacy(old_store, ads)
    with old_store.connect() as db:
        db.execute("DROP TABLE grouping_aliases")
    store = RadarStore(path)
    run = store.start_run({})
    report = report_for(ads)
    store.persist_report(run, report)
    group = report.groups[0]
    assert group.legacy_group_ids == [legacy_id]
    assert group.grouping_migration == "MATCHED"
    assert group.review_status == "ACOMPANHAR"
    assert group.first_observed_at <= group.last_observed_at
    assert store.group_history(group.group_id) == [5, 5]
    assert not store.review_queue(run)
    assert store.review_queue(legacy_run, include_acknowledged=True)[0].acknowledged_at
    store.set_review_status(legacy_id, "APROVADO")
    assert store.review_queue(legacy_run, include_acknowledged=True)[0].review_status == "APROVADO"


def test_split_migration_keeps_old_decision_and_flags_both_successors(tmp_path):
    store = RadarStore(tmp_path / "radar.db")
    ads = [make_ad(i) for i in range(5)]
    legacy_run, legacy_id = _save_legacy(store, ads)
    report = report_for([make_ad(i, url=f"https://loja.example/produto?ref={i < 3}") for i in range(5)])
    run = store.start_run({})
    store.persist_report(run, report)
    assert len(report.groups) == 2
    for group in report.groups:
        assert group.legacy_group_ids == [legacy_id]
        assert group.grouping_migration == "AMBIGUOUS"
        assert group.review_status == "NOVO"
    queue = store.review_queue(run)
    assert all(item.queue_type == "REVISAR_AMBIGUIDADE" for item in queue)
    assert store.review_queue(legacy_run, include_acknowledged=True)[0].review_status == "ACOMPANHAR"
    assert len(queue) == 2
    assert json.loads(render_queue_json(queue))[0]["group"]["grouping_version"] == 2
    assert "Correspondencia V1" in render_markdown(report)
    assert "Evidencias de identidade" in render_queue_markdown(queue)


def test_same_ad_ids_with_a_new_destination_do_not_inherit_decision(tmp_path):
    store = RadarStore(tmp_path / "radar.db")
    _save_legacy(store, [make_ad(i) for i in range(5)])
    report = report_for([make_ad(i, url="https://loja.example/new-product") for i in range(5)])
    store.persist_report(store.start_run({}), report)
    group = report.groups[0]
    assert group.grouping_migration == "AMBIGUOUS"
    assert group.review_status == "NOVO"
