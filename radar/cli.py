"""Small command-line interface for controlled Radar runs."""

from __future__ import annotations

import argparse
import logging
import sys
from json import dumps
from pathlib import Path

from .batch import run_batch
from .cycle import get_cycle, run_cycle, saved_cycle_report, start_cycle
from .lp import LPConfig, investigate_groups
from .models import RadarConfig
from .operations import LOGGER, collection_lock, diagnostic, write_reports
from .registry import load_fernando_registry
from .reporting import (
    render_cycle_json,
    render_cycle_markdown,
    render_events_json,
    render_json,
    render_lp_markdown,
    render_markdown,
    render_queue_json,
    render_queue_markdown,
)
from .storage import RadarStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Radar Low-Ticket Fase 4A recorrente")
    parser.add_argument("--cycle-action", choices=["start", "run", "resume", "status", "report", "queue", "replay"],
                        help="contrato manual de ciclo; ausente preserva a CLI da Fase 3")
    parser.add_argument("--cycle-id", help="identificador retornado por --cycle-action start")
    parser.add_argument("--registry", type=Path, help="JSON canonico com as 106 keywords Fernando")
    parser.add_argument("--gate", type=int, choices=[10, 25, 50, 106])
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
    parser.add_argument("--lp-enrichment", action="store_true", help="investigação seletiva e propostas por LP")
    parser.add_argument("--lp-min-ids", type=int, default=3)
    parser.add_argument("--lp-ttl-hours", type=float, default=24)
    parser.add_argument("--lp-timeout", type=float, default=8)
    parser.add_argument("--lp-max-redirects", type=int, default=3)
    parser.add_argument("--lp-max-bytes", type=int, default=384 * 1024)
    parser.add_argument("--lp-max-urls", type=int, default=40)
    parser.add_argument("--log-json", type=Path, help="diagnóstico operacional em JSON Lines")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    outputs = (args.json_output, args.markdown_output, args.events_output,
               args.queue_json_output, args.queue_markdown_output, args.log_json)
    paths = [path.resolve() for path in outputs if path is not None]
    db = args.database.resolve()
    protected = {db, *(Path(str(db) + suffix) for suffix in ("-wal", "-shm", "-journal", ".lock.sqlite3"))}
    if args.registry:
        protected.add(args.registry.resolve())
    if len(paths) != len(set(paths)) or protected.intersection(paths):
        parser.error("output paths must be distinct and cannot overwrite the database or registry")
    handler = logging.FileHandler(args.log_json, encoding="utf-8") if args.log_json else None
    level = LOGGER.level
    if handler:
        LOGGER.setLevel(logging.INFO)
        LOGGER.addHandler(handler)
    try:
        return _main(parser, args)
    except KeyboardInterrupt:
        diagnostic("execution_interrupted", cycle_id=args.cycle_id)
        _print_report("Execução interrompida. Use resume com o mesmo ciclo para continuar.", stream=sys.stderr)
        return 130
    except Exception as exc:
        diagnostic("execution_failed", cycle_id=args.cycle_id, exception=type(exc).__name__)
        raise
    finally:
        if handler:
            LOGGER.removeHandler(handler)
            handler.close()
            LOGGER.setLevel(level)


def _main(parser, args) -> int:
    lp_config = None
    if args.lp_enrichment:
        try:
            lp_config = LPConfig(
                min_ids=args.lp_min_ids, ttl_hours=args.lp_ttl_hours, timeout=args.lp_timeout,
                max_redirects=args.lp_max_redirects, max_bytes=args.lp_max_bytes, max_urls=args.lp_max_urls,
            )
        except ValueError as exc:
            parser.error(str(exc))
    if args.cycle_action == "replay":
        if not args.cycle_id or not lp_config or not args.database.is_file():
            parser.error("replay requires --cycle-id, --lp-enrichment and an existing --database copy")
        return _replay(args, lp_config)
    if args.cycle_action in {"status", "report", "queue"} and (not args.cycle_id or not args.database.is_file()):
        parser.error("this action requires --cycle-id and an existing --database")
    needs_plan = args.cycle_action not in {"status", "report", "queue"}
    if needs_plan and (args.registry is None or args.gate is None):
        parser.error("--registry and --gate are required")
    config = RadarConfig(
        args.country, args.status, args.limit, args.max_age_days, args.min_related_ads,
        args.batch_size, args.interval, args.backoff,
    )
    entries = load_fernando_registry(args.registry)[:args.gate] if needs_plan else []
    store = RadarStore(args.database)
    if args.cycle_action:
        if args.cycle_action == "start":
            cycle = start_cycle(entries, store, config)
            _print_report(render_cycle_markdown(cycle))
            return 0
        if not args.cycle_id:
            raise SystemExit("--cycle-id is required for this cycle action")
        if args.cycle_action in {"run", "resume"}:
            cycle, report = run_cycle(store, args.cycle_id, entries, config)
            run_id = cycle.run_ids[-1]
            for group_id in args.ack_group:
                store.acknowledge(run_id, group_id)
            if lp_config:
                _enrich(report, store, lp_config, config.min_related_ads)
            events, queue = store.events(run_id), store.review_queue(run_id)
            summary = render_cycle_markdown(cycle, report, events, queue)
            _write_cycle_reports(args, cycle, report, events, queue, summary)
            _print_report(summary)
            return 0
        cycle = get_cycle(store, args.cycle_id)
        if args.cycle_action == "report":
            report, run_id = saved_cycle_report(store, cycle)
            events, queue = store.events(run_id), store.review_queue(run_id)
            summary = render_cycle_markdown(cycle, report, events, queue)
            _write_cycle_reports(args, cycle, report, events, queue, summary)
            _print_report(summary)
        elif args.cycle_action == "status":
            _print_report(render_cycle_markdown(cycle))
        else:
            if args.ack_group and cycle.run_ids:
                for group_id in args.ack_group:
                    store.acknowledge(cycle.run_ids[-1], group_id)
            _print_report(render_queue_markdown(store.review_queue()))
        return 0

    run_id, report, _ = run_batch(entries, store, config)
    if lp_config:
        _enrich(report, store, lp_config, config.min_related_ads)
    summary = render_markdown(report)
    for group_id in args.ack_group:
        store.acknowledge(run_id, group_id)
    events = store.events_since(args.since_run_id) if args.since_run_id is not None else store.events(run_id)
    queue = store.review_queue_since(args.since_run_id) if args.since_run_id is not None else store.review_queue(run_id)
    write_reports([
        (args.json_output, render_json(report) + "\n" if args.json_output else ""),
        (args.markdown_output, summary + "\n"),
        (args.events_output, render_events_json(events) + "\n" if args.events_output else ""),
        (args.queue_json_output, render_queue_json(queue) + "\n" if args.queue_json_output else ""),
        (args.queue_markdown_output, render_queue_markdown(queue) + "\n" if args.queue_markdown_output else ""),
    ])
    _print_report(summary)
    _print_report("\n" + render_queue_markdown(queue))
    return 0


def _enrich(report, store, config, candidate_min):
    with collection_lock(store.path):
        report.collection["lp_enrichment"] = investigate_groups(
            report.to_dict()["groups"], store, candidate_min=candidate_min, config=config,
        )


def _print_report(summary: str, *, stream=None) -> None:
    stream = stream if stream is not None else sys.stdout
    encoding = stream.encoding or "utf-8"
    print(summary.encode(encoding, errors="backslashreplace").decode(encoding), file=stream)


def _write_cycle_reports(args, cycle, report, events, queue, summary):
    write_reports([
        (args.json_output, render_cycle_json(cycle, report, events, queue) + "\n" if args.json_output else ""),
        (args.markdown_output, summary + "\n"),
        (args.events_output, render_events_json(events) + "\n" if args.events_output else ""),
        (args.queue_json_output, render_queue_json(queue) + "\n" if args.queue_json_output else ""),
        (args.queue_markdown_output, render_queue_markdown(queue) + "\n" if args.queue_markdown_output else ""),
    ])


def _replay(args: argparse.Namespace, config: LPConfig) -> int:
    store = RadarStore(args.database)
    cycle = get_cycle(store, args.cycle_id)
    if not cycle.run_ids or cycle.status not in {"COMPLETED", "PARTIAL"}:
        raise SystemExit("replay requires a completed or partial cycle with a stored snapshot")
    run_id = cycle.run_ids[-1]
    groups = store.run_group_snapshots(run_id)
    with collection_lock(store.path):
        investigation = investigate_groups(groups, store, candidate_min=cycle.config["min_related_ads"], config=config)
    payload = {"cycle_id": cycle.cycle_id, "coverage": cycle.coverage_kind, "run_id": run_id, **investigation}
    summary = "\n".join([
        f"# Replay LP do ciclo {cycle.cycle_id}",
        f"\nCobertura Meta preservada em {cycle.coverage_kind}.",
        render_lp_markdown(investigation),
    ])
    write_reports([
        (args.json_output, dumps(payload, ensure_ascii=False, indent=2) + "\n" if args.json_output else ""),
        (args.markdown_output, summary + "\n"),
    ])
    _print_report(summary)
    return 0
