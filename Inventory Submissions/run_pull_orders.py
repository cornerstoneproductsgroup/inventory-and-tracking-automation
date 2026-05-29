"""CLI: morning pull-orders (CommerceHub PDF/CSV, SPS Tractor/Grainger, warehouse print)."""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _parse_date(raw: str) -> date:
    text = (raw or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            from datetime import datetime

            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Invalid date {raw!r}; use YYYY-MM-DD or MM/DD/YYYY.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Pull morning orders: CommerceHub packing slips + CSVs, "
            "SPS Tractor/Grainger PDF+CSV, then warehouse print PDFs."
        )
    )
    parser.add_argument(
        "--date",
        metavar="DATE",
        default=None,
        help="Order date for file names (default: today). Formats: YYYY-MM-DD or MM/DD/YYYY.",
    )
    parser.add_argument("--skip-commercehub", action="store_true")
    parser.add_argument("--skip-sps", action="store_true")
    parser.add_argument("--skip-warehouse-print", action="store_true")
    parser.add_argument(
        "--skip-warehouse-wait",
        action="store_true",
        help="Do not poll for warehouse PDFs; fail if files are not already present.",
    )
    args = parser.parse_args()

    os.chdir(_HERE)
    if str(_HERE) not in sys.path:
        sys.path.insert(0, str(_HERE))

    order_date: date | None = None
    if args.date:
        try:
            order_date = _parse_date(args.date)
        except ValueError as exc:
            print(f"ERROR: {exc}")
            return 1

    from automation.pull_orders_chain import run_pull_orders

    return run_pull_orders(
        order_date=order_date,
        skip_commercehub=args.skip_commercehub,
        skip_sps=args.skip_sps,
        skip_warehouse_print=args.skip_warehouse_print,
        skip_warehouse_wait=args.skip_warehouse_wait,
    )


if __name__ == "__main__":
    raise SystemExit(main())
