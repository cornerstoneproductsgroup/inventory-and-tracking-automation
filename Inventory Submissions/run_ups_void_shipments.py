"""CLI: UPS.com — void all shipments for a day via Shipping History."""
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
            "UPS.com Shipping History: filter to one day, then void each shipment "
            "(skips rows already voided)."
        )
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=_DEFAULT_CONFIG,
        help=f"UPS UI config JSON (default: {_DEFAULT_CONFIG.name})",
    )
    parser.add_argument(
        "--date",
        metavar="DATE",
        default=None,
        help="Ship date to void (default: today).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Open history and walk rows without clicking Void.",
    )
    parser.add_argument(
        "--manual-login",
        action="store_true",
        help="You log into UPS manually, then automation continues.",
    )
    args = parser.parse_args()

    if str(_HERE) not in sys.path:
        sys.path.insert(0, str(_HERE))

    from dotenv import load_dotenv

    load_dotenv(_HERE / ".env", override=False)

    ship_date = _parse_date(args.date) if args.date else None

    from automation.ups_online_batch_shipping import UpsBatchError
    from automation.ups_void_shipments import run_ups_void_shipments

    try:
        result = run_ups_void_shipments(
            config_path=args.config,
            ship_date=ship_date,
            manual_login=args.manual_login,
            dry_run=args.dry_run,
        )
    except UpsBatchError as exc:
        print(f"[ups/void] ERROR: {exc}", flush=True)
        return 1
    except Exception as exc:
        import traceback

        print(f"[ups/void] ERROR: {exc}", flush=True)
        traceback.print_exc()
        return 1

    print(
        f"[ups/void] Complete for {result.ship_date} — "
        f"voided {result.voided}, skipped {result.skipped}, failed {result.failed}, "
        f"{result.pages} page(s).",
        flush=True,
    )
    return 0 if result.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
