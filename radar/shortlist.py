"""Evidence-only shortlist for the automatic Radar report."""

from __future__ import annotations

import html
from collections import Counter, defaultdict
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

from .models import CollectionCycle, IncrementalEvent, RadarReport


def _destination_link(url: str) -> str | None:
    try:
        parts = urlsplit(url)
        if parts.scheme in {"http", "https"} and parts.hostname and parts.username is None:
            return quote(url, safe=":/?#[]@!$&'*+,;=%")
    except ValueError:
        pass
    return None


def _timestamp(value: str | None) -> float:
    if not value:
        return float("-inf")
    date = datetime.fromisoformat(value)
    return (date if date.tzinfo else date.replace(tzinfo=timezone.utc)).timestamp()


def build_shortlist(
    report: RadarReport, events: list[IncrementalEvent], *, limit: int = 5, review_status: dict[str, str] | None = None
) -> list[dict]:
    """Rank candidates already selected by the Radar using only current-cycle evidence."""
    minimum = report.config["min_related_ads"]
    event_types = defaultdict(set)
    new_ids = defaultdict(set)
    for event in events:
        event_types[event.group_id].add(event.event_type)
        if event.event_type == "NEW_ADS" and event.after and event.before:
            new_ids[event.group_id].update(set(event.after["ad_ids"]) - set(event.before["ad_ids"]))
    reviewed = review_status or {}
    eligible = []
    for group in report.groups:
        history = (group.coverage_context or {}).get("historical_only_ad_ids", [])
        current_ids = sorted(set(group.ad_ids) - set(history))
        human_status = reviewed.get(group.group_id, group.review_status)
        if (
            group.final_status != "CANDIDATO_RADAR"
            or human_status == "DESCARTADO"
            or group.grouping_confidence != "ALTA"
            or group.evidence_level != "STRONG_SAME_OFFER"
            or group.conflicting_evidence
            or group.criterion_5_plus != "PASSA"
            or group.criterion_max_age != "PASSA"
            or len(current_ids) < minimum
        ):
            continue
        fresh_ids = sorted(new_ids[group.group_id].intersection(current_ids))
        links = [
            f"https://www.facebook.com/ads/library/?id={ad_id}"
            for ad_id in current_ids
            if ad_id.isascii() and ad_id.isdecimal()
        ]
        destinations = sorted({link for url in group.destination_urls if (link := _destination_link(url))})
        row = {
            "group_id": group.group_id,
            "offer_name": group.offer_name,
            "advertiser": group.advertiser,
            "page_id": group.page_id,
            "confidence": group.grouping_confidence,
            "review_status": human_status,
            "observed_ads": len(current_ids),
            "ad_ids": current_ids,
            "new_ads_vs_history": len(fresh_ids),
            "first_start_date": group.first_start_date,
            "last_start_date": group.last_start_date,
            "oldest_ad_age_days": max((a for a in group.ages_days if a is not None), default=None),
            "keywords": group.keywords,
            "event_types": sorted(event_types[group.group_id]),
            "ad_links": links,
            "destinations": destinations,
            "destination_domains": group.destination_domains,
            "reasons": group.reasons,
            "identity_evidence": group.confidence_reasons,
        }
        eligible.append(row)
    eligible.sort(
        key=lambda item: (
            -item["observed_ads"],
            -_timestamp(item["last_start_date"]),
            -item["new_ads_vs_history"],
            item["group_id"],
        )
    )
    return [{"rank": rank, **item} for rank, item in enumerate(eligible[:limit], 1)]


def _text(value: str | None) -> str:
    return html.escape(" ".join((value or "Não informado").split())).replace("\\", "\\\\").replace("[", "\\[")


def render_shortlist(cycle: CollectionCycle, shortlist: list[dict], *, limit: int) -> str:
    lines = [
        "# Radar Low-Ticket — shortlist técnica",
        "",
        f"Ciclo `{cycle.cycle_id}` · **{cycle.coverage_kind}** · {cycle.succeeded}/{cycle.total_keywords_expected}",
        "",
        f"Até {limit} ofertas candidatas, em ordem de IDs únicos observados neste ciclo, data de início mais recente "
        "e novos IDs frente ao histórico, com desempate por ID do grupo.",
        "",
        "A ordem prioriza revisão. Vendas, preço e demanda comercial exigem evidência independente.",
        "",
    ]
    if cycle.coverage_kind != "FULL_COVERAGE":
        lines.extend(["**Lista provisória.** A cobertura parcial limita as conclusões sobre presença e ausência.", ""])
    if not shortlist:
        lines.append("Nenhum grupo apresentou evidência suficiente para esta shortlist.")
    for row in shortlist:
        title = row["offer_name"] or next(iter(row["destination_domains"]), None)
        lines.extend(
            [
                f"## {row['rank']}. {_text(title)}",
                "",
                f"- Grupo `{row['group_id']}` · anunciante {_text(row['advertiser'])} · confiança {row['confidence']}",
                f"- {row['observed_ads']} IDs neste ciclo · {row['new_ads_vs_history']} novos IDs frente ao histórico",
                f"- Datas de início de {_text(row['first_start_date'])} a {_text(row['last_start_date'])}",
                f"- Motivo técnico: {_text(', '.join(row['reasons']))}",
                f"- Evidência de identidade: {_text(', '.join(row['identity_evidence']))}",
            ]
        )
        for index, url in enumerate(row["destinations"], 1):
            lines.append(f"- [Destino {index}](<{url}>)")
        if row["ad_links"]:
            lines.append("- " + " · ".join(f"[Anúncio {i}](<{url}>)" for i, url in enumerate(row["ad_links"], 1)))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def offer_counts(report: RadarReport) -> dict:
    return {
        "status": dict(Counter(group.final_status for group in report.groups)),
        "confidence": dict(Counter(group.grouping_confidence for group in report.groups)),
    }
