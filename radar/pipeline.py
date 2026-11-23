"""Collection adapter, deterministic grouping, and Radar evidence rules."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timezone

from meta_ads_collector import Ad, MetaAdsCollector

from .models import EvidenceGroup, NormalizedAd, RadarConfig, RadarReport
from .normalize import normalize_ad


def _group_key(ad: NormalizedAd) -> tuple[str, str]:
    page = ad.page_id or f"unknown-page:{ad.advertiser or ad.ad_id}"
    if ad.offer_ambiguous:
        return page, f"ambiguous-multicard:{ad.ad_id}"
    if ad.destination_key:
        return page, f"url:{ad.destination_key}"
    if ad.text_key:
        return page, f"text:{ad.text_key}"
    return page, f"ambiguous:{ad.ad_id}"


def _evaluate(items: list[NormalizedAd], config: RadarConfig) -> EvidenceGroup:
    first = items[0]
    ambiguous = any(item.offer_ambiguous for item in items)
    has_url = not ambiguous and all(
        item.destination_key == first.destination_key and item.destination_key for item in items
    )
    has_text = not ambiguous and all(item.text_key == first.text_key and item.text_key for item in items)
    confidence = "ALTA" if has_url else "MEDIA" if has_text else "BAIXA"
    signals = ["mesmo_page_id"] if first.page_id else ["anunciante_sem_page_id"]
    if has_url:
        signals += ["mesma_url_destino_canonica", "mesmo_dominio"]
    elif has_text:
        signals.append("mesmo_texto_normalizado")
    elif ambiguous:
        signals.append("multiplos_destinos_no_mesmo_anuncio")
    else:
        signals.append("sem_sinal_estavel_de_oferta")

    ad_ids = sorted({item.ad_id for item in items})
    related_count = len(ad_ids)
    criterion_count = "PASSA" if related_count >= config.min_related_ads else "FALHA"
    known_ages = [item.age_days for item in items if item.age_days is not None]
    if len(known_ages) != len(items):
        criterion_age = "INCERTO"
    elif all(0 <= age <= config.max_age_days for age in known_ages):
        criterion_age = "PASSA"
    else:
        criterion_age = "FALHA"

    fingerprints = [item.creative_fingerprint for item in items]
    if all(fingerprints):
        unique_creatives = len(set(fingerprints))
        duplicate_creatives = len(fingerprints) - unique_creatives
    else:
        unique_creatives = duplicate_creatives = None

    reasons: list[str] = []
    if any(item.is_active is not True for item in items):
        reasons.append("status ativo ausente ou nao confirmado em parte das evidencias")
    if confidence == "BAIXA":
        if ambiguous:
            reasons.append("anuncio multi-card contem destinos distintos; ofertas nao foram fundidas")
        else:
            reasons.append("agrupamento sem sinal estavel suficiente")
    if criterion_count != "PASSA":
        reasons.append(f"grupo tem {related_count} anuncios relacionados; minimo e {config.min_related_ads}")
    if criterion_age == "INCERTO":
        reasons.append("data de inicio ausente; idade nao foi inventada")
    elif criterion_age == "FALHA":
        reasons.append(f"ao menos um anuncio excede {config.max_age_days} dias")

    active_confirmed = all(item.is_active is True for item in items)
    if confidence in {"ALTA", "MEDIA"} and active_confirmed and criterion_count == criterion_age == "PASSA":
        status = "CANDIDATO_RADAR"
        reasons.append("evidencias coletadas atendem aos filtros do MVP")
    elif criterion_age == "INCERTO" or not active_confirmed:
        status = "EVIDENCIA_INSUFICIENTE"
    elif confidence == "BAIXA":
        status = "INVESTIGAR"
    else:
        status = "REJEITADO"

    raw_key = "|".join(_group_key(first))
    known_dates = sorted(item.start_time for item in items if item.start_time is not None)
    return EvidenceGroup(
        group_id=hashlib.sha256(raw_key.encode()).hexdigest()[:12],
        keywords=sorted(set().union(*(item.keywords for item in items))),
        page_id=first.page_id,
        advertiser=first.advertiser,
        offer_name=first.offer_name,
        total_collected=len(items),
        related_ads_count=related_count,
        unique_creatives_estimate=unique_creatives,
        duplicate_creatives_estimate=duplicate_creatives,
        ad_ids=ad_ids,
        start_dates=[item.start_time.isoformat() if item.start_time else None for item in items],
        ages_days=[item.age_days for item in items],
        first_start_date=known_dates[0].isoformat() if known_dates else None,
        last_start_date=known_dates[-1].isoformat() if known_dates else None,
        destination_urls=sorted({item.destination_url for item in items if item.destination_url}),
        destination_domains=sorted({item.destination_domain for item in items if item.destination_domain}),
        grouping_signals=signals,
        grouping_confidence=confidence,
        criterion_5_plus=criterion_count,
        criterion_max_age=criterion_age,
        final_status=status,
        reasons=reasons,
    )


def analyze_ads(
    records: Iterable[tuple[Ad, set[str]]],
    config: RadarConfig | None = None,
    *,
    now: datetime | None = None,
) -> RadarReport:
    config = config or RadarConfig()
    now = now or datetime.now(timezone.utc)
    by_ad_id: dict[str, NormalizedAd] = {}
    observations = 0
    for ad, keywords in records:
        observations += 1
        normalized = normalize_ad(ad, keywords, now)
        if normalized.ad_id in by_ad_id:
            by_ad_id[normalized.ad_id].keywords.update(keywords)
        else:
            by_ad_id[normalized.ad_id] = normalized
    grouped: dict[tuple[str, str], list[NormalizedAd]] = defaultdict(list)
    for ad in by_ad_id.values():
        grouped[_group_key(ad)].append(ad)
    groups = [_evaluate(items, config) for items in grouped.values()]
    groups.sort(key=lambda group: (group.final_status != "CANDIDATO_RADAR", -group.related_ads_count, group.group_id))
    return RadarReport.create(
        config,
        {"observations": observations, "unique_ad_ids": len(by_ad_id), "groups": len(groups)},
        groups,
    )


def collect_and_analyze(keywords: list[str], config: RadarConfig | None = None) -> RadarReport:
    config = config or RadarConfig()
    if not keywords:
        raise ValueError("Radar requires at least one keyword")
    records: list[tuple[Ad, set[str]]] = []
    with MetaAdsCollector() as collector:
        for keyword in keywords:
            for ad in collector.search(
                query=keyword,
                country=config.country,
                status=config.status,
                max_results=config.max_results_per_keyword,
            ):
                records.append((ad, {keyword}))
    return analyze_ads(records, config)
