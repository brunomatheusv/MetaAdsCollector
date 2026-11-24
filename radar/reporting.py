"""Radar JSON and human-readable report rendering."""

from __future__ import annotations

import json

from .models import CollectionCycle, IncrementalEvent, RadarReport, ReviewQueueItem


def render_json(report: RadarReport) -> str:
    return json.dumps(report.to_dict(), ensure_ascii=False, indent=2)


def render_markdown(report: RadarReport) -> str:
    lines = ["# Relatorio Radar Low-Ticket — Fase 3", "", f"> {report.disclaimer}", ""]
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


def render_events_json(events: list[IncrementalEvent]) -> str:
    return json.dumps([event.to_dict() for event in events], ensure_ascii=False, indent=2)


def render_queue_json(items: list[ReviewQueueItem]) -> str:
    return json.dumps([item.to_dict() for item in items], ensure_ascii=False, indent=2)


def render_queue_markdown(items: list[ReviewQueueItem]) -> str:
    lines = ["# Fila de revisao incremental — Fase 3", "",
             "> Entrada no Radar indica somente filtros tecnicos; nao e validacao comercial.", ""]
    if not items:
        return "\n".join(lines + ["Nenhuma mudanca material pendente."])
    for item in items:
        group = item.group
        previous = group.get("previous") or {}
        old_count = previous.get("related_ads_count", "novo")
        old_ids = set(previous.get("ad_ids", []))
        new_ids = sorted(set(group.get("ad_ids", [])) - old_ids)
        lines += [
            f"## P{item.priority} · {item.queue_type} · {group.get('offer_name') or item.group_id}", "",
            f"- Anunciante: {group.get('advertiser') or 'desconhecido'}",
            f"- Contagem antes -> agora: {old_count} -> {group.get('related_ads_count')}",
            f"- Novos IDs: {', '.join(new_ids) or 'nenhum'}",
            f"- IDs atuais: {', '.join(group.get('ad_ids', [])) or 'ausentes'}",
            f"- Datas: {group.get('first_start_date') or 'ausente'} -> {group.get('last_start_date') or 'ausente'}",
            f"- Keywords: {', '.join(group.get('keywords', [])) or 'ausentes'}",
            f"- URLs/dominios: {', '.join(group.get('destination_urls', [])) or 'ausentes'} / "
            f"{', '.join(group.get('destination_domains', [])) or 'ausentes'}",
            f"- Confianca/status tecnico/revisao: {group.get('grouping_confidence')} / "
            f"{group.get('final_status')} / {item.review_status}",
            f"- Eventos: {', '.join(item.event_types)}",
            f"- Motivo: {item.reason}", "",
        ]
    return "\n".join(lines)


def render_cycle_json(cycle: CollectionCycle, report: RadarReport | None = None,
                      events: list[IncrementalEvent] | None = None,
                      queue: list[ReviewQueueItem] | None = None) -> str:
    payload = cycle.to_dict()
    payload["plan_scope_warning"] = (
        f"{cycle.coverage_kind} descreve a cobertura do plano; o limite por keyword nao cobre toda a Meta Ads Library."
    )
    if report is not None:
        payload["collection"] = report.collection
    if events is not None:
        counts: dict[str, int] = {}
        for event in events:
            counts[event.event_type] = counts.get(event.event_type, 0) + 1
        payload["events_by_type"] = counts
    if queue is not None:
        payload["queue_pending"] = len(queue)
    return json.dumps(payload, ensure_ascii=False, indent=2)


def render_cycle_markdown(cycle: CollectionCycle, report: RadarReport | None = None,
                          events: list[IncrementalEvent] | None = None,
                          queue: list[ReviewQueueItem] | None = None) -> str:
    lines = [
        f"# Ciclo {cycle.cycle_id}", "",
        f"- Status: {cycle.status}",
        f"- Cobertura: {cycle.coverage_kind}",
        f"- Esperadas / sucesso / falha / vazias / pendentes: {cycle.total_keywords_expected} / "
        f"{cycle.succeeded} / {cycle.failed} / {cycle.empty} / {cycle.pending}",
        f"- Ratio: {cycle.coverage_ratio:.2%}",
        f"- Limit por keyword: {cycle.config['max_results_per_keyword']}",
        f"- Inicio / fim: {cycle.started_at} / {cycle.finished_at or 'em andamento'}",
        f"- Duracao: {cycle.duration_seconds if cycle.duration_seconds is not None else 'em andamento'} s",
        f"- Runs tecnicos: {', '.join(map(str, cycle.run_ids)) or 'nenhum'}", "",
        "> FULL significa todas as keywords do plano oficial processadas com sucesso; "
        "nao significa cobertura total da Meta Ads Library.", "",
    ]
    if report is not None:
        lines += [
            f"- Observacoes brutas: {report.collection.get('observations', 0)}",
            f"- Ads unicos: {report.collection.get('unique_ad_ids', 0)}",
            f"- Grupos: {report.collection.get('groups', 0)}",
            f"- Saidas suprimidas por cobertura: {report.collection.get('suppressed_absence_events', 0)}", "",
        ]
    if events is not None:
        counts: dict[str, int] = {}
        for event in events:
            counts[event.event_type] = counts.get(event.event_type, 0) + 1
        lines.append("- Eventos: " + (", ".join(f"{key}={value}" for key, value in sorted(counts.items())) or "nenhum"))
    if queue is not None:
        lines.append(f"- Fila pendente: {len(queue)}")
    return "\n".join(lines)
