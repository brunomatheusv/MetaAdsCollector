"""Radar JSON and human-readable report rendering."""

from __future__ import annotations

import json

from .models import RadarReport


def render_json(report: RadarReport) -> str:
    return json.dumps(report.to_dict(), ensure_ascii=False, indent=2)


def render_markdown(report: RadarReport) -> str:
    lines = ["# Relatorio Radar Low-Ticket — Fase 2", "", f"> {report.disclaimer}", ""]
    lines += [
        f"- Observacoes coletadas: {report.collection['observations']}",
        f"- IDs de anuncio unicos: {report.collection['unique_ad_ids']}",
        f"- Grupos de oferta: {report.collection['groups']}",
        "",
    ]
    for group in report.groups:
        lines += [
            f"## {group.offer_name or 'Oferta nao identificada'} — {group.final_status} / {group.review_status}",
            "",
            f"- Anunciante/page_id: {group.advertiser or 'desconhecido'} / {group.page_id or 'ausente'}",
            f"- Anuncios relacionados: {group.related_ads_count} (observacoes no grupo: {group.total_collected})",
            "- Criativos unicos/duplicados estimados: "
            f"{group.unique_creatives_estimate} / {group.duplicate_creatives_estimate}",
            f"- Confianca: {group.grouping_confidence}; sinais: {', '.join(group.grouping_signals)}",
            f"- Criterios 5+ / idade: {group.criterion_5_plus} / {group.criterion_max_age}",
            "- Primeira/ultima data de inicio: "
            f"{group.first_start_date or 'ausente'} / {group.last_start_date or 'ausente'}",
            f"- Palavras-chave: {', '.join(group.keywords)}",
            f"- Destinos: {', '.join(group.destination_urls) or 'ausentes'}",
            f"- Dominios: {', '.join(group.destination_domains) or 'ausentes'}",
            f"- IDs para revisao: {', '.join(group.ad_ids)}",
            f"- Motivos: {'; '.join(group.reasons)}",
            "",
        ]
    return "\n".join(lines)
