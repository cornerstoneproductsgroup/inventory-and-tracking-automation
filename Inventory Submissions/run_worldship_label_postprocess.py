"""CLI: merge today's WorldShip label PDFs into z- Daily Vendor Orders (DFC default)."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Merge today's per-PO WorldShip label PDFs into resized daily vendor "
            "label files (default retailer: DFC)."
        )
    )
    parser.add_argument(
        "--date",
        metavar="YYYY-MM-DD",
        help="Treat PDFs modified on this date as today's batch (default: today).",
    )
    args = parser.parse_args()

    order_date: date | None = None
    if args.date:
        raw = args.date.strip()
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
            try:
                from datetime import datetime

                order_date = datetime.strptime(raw, fmt).date()
                break
            except ValueError:
                continue
        if order_date is None:
            print(f"[worldship/labels] ERROR: invalid --date {raw!r}", flush=True)
            return 1

    from automation.worldship_batch_import import _build_label_destination
    from automation.worldship_cornerstone_master import load_cornerstone_orders
    from automation.worldship_label_postprocess import postprocess_worldship_labels
    from automation.worldship_label_work_plan import partition_worldship_label_rows
    from automation.worldship_vendor_map import VendorMapRegistry

    orders = load_cornerstone_orders()
    vendor_maps = VendorMapRegistry()
    plan = partition_worldship_label_rows(
        orders, vendor_maps, build_destination=_build_label_destination
    )
    n = postprocess_worldship_labels(plan, vendor_maps, order_date=order_date)
    print(f"[worldship/labels] Done — {n} merged vendor file(s).", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
