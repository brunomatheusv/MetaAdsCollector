"""Single-command, restartable operation using the existing Radar cycle."""

from __future__ import annotations

import argparse
import json
import logging
import math
import re
import sys
import time
from collections import Counter
from pathlib import Path

from .commercial import DEFAULT_CEILING_BRL, PRICE_PRIORITY_POINTS, classify, render_commercial
from .commercial_auto import CommercialInspection
from .cycle import _run_cycle, get_cycle, saved_cycle_report, start_cycle
from .lp import LPConfig, investigate_groups
from .models import RadarConfig
from .operations import LOGGER, collection_lock, diagnostic, write_reports
from .registry import load_fernando_registry
from .reporting import render_cycle_json, render_cycle_markdown
from .shortlist import build_shortlist, offer_counts, render_shortlist
from .storage import RadarStore

REGISTRY = "fernando-oliveira-pro-106.json"
REPORTS = ("shortlist.md", "shortlist-tecnica.md", "relatorio.md", "relatorio.json")
KNOWN_FAILURES = {"rate_limit", "access_blocked", "transient_transport", "incomplete_search", "search_error"}
REVIEW_PENDING = "PENDING"
REVIEW_FINALIZED = "FINALIZED"


def _report_files(workspace: Path, cycle_id: str) -> tuple[Path, ...]:
    if not re.fullmatch(r"[0-9a-f]{32}", cycle_id):
        raise ValueError("ID de ciclo inválido")
    directory = workspace / "relatorios" / cycle_id
    paths = tuple(directory / name for name in REPORTS)
    if not directory.resolve().is_relative_to(workspace) or any(path.resolve() != path for path in paths):
        raise ValueError("diretório ou arquivo de relatório redirecionado")
    return paths


def _saved_export(workspace, cycle) -> dict | None:
    *documents, manifest = _report_files(workspace, cycle.cycle_id)
    if not all(path.is_file() for path in (*documents, manifest)):
        return None
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    if isinstance(data, dict) and (data.get("cycle_id"), data.get("coverage_kind"), data.get("run_ids")) == (
        cycle.cycle_id,
        cycle.coverage_kind,
        cycle.run_ids,
    ):
        return {key: data.get(key) for key in ("commercial_review_state", "commercial_ceiling_brl")}
    return None


def _cycle_events(store, cycle):
    return [event for run_id in cycle.run_ids for event in store.events(run_id)]


def _daily_signature(row):
    return [row[k] for k in ("name", "price", "currency", "format", "promise", "checkout")]


def _daily_shortlist(workspace, cycle, assessed, limit, *, record):
    path = workspace / "historico-shortlist.json"
    if path.resolve() != path:
        raise ValueError("arquivo de histórico comercial redirecionado")
    history = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    last = history.setdefault("last", {})
    daily = history.setdefault("daily", {})
    qualified = {row["group_id"]: row for row in assessed if row["qualified"]}
    if cycle.cycle_id in daily and (record or daily[cycle.cycle_id]):
        chosen = [qualified[key] for key in daily[cycle.cycle_id] if key in qualified]
    else:
        ranked = sorted(
            qualified.values(),
            key=lambda row: (
                row["group_id"] in last,
                -PRICE_PRIORITY_POINTS.get(row.get("price_priority"), -1),
                -row["priority"],
                row["rank"],
            ),
        )
        chosen = []
        for row in ranked:
            previous = last.get(row["group_id"])
            if (
                record
                and previous
                and (previous["signature"] == _daily_signature(row) and previous["observed_ads"] >= row["observed_ads"])
            ):
                continue
            chosen.append(row)
            if len(chosen) == limit:
                break
        if record and cycle.coverage_kind == "FULL_COVERAGE":
            daily[cycle.cycle_id] = [row["group_id"] for row in chosen]
            for row in chosen:
                last[row["group_id"]] = {
                    "observed_ads": row["observed_ads"],
                    "signature": _daily_signature(row),
                }
            write_reports([(path, json.dumps(history, ensure_ascii=False, indent=2) + "\n")])
    return [{**row, "rank": index} for index, row in enumerate(chosen[:limit], 1)]


def _automatic_review(workspace, cycle, ranked, ceiling):
    path = _report_files(workspace, cycle.cycle_id)[0].with_name("evidencia-automatica.json")
    if path.resolve() != path:
        raise ValueError("arquivo de evidência redirecionado")
    stored = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    if ceiling is None or (stored.get("runs") == cycle.run_ids and stored.get("ceiling_brl") == ceiling):
        return stored if stored.get("runs") == cycle.run_ids else {}
    inspection = CommercialInspection(ceiling)
    assessed = {row["group_id"]: inspection.inspect(row) for row in ranked[:12] if row["destinations"]}
    result = {
        "runs": cycle.run_ids,
        "ceiling_brl": ceiling,
        "groups": assessed,
        "http_requests": inspection.requests,
        "errors": inspection.errors,
        "hosts_paused": sorted(inspection.blocked),
        "technical_groups_not_inspected": max(0, len(ranked) - 12),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    write_reports([(path, json.dumps(result, ensure_ascii=False, indent=2) + "\n")])
    return result


def _export(
    workspace, store, cycle, report, limit, *, ceiling=None, saved_ceiling=None, record=False, review_state=None,
):
    events = _cycle_events(store, cycle)
    pending = store.review_queue()
    ranked = build_shortlist(report, events, limit=len(report.groups), review_status=store.review_statuses())
    automatic = _automatic_review(workspace, cycle, ranked, ceiling)
    effective_ceiling = ceiling or automatic.get("ceiling_brl") or saved_ceiling or DEFAULT_CEILING_BRL
    assessed = classify(
        workspace,
        store,
        cycle,
        ranked,
        automatic=automatic.get("groups"),
        ceiling=effective_ceiling,
    )
    shortlist = _daily_shortlist(workspace, cycle, assessed, min(limit, 5), record=record)
    technical = ranked[:limit]
    short_md = render_commercial(shortlist, cycle, shortlist=True)
    if review_state == REVIEW_PENDING:
        short_md = "> Prévia. A verificação comercial deste ciclo está pendente.\n\n" + short_md
    technical_md = render_shortlist(cycle, technical, limit=limit)
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
            "shortlist_limit": min(limit, 5),
            "technical_shortlist_limit": limit,
            "shortlist": shortlist,
            "technical_shortlist": technical,
            "commercial_assessments": assessed,
            "commercial_ceiling_brl": effective_ceiling,
            "commercial_inspection": {key: value for key, value in automatic.items() if key != "groups"},
            "offers": offer_counts(report),
            "keywords": keyword_rows,
            "incremental_events": [event.to_dict() for event in events],
            "pending_review": [item.to_dict() for item in pending],
        }
    )
    if review_state:
        payload["commercial_review_state"] = review_state
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
    short_path, technical_path, full_path, json_path = _report_files(workspace, cycle.cycle_id)
    short_path.parent.mkdir(parents=True, exist_ok=True)
    write_reports(
        [
            (short_path, short_md),
            (technical_path, technical_md),
            (
                full_path,
                short_md
                + "\n"
                + render_commercial(assessed, cycle)
                + "\n"
                + "\n".join(monitoring)
                + "\n"
                + tech_md
                + "\n",
            ),
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
    commercial_ceiling: float | None = None,
    assisted_review: bool = False,
    search=None,
    sleep=time.sleep,
) -> tuple:
    with collection_lock(store.path):
        previous = report_cycle or store.latest_managed_cycle()
        cycle = get_cycle(store, previous) if previous else None
        saved = _saved_export(workspace, cycle) if cycle else None
        review_state = saved["commercial_review_state"] if saved else None
        saved_ceiling = saved["commercial_ceiling_brl"] if saved else None
        if report_cycle:
            if assisted_review:
                if cycle.coverage_kind != "FULL_COVERAGE" or review_state not in (REVIEW_PENDING, REVIEW_FINALIZED):
                    raise ValueError("Para finalizar a revisão, prepare primeiro um ciclo FULL com --assisted-review.")
                review_state = REVIEW_FINALIZED
            report, _ = saved_cycle_report(store, cycle)
        else:
            if review_state == REVIEW_PENDING:
                commercial_ceiling = saved_ceiling
            if cycle and cycle.status == "COMPLETED" and (not saved or review_state == REVIEW_PENDING):
                report, _ = saved_cycle_report(store, cycle)
            else:
                entries = load_fernando_registry(Path(__file__).parent / "data" / REGISTRY)
                config = RadarConfig()
                if cycle is None or cycle.status == "COMPLETED":
                    cycle = start_cycle(entries, store, config, managed=True)
                    review_state = None
                    saved_ceiling = None
                diagnostic("mining_cycle_selected", cycle_id=cycle.cycle_id, previous_status=cycle.status)
                cycle, report = _run_cycle(store, cycle.cycle_id, entries, config, search, None, sleep)
            if assisted_review:
                review_state = REVIEW_PENDING
            if lp_enrichment:
                report.collection["lp_enrichment"] = investigate_groups(
                    report.to_dict()["groups"],
                    store,
                    candidate_min=config.min_related_ads,
                    config=LPConfig(),
                )
        path, shortlisted = _export(
            workspace,
            store,
            cycle,
            report,
            limit,
            ceiling=commercial_ceiling,
            saved_ceiling=saved_ceiling,
            record=review_state != REVIEW_PENDING and (report_cycle is None or assisted_review),
            review_state=review_state,
        )
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
    parser.add_argument(
        "--assisted-review", action="store_true",
        help="prepara a verificação comercial; com --report-cycle, fecha a shortlist sem nova coleta",
    )
    parser.add_argument("--low-ticket-max-brl", type=float, help="teto em reais para verificar ofertas novas")
    args = parser.parse_args(argv)
    ceiling = args.low_ticket_max_brl
    if ceiling is None and not args.report_cycle:
        ceiling = DEFAULT_CEILING_BRL
    if ceiling is not None and (not math.isfinite(ceiling) or ceiling <= 0):
        parser.error("--low-ticket-max-brl exige um valor finito e positivo")
    if args.every_hours is not None and not 1 <= args.every_hours <= 168:
        parser.error("--every-hours deve estar entre 1 e 168")
    if args.report_cycle and (args.every_hours is not None or args.lp_enrichment or ceiling is not None):
        parser.error("--report-cycle é uma exportação local sem rede")
    if args.assisted_review and args.every_hours is not None:
        parser.error("--assisted-review exige execução sob demanda")
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
                commercial_ceiling=ceiling,
                assisted_review=args.assisted_review,
            )
            _console(
                f"{cycle.coverage_kind} {cycle.succeeded}/{cycle.total_keywords_expected} · "
                f"{'prévia: ' if args.assisted_review and not args.report_cycle else ''}"
                f"{count} qualificadas no Brasil · {shortlist}"
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
