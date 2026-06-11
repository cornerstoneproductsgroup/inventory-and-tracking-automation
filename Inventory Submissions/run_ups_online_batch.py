"""CLI: UPS.com batch file shipping — upload CSV, process batch, save labels."""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_DEFAULT_CONFIG = _HERE / "ups_batch.json"
if not _DEFAULT_CONFIG.is_file():
    _DEFAULT_CONFIG = _HERE / "ups_batch.example.json"

_LANE_HELP = "depot (Home Depot), thdso (Depot Special Order), tractor (Tractor Supply), or all"


def _parse_date(raw: str) -> date:
    text = (raw or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            from datetime import datetime

            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Invalid date {raw!r}; use YYYY-MM-DD or MM/DD/YYYY.")


def _run_lane(
    *,
    lane: str,
    args: argparse.Namespace,
    order_date: date | None,
) -> int:
    from automation.ups_lane_csv import UpsCsvSkip, resolve_upload_csv
    from automation.ups_online_batch_shipping import UpsBatchError, run_ups_batch

    if not args.skip_upload:
        try:
            csv_path = resolve_upload_csv(
                lane=lane,
                order_date=order_date,
                explicit_path=args.csv,
            )
            print(f"[ups] [{lane}] CSV: {csv_path}", flush=True)
        except UpsCsvSkip as exc:
            print(
                f"[ups] [{lane}] SKIP: no CSV for today (newest: {exc.top_filename})",
                flush=True,
            )
            return 0
        except FileNotFoundError as exc:
            print(f"[ups] [{lane}] ERROR: {exc}", flush=True)
            return 1

    try:
        result = run_ups_batch(
            lane=lane,
            config_path=args.config,
            csv_path=args.csv,
            order_date=order_date,
            manual_login=args.manual_login,
            skip_upload=args.skip_upload,
        )
    except UpsBatchError as exc:
        print(f"[ups] [{lane}] ERROR: {exc}", flush=True)
        return 1
    except Exception as exc:
        import traceback

        print(f"[ups] [{lane}] ERROR: {exc}", flush=True)
        traceback.print_exc()
        return 1

    print(f"[ups] [{lane}] Done — labels saved to {result.labels_path}", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "UPS.com batch shipping: log in, upload today's Order Splitter CSV, "
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
        "--lane",
        choices=("depot", "thdso", "tractor", "all"),
        default="depot",
        help=f"Retailer lane to run ({_LANE_HELP}). Default: depot.",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=None,
        help="CSV to upload (default: today's Order Splitter file for the lane).",
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
    parser.add_argument(
        "--setup-login",
        action="store_true",
        help=(
            "One-time setup: open dedicated ups_browser_profile, log into UPS manually, "
            "save session (use UPS_BROWSER_MODE=dedicated afterward)."
        ),
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

    if args.setup_login:
        from automation.ups_online_batch_shipping import run_ups_browser_setup

        if not args.config.is_file():
            print(f"ERROR: config not found: {args.config}", flush=True)
            return 1
        try:
            run_ups_browser_setup(config_path=args.config)
        except Exception as exc:
            print(f"[ups] ERROR: {exc}", flush=True)
            return 1
        return 0

    order_date = _parse_date(args.date) if args.date else None

    if args.lane == "all":
        from automation.ups_batch_config import UPS_BATCH_LANE_ORDER

        exit_code = 0
        for lane in UPS_BATCH_LANE_ORDER:
            print(f"\n[ups] === Lane: {lane} ===", flush=True)
            exit_code = max(exit_code, _run_lane(lane=lane, args=args, order_date=order_date))
        return exit_code

    return _run_lane(lane=args.lane, args=args, order_date=order_date)


if __name__ == "__main__":
    raise SystemExit(main())
