"""Collection adapter, deterministic grouping, and Radar evidence rules."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timezone

from meta_ads_collector import Ad, MetaAdsCollector

from .models import EvidenceGroup, NormalizedAd, RadarConfig, RadarReport
from .normalize import normalize_ad


def _group_key(ad: NormalizedAd) -> tuple[str, ...]:
    if ad.page_id and not ad.offer_ambiguous:
        if ad.destination_kind == "SPECIFIC_PATH" and ad.destination_key:
            return ("destination", ad.page_id, ad.destination_key)
        if ad.text_key and ad.creative_fingerprint:
            return ("creative", ad.page_id, ad.destination_key or "", ad.text_key, ad.creative_fingerprint)
    return ("individual", ad.ad_id)


def _evaluate(
    key: tuple[str, ...], items: list[NormalizedAd], config: RadarConfig, *, multiple_offers: bool
) -> EvidenceGroup:
    first = items[0]
    conflicts = sorted(set().union(*(item.conflicting_evidence for item in items)))
    signals = ["mesmo_page_id"] if first.page_id and len(items) > 1 else []
    flags = set()
    if first.destination_kind == "MISSING":
        flags.add("sem_destino_utilizavel")
    elif first.destination_kind != "SPECIFIC_PATH":
        flags.add("destino_generico_ou_redirecionador")
    if multiple_offers:
        flags.add("anunciante_com_destinos_de_oferta_distintos")
    if conflicts:
        flags.add("sinais_de_identidade_conflitantes")
    if any(item.offer_ambiguous for item in items):
        flags.add("destinos_inconsistentes_no_anuncio")
    if key[0] == "individual":
        flags.add("evidencia_de_oferta_insuficiente")
        if first.page_id and not first.destination_key and not first.text_key:
            flags.add("page_id_sem_corroboracao")
    if key[0] == "destination":
        signals += ["mesma_url_destino_canonica", "mesmo_dominio"] if len(items) > 1 else ["path_de_destino_especifico"]
    elif key[0] == "creative":
        signals += ["mesmo_texto_normalizado", "mesmo_fingerprint_criativo"]
    signals.extend(conflicts)
    if multiple_offers:
        signals.append("destinos_especificos_distintos_mantidos_separados")
    if not signals:
        signals.append("sem_sinal_estavel_de_oferta")

    if conflicts or key[0] == "individual":
        confidence, evidence = "BAIXA", "AMBIGUOUS"
    elif key[0] == "creative":
        confidence, evidence = "MEDIA", "PROBABLE_SAME_OFFER"
    else:
        confidence, evidence = "ALTA", "STRONG_SAME_OFFER"

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
        if any(item.offer_ambiguous for item in items):
            reasons.append("destinos inconsistentes no anuncio; ofertas nao foram fundidas")
        else:
            reasons.append("agrupamento sem sinal estavel suficiente")
    if criterion_count != "PASSA":
        reasons.append(f"grupo tem {related_count} anuncios relacionados; minimo e {config.min_related_ads}")
    if criterion_age == "INCERTO":
        reasons.append("data de inicio ausente; idade nao foi inventada")
    elif criterion_age == "FALHA":
        reasons.append(f"ao menos um anuncio excede {config.max_age_days} dias")

    active_confirmed = all(item.is_active is True for item in items)
    if confidence == "ALTA" and active_confirmed and criterion_count == criterion_age == "PASSA":
        status = "CANDIDATO_RADAR"
        reasons.append("evidencias coletadas atendem aos filtros do MVP")
    elif criterion_age == "INCERTO" or not active_confirmed:
        status = "EVIDENCIA_INSUFICIENTE"
    elif confidence != "ALTA":
        status = "INVESTIGAR"
    else:
        status = "REJEITADO"

    if confidence == "MEDIA":
        reasons.append("criativo e texto coincidem, mas falta destino especifico corroborado")
    raw_key = json.dumps((2, *key), ensure_ascii=False, separators=(",", ":"))
    destinations = {destination for item in items for destination in item.destinations}
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
        destination_urls=sorted({url for url, _, _ in destinations}),
        destination_domains=sorted({domain for _, domain, _ in destinations}),
        grouping_signals=signals,
        grouping_confidence=confidence,
        criterion_5_plus=criterion_count,
        criterion_max_age=criterion_age,
        final_status=status,
        reasons=reasons,
        evidence_level=evidence,
        separation_level="STRONG_DIFFERENT_OFFER" if multiple_offers and key[0] == "destination" else None,
        confidence_reasons=signals.copy(),
        conflicting_evidence=conflicts,
        noise_flags=sorted(flags),
        destination_fingerprints=sorted({fingerprint for _, _, fingerprint in destinations}),
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
        previous = by_ad_id.setdefault(normalized.ad_id, normalized)
        if previous is not normalized:
            previous.keywords.update(keywords)
            previous.destinations = tuple(sorted(set(previous.destinations) | set(normalized.destinations)))
            previous.conflicting_evidence.update(normalized.conflicting_evidence)
            if previous.destination_key != normalized.destination_key:
                previous.conflicting_evidence.add("destinos_divergentes_para_o_mesmo_ad_id")
            previous.offer_ambiguous = bool(previous.conflicting_evidence)
    grouped: dict[tuple[str, ...], list[NormalizedAd]] = defaultdict(list)
    page_destinations: dict[str, set[str]] = defaultdict(set)
    collations: dict[tuple[str, str], list[NormalizedAd]] = defaultdict(list)
    for ad in sorted(by_ad_id.values(), key=lambda item: item.ad_id):
        grouped[_group_key(ad)].append(ad)
        if ad.page_id and ad.destination_kind == "SPECIFIC_PATH" and not ad.offer_ambiguous and ad.destination_key:
            page_destinations[ad.page_id].add(ad.destination_key)
            if collation := ad.stable_ids.get("collation_id"):
                collations[ad.page_id, collation].append(ad)
    for ads in collations.values():
        if len({ad.destination_key for ad in ads}) > 1:
            for ad in ads:
                ad.conflicting_evidence.add("collation_com_destinos_especificos_distintos")
    groups = [
        _evaluate(key, items, config, multiple_offers=len(page_destinations.get(items[0].page_id or "", ())) > 1)
        for key, items in grouped.items()
    ]
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
