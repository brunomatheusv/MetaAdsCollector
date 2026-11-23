from __future__ import annotations

from datetime import datetime, timedelta, timezone

from meta_ads_collector import Ad, AdCreative
from meta_ads_collector.models import PageInfo
from radar.models import RadarConfig
from radar.pipeline import analyze_ads

NOW = datetime(2026, 11, 23, tzinfo=timezone.utc)


def make_ad(
    number: int,
    *,
    page_id: str = "page-1",
    url: str | None = "https://loja.example/produto",
    title: str | None = "Produto A",
    days: int | None = 10,
    body: str | None = None,
    active: bool | None = True,
) -> Ad:
    return Ad(
        id=f"ad-{number}",
        page=PageInfo(id=page_id, name=f"Anunciante {page_id}"),
        is_active=active,
        ad_status="ACTIVE" if active else None,
        delivery_start_time=NOW - timedelta(days=days) if days is not None else None,
        creatives=[AdCreative(title=title, body=body or f"Criativo {number}", link_url=url)],
    )


def report_for(ads: list[Ad]):
    return analyze_ads([(ad, {"keyword"}) for ad in ads], RadarConfig(), now=NOW)


def test_a_seven_recent_ads_same_offer_are_candidate():
    group = report_for([make_ad(i, days=i) for i in range(1, 8)]).groups[0]
    assert group.related_ads_count == 7
    assert group.criterion_5_plus == "PASSA"
    assert group.criterion_max_age == "PASSA"
    assert group.final_status == "CANDIDATO_RADAR"


def test_b_twelve_page_ads_split_into_three_offers_do_not_pass():
    ads = [make_ad(offer * 10 + i, url=f"https://loja.example/oferta-{offer}") for offer in range(3) for i in range(4)]
    groups = report_for(ads).groups
    assert len(groups) == 3
    assert all(group.related_ads_count == 4 for group in groups)
    assert all(group.final_status == "REJEITADO" for group in groups)


def test_c_old_ads_fail_time_rule():
    group = report_for([make_ad(i, days=61 + i) for i in range(6)]).groups[0]
    assert group.criterion_5_plus == "PASSA"
    assert group.criterion_max_age == "FALHA"
    assert group.final_status == "REJEITADO"


def test_d_missing_dates_are_insufficient_and_never_filled():
    group = report_for([make_ad(i, days=None) for i in range(6)]).groups[0]
    assert group.start_dates == [None] * 6
    assert group.ages_days == [None] * 6
    assert group.criterion_max_age == "INCERTO"
    assert group.final_status == "EVIDENCIA_INSUFICIENTE"


def test_e_duplicate_creatives_are_reported_separately_from_ads():
    ads = [make_ad(i, body="Mesmo criativo") for i in range(4)] + [make_ad(10, body="Criativo B")]
    group = report_for(ads).groups[0]
    assert group.related_ads_count == 5
    assert group.unique_creatives_estimate == 2
    assert group.duplicate_creatives_estimate == 3


def test_f_same_advertiser_different_urls_are_not_merged():
    report = report_for(
        [
            make_ad(1, url="https://loja.example/produto-a", title="A"),
            make_ad(2, url="https://loja.example/produto-b", title="B"),
        ]
    )
    assert len(report.groups) == 2
    assert {tuple(group.destination_urls) for group in report.groups} == {
        ("https://loja.example/produto-a",),
        ("https://loja.example/produto-b",),
    }


def test_g_ambiguous_group_is_investigate_not_candidate():
    ad = make_ad(1, url=None, title=None)
    ad.creatives = [AdCreative()]
    group = report_for([ad]).groups[0]
    assert group.grouping_confidence == "BAIXA"
    assert group.final_status == "INVESTIGAR"


def test_same_ad_found_by_two_keywords_counts_once_and_keeps_attribution():
    ad = make_ad(1)
    report = analyze_ads([(ad, {"kw-a"}), (ad, {"kw-b"})], now=NOW)
    group = report.groups[0]
    assert report.collection == {"observations": 2, "unique_ad_ids": 1, "groups": 1}
    assert group.related_ads_count == 1
    assert group.keywords == ["kw-a", "kw-b"]


def test_tracking_parameters_do_not_split_same_offer():
    ads = [
        make_ad(1, url="https://loja.example/produto?utm_source=meta&variant=blue"),
        make_ad(2, url="https://www.loja.example/produto?variant=blue&fbclid=x"),
    ]
    assert len(report_for(ads).groups) == 1


def test_unknown_active_status_is_never_candidate():
    group = report_for([make_ad(i, active=None) for i in range(6)]).groups[0]
    assert group.final_status == "EVIDENCIA_INSUFICIENTE"


def test_multicard_ad_with_distinct_destinations_is_ambiguous():
    ad = make_ad(1)
    ad.creatives = [
        AdCreative(title="Oferta A", link_url="https://loja.example/a"),
        AdCreative(title="Oferta B", link_url="https://loja.example/b"),
    ]
    group = report_for([ad]).groups[0]
    assert group.grouping_confidence == "BAIXA"
    assert group.final_status == "INVESTIGAR"
    assert "multiplos_destinos_no_mesmo_anuncio" in group.grouping_signals


def test_config_rejects_scope_outside_controlled_mvp():
    try:
        RadarConfig(country="US")
    except ValueError as exc:
        assert "only country=BR" in str(exc)
    else:
        raise AssertionError("expected controlled MVP validation")
