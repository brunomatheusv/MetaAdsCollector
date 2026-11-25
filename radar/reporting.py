"""Radar JSON and human-readable report rendering."""

from __future__ import annotations

import json

from .models import CollectionCycle, IncrementalEvent, RadarReport, ReviewQueueItem


def render_json(report: RadarReport) -> str:
    return json.dumps(report.to_dict(), ensure_ascii=False, indent=2)


def _identity_lines(group: dict) -> list[str]:
    lines = [f"- Grupo: {group['group_id']} (V{group.get('grouping_version', 1)})"]
    for key, label in (
        ("confidence_reasons", "Evidencias de identidade"),
        ("conflicting_evidence", "Evidencias conflitantes"),
        ("noise_flags", "Sinais de ruido"),
        ("legacy_group_ids", "Grupos V1 relacionados"),
    ):
        if values := group.get(key):
            lines.append(f"- {label}: {', '.join(values)}")
    if separation := group.get("separation_level"):
        lines.append(f"- Evidencia de separacao: {separation}")
    if migration := group.get("grouping_migration"):
        lines.append(f"- Correspondencia V1: {migration}")
    if first := group.get("first_observed_at"):
        lines.append(f"- Primeira/ultima observacao: {first} / {group.get('last_observed_at')}")
    if context := group.get("coverage_context"):
        lines.append(f"- Ciclo / cobertura: {context.get('cycle_id')} / {context.get('kind')}")
        if "current_related_ads_count" in context:
            lines.append(f"- IDs observados no ciclo atual: {context['current_related_ads_count']}")
    return lines


def render_markdown(report: RadarReport) -> str:
    lines = ["# Relatorio Radar Low-Ticket — Fase 4B", "", f"> {report.disclaimer}", ""]
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
            *_identity_lines(vars(group)),
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
    if enrichment := report.collection.get("lp_enrichment"):
        lines.extend(["", render_lp_markdown(enrichment)])
    return "\n".join(lines)


def render_events_json(events: list[IncrementalEvent]) -> str:
    return json.dumps([event.to_dict() for event in events], ensure_ascii=False, indent=2)


def render_queue_json(items: list[ReviewQueueItem]) -> str:
    return json.dumps([item.to_dict() for item in items], ensure_ascii=False, indent=2)


def render_queue_markdown(items: list[ReviewQueueItem]) -> str:
    lines = ["# Fila de revisao incremental — Fase 4B", "",
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
            *_identity_lines(group),
            f"- Anunciante/page_id: {group.get('advertiser') or 'desconhecido'} / {group.get('page_id') or 'ausente'}",
            f"- Criativos unicos estimados: {group.get('unique_creatives_estimate')}",
            f"- Idade em dias: {', '.join(str(age) for age in group.get('ages_days', []) if age is not None)}",
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
        payload["groups"] = report.to_dict()["groups"]
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
    if report is not None:
        lines += ["", render_markdown(report)]
    return "\n".join(lines)


def render_lp_markdown(investigation: dict) -> str:
    metrics = investigation["metrics"]
    lines = [
        "## Investigação seletiva de LP", "",
        f"- Grupos elegíveis / ignorados: {metrics['eligible']} / {metrics['skipped']}",
        f"- URLs inspecionadas / cache persistente / reuso no mesmo ciclo: "
        f"{metrics['unique_inspections']} / {metrics['cache_hits']} / {metrics['shared_urls_saved']}",
        f"- Evidência por grupo: {metrics['levels']}",
        f"- Promoções / consolidações propostas: {metrics['promotion_proposals']} / {metrics['merge_proposals']}",
        f"- Erros por URL: {metrics['errors_by_url']}",
        "",
    ]
    for card in investigation["cards"]:
        if card["status_before"] != "INVESTIGAR":
            continue
        lines.extend([
            f"### Grupo {card['group_id']}",
            "",
            f"- Elegível: {card['eligible']}. Motivos: "
            f"{', '.join(card['reasons'] or card['skipped_reasons'])}",
            f"- Evidência: {card['enrichment_status']}. Fonte: {card['source']}",
            f"- Status: {card['status_before']} → {card['status_after']}",
            f"- Confiança: {card['confidence_before']} → {card['confidence_after']}",
            f"- Grupo proposto: {', '.join(card['grouping_after'])}",
        ])
        if lp := card["lp"]:
            for key in ("requested_url", "final_url", "canonical_url"):
                if value := lp.get(key):
                    lines.append(f"- {key}: `{value.replace(chr(96), '%60')}`")
            lines.append(f"- Redirecionamentos: {max(0, len(lp['redirect_chain']) - 1)}")
            lines.append(f"- Evidência e conflitos: {', '.join(lp['reasons'] + lp['conflicts'])}")
        if card["why_changed"]:
            lines.append(f"- Motivo da alteração proposta: {', '.join(card['why_changed'])}")
        lines.append("")
    return "\n".join(lines)
