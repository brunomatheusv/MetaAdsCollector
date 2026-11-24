"""Small command-line interface for controlled Radar runs."""

from __future__ import annotations

import argparse
from pathlib import Path

from .batch import run_batch
from .cycle import get_cycle, run_cycle, start_cycle
from .models import RadarConfig
from .registry import load_fernando_registry
from .reporting import (
    render_cycle_json,
    render_cycle_markdown,
    render_events_json,
    render_json,
    render_markdown,
    render_queue_json,
    render_queue_markdown,
)
from .storage import RadarStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Radar Low-Ticket Fase 4A recorrente")
    parser.add_argument("--cycle-action", choices=["start", "run", "resume", "status", "report", "queue"],
                        help="contrato manual de ciclo; ausente preserva a CLI da Fase 3")
    parser.add_argument("--cycle-id", help="identificador retornado por --cycle-action start")
    parser.add_argument("--registry", type=Path, required=True, help="JSON canonico com as 106 keywords Fernando")
    parser.add_argument("--gate", type=int, choices=[10, 25, 50, 106], required=True)
    parser.add_argument("--database", type=Path, default=Path("radar.sqlite3"))
    parser.add_argument("--country", default="BR", choices=["BR"])
    parser.add_argument("--status", default="active", choices=["active"])
    parser.add_argument("--limit", type=int, default=20, help="limite conservador por palavra-chave")
    parser.add_argument("--max-age-days", type=int, default=60)
    parser.add_argument("--min-related-ads", type=int, default=5)
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--backoff", type=float, default=5.0)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    parser.add_argument("--events-output", type=Path, help="exporta eventos incrementais JSON")
    parser.add_argument("--queue-json-output", type=Path, help="exporta fila pendente JSON")
    parser.add_argument("--queue-markdown-output", type=Path, help="exporta fila pendente Markdown")
    parser.add_argument("--since-run-id", type=int, help="exporta eventos/fila posteriores a este run_id")
    parser.add_argument("--ack-group", action="append", default=[], metavar="GROUP_ID",
                        help="confirma itens deste grupo na execucao atual sem alterar review_status")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config = RadarConfig(
        args.country, args.status, args.limit, args.max_age_days, args.min_related_ads,
        args.batch_size, args.interval, args.backoff,
    )
    entries = load_fernando_registry(args.registry)[:args.gate]
    store = RadarStore(args.database)
    if args.cycle_action:
        if args.cycle_action == "start":
            cycle = start_cycle(entries, store, config)
            print(render_cycle_markdown(cycle))
            return 0
        if not args.cycle_id:
            raise SystemExit("--cycle-id is required for this cycle action")
        if args.cycle_action in {"run", "resume"}:
            cycle, report = run_cycle(store, args.cycle_id, entries, config)
            run_id = cycle.run_ids[-1]
            for group_id in args.ack_group:
                store.acknowledge(run_id, group_id)
            events, queue = store.events(run_id), store.review_queue(run_id)
            summary = render_cycle_markdown(cycle, report, events, queue)
            if args.json_output:
                args.json_output.write_text(render_cycle_json(cycle, report, events, queue) + "\n", encoding="utf-8")
            if args.markdown_output:
                args.markdown_output.write_text(summary + "\n", encoding="utf-8")
            print(summary)
            return 0
        cycle = get_cycle(store, args.cycle_id)
        if args.cycle_action in {"status", "report"}:
            print(render_cycle_markdown(cycle))
        else:
            if args.ack_group and cycle.run_ids:
                for group_id in args.ack_group:
                    store.acknowledge(cycle.run_ids[-1], group_id)
            print(render_queue_markdown(store.review_queue()))
        return 0

    run_id, report, _ = run_batch(entries, store, config)
    if args.json_output:
        args.json_output.write_text(render_json(report) + "\n", encoding="utf-8")
    summary = render_markdown(report)
    if args.markdown_output:
        args.markdown_output.write_text(summary + "\n", encoding="utf-8")
    for group_id in args.ack_group:
        store.acknowledge(run_id, group_id)
    events = store.events_since(args.since_run_id) if args.since_run_id is not None else store.events(run_id)
    queue = store.review_queue_since(args.since_run_id) if args.since_run_id is not None else store.review_queue(run_id)
    if args.events_output:
        args.events_output.write_text(render_events_json(events) + "\n", encoding="utf-8")
    if args.queue_json_output:
        args.queue_json_output.write_text(render_queue_json(queue) + "\n", encoding="utf-8")
    if args.queue_markdown_output:
        args.queue_markdown_output.write_text(render_queue_markdown(queue) + "\n", encoding="utf-8")
    print(summary)
    print("\n" + render_queue_markdown(queue))
    return 0
