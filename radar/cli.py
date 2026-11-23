"""Small command-line interface for controlled Radar runs."""

from __future__ import annotations

import argparse
from pathlib import Path

from .batch import run_batch
from .models import RadarConfig
from .registry import load_fernando_registry
from .reporting import render_json, render_markdown
from .storage import RadarStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Radar Low-Ticket Fase 2")
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
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config = RadarConfig(
        args.country, args.status, args.limit, args.max_age_days, args.min_related_ads,
        args.batch_size, args.interval, args.backoff,
    )
    entries = load_fernando_registry(args.registry)[:args.gate]
    _, report, _ = run_batch(entries, RadarStore(args.database), config)
    args.json_output.write_text(render_json(report) + "\n", encoding="utf-8")
    summary = render_markdown(report)
    if args.markdown_output:
        args.markdown_output.write_text(summary + "\n", encoding="utf-8")
    print(summary)
    return 0
