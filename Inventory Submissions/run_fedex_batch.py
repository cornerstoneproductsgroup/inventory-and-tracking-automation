"""CLI: FedEx batch shipping — upload Lowe's CSV, finalize, save labels by SKU/vendor."""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_DEFAULT_CONFIG = _HERE / "fedex_batch.json"
if not _DEFAULT_CONFIG.is_file():
    _DEFAULT_CONFIG = _HERE / "fedex_batch.example.json"


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
            "FedEx batch shipping: log in, upload Lowe's order CSV, finalize shipments, "
            "save labels to vendor folders using SKU map."
        )
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=_DEFAULT_CONFIG,
        help=f"FedEx UI config JSON (default: {_DEFAULT_CONFIG.name})",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=None,
        help="Lowe's CSV to upload (default: today's Pull Orders file or FEDEX_LOWES_CSV_PATH).",
    )
    parser.add_argument("--date", metavar="DATE", default=None, help="Order date for default CSV name.")
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="Only load CSV and print SKU/vendor label plan (no browser).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build plan only; do not open FedEx (same as --plan-only for browser).",
    )
    parser.add_argument(
        "--skip-upload",
        action="store_true",
        help="Skip CSV upload (still logs in); open today's batch already in FedEx.",
    )
    parser.add_argument(
        "--check-credentials",
        action="store_true",
        help="Verify FEDEX_USERNAME and FEDEX_PASSWORD in .env (no browser).",
    )
    args = parser.parse_args()

    os.chdir(_HERE)
    if str(_HERE) not in sys.path:
        sys.path.insert(0, str(_HERE))

    try:
        from dotenv import load_dotenv

        load_dotenv(_HERE / ".env")
    except ImportError:
        pass

    if not args.config.is_file():
        print(f"ERROR: Config not found: {args.config}")
        return 1

    order_date: date | None = None
    if args.date:
        try:
            order_date = _parse_date(args.date)
        except ValueError as exc:
            print(f"ERROR: {exc}")
            return 1

    from automation.fedex_batch_shipping import run_fedex_batch
    from automation.fedex_credentials import env_file_path, load_fedex_credentials
    from automation.fedex_lowes_csv import LowesCsvSkip

    if args.check_credentials:
        try:
            import json

            cfg = json.loads(args.config.read_text(encoding="utf-8"))
            creds = load_fedex_credentials(cfg)
            print(f"OK: FedEx credentials found for {creds.username!r}")
            print(f"    Loaded from {env_file_path()}")
            return 0
        except Exception as exc:
            print(f"ERROR: {exc}")
            return 1

    try:
        return run_fedex_batch(
            config_path=args.config.resolve(),
            order_date=order_date,
            csv_path=args.csv.resolve() if args.csv else None,
            plan_only=bool(args.plan_only or args.dry_run),
            skip_upload=args.skip_upload,
            dry_run=args.dry_run,
        )
    except LowesCsvSkip as skip:
        print(f"[fedex] Skipping: newest file {skip.top_filename!r} is not today's Lowe's Output.")
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
