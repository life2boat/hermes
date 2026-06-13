#!/usr/bin/env python3
from __future__ import annotations

import argparse

from gateway.memory.analytics import compute_memory_analytics_summary, format_memory_analytics_report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Show Memory OS analytics from SQLite")
    parser.add_argument("--db-path", default=None, help="Path to SQLite database with memory_analytics_logs")
    parser.add_argument("--hours", type=float, default=None, help="Only include analytics from the last N hours")
    parser.add_argument("--user-id", type=int, default=None, help="Limit metrics to one Telegram user_id")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    summary = compute_memory_analytics_summary(args.db_path, user_id=args.user_id, hours=args.hours)
    report = format_memory_analytics_report(summary)
    scope_parts = []
    if args.hours is not None:
        scope_parts.append(f"last {args.hours:g}h")
    if args.user_id is not None:
        scope_parts.append(f"user_id={args.user_id}")
    if scope_parts:
        report += "\nScope: " + ", ".join(scope_parts)
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())