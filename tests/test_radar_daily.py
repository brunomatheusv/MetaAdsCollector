from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from radar.commercial_auto import CommercialInspection
from radar.lp_http import PageResponse
from radar.mining import _daily_shortlist

LP = "https://site.example.com/curso"
CHECKOUT = "https://pay.example.com/checkout"


def _page(url, *, price=37, sku="ABC", country="BR", recurring=False):
    offer = {
        "@type": "Offer",
        "price": price,
        "priceCurrency": "BRL",
        "availability": "https://schema.org/InStock",
        "url": CHECKOUT,
        "seller": {"address": {"addressCountry": country}},
    }
    if recurring:
        offer["billingDuration"] = "P1M"
    data = {
        "@type": "Course",
        "courseMode": "online",
        "name": "Curso ABC",
        "sku": sku,
        "category": "Costura",
        "description": "Aprenda a montar suas peças",
        "offers": offer,
    }
    text = '<html lang="pt-BR"><h1>Curso ABC</h1><script type="application/ld+json">'
    return PageResponse(url, 200, {}, (text + json.dumps(data) + "</script></html>").encode(), (url,))


def _check(ceiling=50, **kwargs):
    def fetch(url, *, before_request, **options):
        before_request("site.example.com" if url == LP else "pay.example.com")
        return _page(url, **kwargs) if url == CHECKOUT else _page(url)

    return CommercialInspection(ceiling, fetch=fetch, sleep=lambda _: None).inspect({"destinations": [LP]})


def test_same_product_br_and_price_are_proven_by_two_pages():
    result = _check()
    assert result["category"] == "INFOPRODUTO_LOW_TICKET_BR"
    assert result["price"] == 37
    assert result["sources"] == [LP, CHECKOUT]
    assert _check(ceiling=30)["category"] == "INDETERMINADO"


@pytest.mark.parametrize("details", [{"sku": "DIFFERENT"}, {"price": 38}, {"recurring": True}])
def test_checkout_conflicts_fail_closed(details):
    assert _check(**details).get("category") != "INFOPRODUTO_LOW_TICKET_BR"


def test_conservative_without_a_price_ceiling():
    assert _check(ceiling=None)["category"] == "INDETERMINADO"


def test_daily_shortlist_prioritizes_novelty_and_reexports_idempotently(tmp_path):
    def cycle(id):
        return SimpleNamespace(cycle_id=id, coverage_kind="FULL_COVERAGE")

    def group(id, count, points):
        return {
            "group_id": id,
            "observed_ads": count,
            "priority": points,
            "qualified": True,
            "rank": 1,
            "name": id,
            "price": 37,
            "currency": "BRL",
            "format": "E-book",
            "promise": "Aprender",
            "checkout": CHECKOUT,
        }

    old = group("old", 10, 10)
    assert _daily_shortlist(tmp_path, cycle("first"), [old], 5, record=True)[0]["group_id"] == "old"
    assert not _daily_shortlist(tmp_path, cycle("second"), [old], 5, record=True)
    new = group("new", 5, 7)
    updated = group("old", 11, 10)
    for record in (True, False):
        ranked = _daily_shortlist(tmp_path, cycle("third"), [updated, new], 5, record=record)
        assert [row["group_id"] for row in ranked] == ["new", "old"]


@pytest.mark.parametrize(
    ("amount", "band"),
    [
        (9.99, "SECUNDARIA"),
        (10, "MAXIMA"),
        (27.90, "MAXIMA"),
        (27.91, "ALTA"),
        (37, "ALTA"),
        (37.01, "MEDIA"),
        (47, "MEDIA"),
    ],
)
def test_daily_price_band_boundaries(amount, band):
    from radar.commercial import price_priority

    assert price_priority(amount) == band


def test_default_ceiling_is_configurable():
    from radar.commercial import DEFAULT_CEILING_BRL

    assert DEFAULT_CEILING_BRL == 47
    assert _check(ceiling=DEFAULT_CEILING_BRL)["category"] == "INFOPRODUTO_LOW_TICKET_BR"
    assert _check(ceiling=36)["category"] == "INDETERMINADO"


def test_price_band_orders_qualified_offers_without_promoting_unknowns(tmp_path):
    cycle = SimpleNamespace(cycle_id="same-novelty", coverage_kind="FULL_COVERAGE")
    offers = [
        {"group_id": "secondary", "qualified": True, "priority": 10, "price_priority": "SECUNDARIA", "rank": 1},
        {"group_id": "media", "qualified": True, "priority": 9, "price_priority": "MEDIA", "rank": 2},
        {"group_id": "max", "qualified": True, "priority": 7, "price_priority": "MAXIMA", "rank": 3},
        {"group_id": "high", "qualified": True, "priority": 8, "price_priority": "ALTA", "rank": 4},
        {"group_id": "unknown", "qualified": False, "priority": None, "price_priority": None, "rank": 5},
    ]
    rows = _daily_shortlist(tmp_path, cycle, offers, 5, record=False)
    assert [row["group_id"] for row in rows] == ["max", "high", "media", "secondary"]
