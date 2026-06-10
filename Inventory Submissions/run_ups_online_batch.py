"""CLI: UPS.com batch file shipping — Home Depot CSV upload, process, save labels."""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_DEFAULT_CONFIG = _HERE / "ups_batch.json"
if not _DEFAULT_CONFIG.is_file():
    _DEFAULT_CONFIG = _HERE / "ups_batch.example.json"


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
            "UPS.com batch shipping (Home Depot): log in, upload today's Depot CSV, "
            "fill ship-from/payment, process batch, save label PDF."
        )
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=_DEFAULT_CONFIG,
        help=f"UPS UI config JSON (default: {_DEFAULT_CONFIG.name})",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=None,
        help="Depot CSV to upload (default: today's Order Splitter file).",
    )
    parser.add_argument("--date", metavar="DATE", default=None, help="Order date for default CSV name.")
    parser.add_argument(
        "--manual-login",
        action="store_true",
        help="Open UPS sign-in; you type credentials; continue when ready.",
    )
    parser.add_argument(
        "--skip-upload",
        action="store_true",
        help="Skip CSV upload (batch form already loaded).",
    )
    parser.add_argument(
        "--check-credentials",
        action="store_true",
        help="Verify UPS_USERNAME and UPS_PASSWORD in .env (no browser).",
    )
    args = parser.parse_args()

    if str(_HERE) not in sys.path:
        sys.path.insert(0, str(_HERE))

    from dotenv import load_dotenv

    load_dotenv(_HERE / ".env", override=False)

    if args.check_credentials:
        from automation.ups_credentials import load_ups_credentials

        try:
            creds = load_ups_credentials()
        except ValueError as exc:
            print(f"ERROR: {exc}", flush=True)
            return 1
        print(f"OK — UPS username configured ({creds.username[:3]}…)", flush=True)
        return 0

    order_date = _parse_date(args.date) if args.date else None

    from automation.ups_depot_csv import DepotCsvSkip, resolve_upload_csv
    from automation.ups_online_batch_shipping import UpsBatchError, run_ups_depot_batch

    if not args.skip_upload:
        try:
            csv_path = resolve_upload_csv(order_date=order_date, explicit_path=args.csv)
            print(f"[ups] CSV: {csv_path}", flush=True)
        except DepotCsvSkip as exc:
            print(f"[ups] SKIP: no Depot CSV for today (newest: {exc.top_filename})", flush=True)
            return 0
        except FileNotFoundError as exc:
            print(f"[ups] ERROR: {exc}", flush=True)
            return 1

    try:
        result = run_ups_depot_batch(
            config_path=args.config,
            csv_path=args.csv,
            order_date=order_date,
            manual_login=args.manual_login,
            skip_upload=args.skip_upload,
        )
    except UpsBatchError as exc:
        print(f"[ups] ERROR: {exc}", flush=True)
        return 1
    except Exception as exc:
        print(f"[ups] ERROR: {exc}", flush=True)
        return 1

    print(
        f"[ups] Done — labels saved to {result.labels_path}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
