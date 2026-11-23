"""Small command-line interface for controlled Radar runs."""

from __future__ import annotations

import argparse
from pathlib import Path

from .models import RadarConfig
from .pipeline import collect_and_analyze
from .reporting import render_json, render_markdown


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Radar Low-Ticket MVP Fase 1")
    parser.add_argument("keywords", nargs="+", help="1 a 5 palavras-chave")
    parser.add_argument("--country", default="BR", choices=["BR"])
    parser.add_argument("--status", default="active", choices=["active"])
    parser.add_argument("--limit", type=int, default=20, help="limite conservador por palavra-chave")
    parser.add_argument("--max-age-days", type=int, default=60)
    parser.add_argument("--min-related-ads", type=int, default=5)
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config = RadarConfig(args.country, args.status, args.limit, args.max_age_days, args.min_related_ads)
    report = collect_and_analyze(args.keywords, config)
    args.json_output.write_text(render_json(report) + "\n", encoding="utf-8")
    summary = render_markdown(report)
    if args.markdown_output:
        args.markdown_output.write_text(summary + "\n", encoding="utf-8")
    print(summary)
    return 0
