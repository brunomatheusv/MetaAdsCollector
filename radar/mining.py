"""Single-command, restartable operation using the existing Radar cycle."""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from collections import Counter
from pathlib import Path

from .cycle import _run_cycle, get_cycle, saved_cycle_report, start_cycle
from .lp import LPConfig, investigate_groups
from .models import RadarConfig
from .operations import LOGGER, collection_lock, diagnostic, write_reports
from .registry import load_fernando_registry
from .reporting import render_cycle_json, render_cycle_markdown
from .shortlist import build_shortlist, offer_counts, render_shortlist
from .storage import RadarStore

REGISTRY = "fernando-oliveira-pro-106.json"
REPORTS = ("shortlist.md", "relatorio.md", "relatorio.json")
KNOWN_FAILURES = {"rate_limit", "access_blocked", "transient_transport", "incomplete_search", "search_error"}


def _report_files(workspace: Path, cycle_id: str) -> tuple[Path, ...]:
    if not re.fullmatch(r"[0-9a-f]{32}", cycle_id):
        raise ValueError("ID de ciclo inválido")
    directory = workspace / "relatorios" / cycle_id
    paths = tuple(directory / name for name in REPORTS)
    if not directory.resolve().is_relative_to(workspace) or any(path.resolve() != path for path in paths):
        raise ValueError("diretório ou arquivo de relatório redirecionado")
    return paths


def _has_final_export(workspace, cycle) -> bool:
    *documents, manifest = _report_files(workspace, cycle.cycle_id)
    if not all(path.is_file() for path in (*documents, manifest)):
        return False
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return False
    return isinstance(data, dict) and (data.get("cycle_id"), data.get("coverage_kind"), data.get("run_ids")) == (
        cycle.cycle_id,
        cycle.coverage_kind,
        cycle.run_ids,
    )


def _cycle_events(store, cycle):
    return [event for run_id in cycle.run_ids for event in store.events(run_id)]


def _export(workspace, store, cycle, report, limit):
    events = _cycle_events(store, cycle)
    pending = store.review_queue()
    shortlist = build_shortlist(report, events, limit=limit, review_status=store.review_statuses())
    short_md = render_shortlist(cycle, shortlist, limit=limit)
    keyword_rows = []
    for row in store.cycle_keywords(cycle.cycle_id):
        error = row["error"]
        keyword_rows.append(
            {
                **{
                    key: row[key]
                    for key in ("keyword_id", "keyword_text", "status", "raw_count", "attempts", "updated_at")
                },
                "error": error if error in KNOWN_FAILURES else "historical_error" if error else None,
            }
        )
    payload = json.loads(render_cycle_json(cycle, report, events, pending))
    payload.update(
        {
            "shortlist_limit": limit,
            "shortlist": shortlist,
            "offers": offer_counts(report),
            "keywords": keyword_rows,
            "incremental_events": [event.to_dict() for event in events],
            "pending_review": [item.to_dict() for item in pending],
        }
    )
    event_counts = Counter(event.event_type for event in events)
    event_labels = ", ".join(f"{key}={value}" for key, value in sorted(event_counts.items())) or "nenhum"
    monitoring = [
        "## Monitoramento incremental",
        "",
        f"- Eventos no ciclo: {len(events)} ({event_labels})",
        f"- Itens de revisão pendentes no histórico: {len(pending)}",
        "- A fila completa, os eventos e a situação das 106 keywords constam no JSON.",
        "",
    ]
    tech_md = render_cycle_markdown(cycle, report, events, pending)
    short_path, full_path, json_path = _report_files(workspace, cycle.cycle_id)
    short_path.parent.mkdir(parents=True, exist_ok=True)
    write_reports(
        [
            (short_path, short_md),
            (full_path, short_md + "\n" + "\n".join(monitoring) + "\n" + tech_md + "\n"),
            (json_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n"),
        ]
    )
    diagnostic(
        "mining_report_written", cycle_id=cycle.cycle_id, shortlisted=len(shortlist), pending_review=len(pending)
    )
    return short_path, len(shortlist)


def mine_once(
    workspace: Path,
    store: RadarStore,
    *,
    limit: int = 5,
    report_cycle: str | None = None,
    lp_enrichment: bool = False,
    search=None,
    sleep=time.sleep,
) -> tuple:
    with collection_lock(store.path):
        if report_cycle:
            cycle = get_cycle(store, report_cycle)
            report, _ = saved_cycle_report(store, cycle)
        else:
            entries = load_fernando_registry(Path(__file__).parent / "data" / REGISTRY)
            config = RadarConfig()
            previous = store.latest_managed_cycle()
            cycle = get_cycle(store, previous) if previous else None
            if cycle and cycle.status == "COMPLETED" and not _has_final_export(workspace, cycle):
                report, _ = saved_cycle_report(store, cycle)
            else:
                if cycle is None or cycle.status == "COMPLETED":
                    cycle = start_cycle(entries, store, config, managed=True)
                diagnostic("mining_cycle_selected", cycle_id=cycle.cycle_id, previous_status=cycle.status)
                cycle, report = _run_cycle(store, cycle.cycle_id, entries, config, search, None, sleep)
            if lp_enrichment:
                report.collection["lp_enrichment"] = investigate_groups(
                    report.to_dict()["groups"],
                    store,
                    candidate_min=config.min_related_ads,
                    config=LPConfig(),
                )
        path, shortlisted = _export(workspace, store, cycle, report, limit)
        return cycle, path, shortlisted


def _console(message: str, stream=None) -> None:
    stream = sys.stdout if stream is None else stream
    encoding = stream.encoding or "utf-8"
    print(message.encode(encoding, errors="backslashreplace").decode(encoding), file=stream, flush=True)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="meta-ads-radar mine", description="Mineração e acompanhamento Radar.")
    parser.add_argument("--workspace", type=Path, default=Path("radar-work"), help="pasta local do SQLite e relatórios")
    parser.add_argument("--shortlist", type=int, default=5, choices=range(1, 11), metavar="1-10")
    parser.add_argument("--every-hours", type=int, metavar="HORAS", help="intervalo de monitoramento em primeiro plano")
    parser.add_argument("--report-cycle", metavar="ID", help="reexporta um ciclo salvo sem rede")
    parser.add_argument("--lp-enrichment", action="store_true", help="investigação opcional de landing pages")
    args = parser.parse_args(argv)
    if args.every_hours is not None and not 1 <= args.every_hours <= 168:
        parser.error("--every-hours deve estar entre 1 e 168")
    if args.report_cycle and (args.every_hours is not None or args.lp_enrichment):
        parser.error("--report-cycle é uma exportação local sem rede")
    workspace = args.workspace.resolve()
    database = workspace / "radar.sqlite3"
    if args.report_cycle and not database.is_file():
        parser.error("--report-cycle precisa do SQLite existente na pasta informada")
    workspace.mkdir(parents=True, exist_ok=True)
    log_path = workspace / "radar-operacao.jsonl"
    if log_path.is_symlink() or (log_path.is_file() and database.is_file() and log_path.samefile(database)):
        parser.error("o caminho do log precisa ser um arquivo separado do SQLite")
    handler = logging.FileHandler(log_path, encoding="utf-8")
    prior_level = LOGGER.level
    LOGGER.setLevel(logging.INFO)
    LOGGER.addHandler(handler)
    try:
        store = RadarStore(database)
        while True:
            cycle, shortlist, count = mine_once(
                workspace,
                store,
                limit=args.shortlist,
                report_cycle=args.report_cycle,
                lp_enrichment=args.lp_enrichment,
            )
            _console(
                f"{cycle.coverage_kind} {cycle.succeeded}/{cycle.total_keywords_expected} · "
                f"{count} na shortlist · {shortlist}"
            )
            if args.every_hours is None:
                return 0 if args.report_cycle or cycle.coverage_kind == "FULL_COVERAGE" else 2
            time.sleep(args.every_hours * 3600)
    except KeyboardInterrupt:
        diagnostic("mining_interrupted")
        _console("Mineração interrompida. Execute o mesmo comando para retomar.", sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError) as exc:
        diagnostic("mining_failed", exception=type(exc).__name__)
        _console(f"Radar interrompido · {exc}", sys.stderr)
        return 1
    finally:
        LOGGER.removeHandler(handler)
        LOGGER.setLevel(prior_level)
        handler.close()
