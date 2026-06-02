"""CLI: download Amazon Deferred Transaction CSV to the Input share folder."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_DEFAULT_CONFIG = _SCRIPT_DIR / "amazon_seller.json"
if not _DEFAULT_CONFIG.is_file():
    _DEFAULT_CONFIG = _SCRIPT_DIR / "amazon_seller.example.json"


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
            "Amazon Seller Central: Payments → Reports Repository → Deferred Transaction → "
            "save CSV to the Amazon Input share (previous calendar day in filename)."
        )
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=_DEFAULT_CONFIG,
        help=f"UI config JSON (default: {_DEFAULT_CONFIG.name})",
    )
    parser.add_argument(
        "--date",
        metavar="DATE",
        default=None,
        help="Run date for filename (default: today → file uses yesterday's date).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Override output CSV path (default: Amazon Input share).",
    )
    parser.add_argument(
        "--skip-postprocess",
        action="store_true",
        help="Only download; do not run amazon_invoice_postprocess.",
    )
    parser.add_argument(
        "--check-credentials",
        action="store_true",
        help="Verify AMAZON_SELLER_EMAIL/PASSWORD in .env (no browser).",
    )
    args = parser.parse_args()

    if str(_SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(_SCRIPT_DIR))

    try:
        from dotenv import load_dotenv

        load_dotenv(_SCRIPT_DIR / ".env")
    except ImportError:
        pass

    if args.check_credentials:
        from amazon_seller_credentials import env_file_path, load_amazon_seller_credentials

        try:
            creds = load_amazon_seller_credentials()
            print(f"OK: Amazon Seller credentials for {creds.email!r} ({env_file_path()})")
            return 0
        except ValueError as exc:
            print(f"ERROR: {exc}")
            return 1

    from amazon_seller_config import ON_HOLD_REASON, seller_download_enabled

    if not seller_download_enabled():
        print(f"\n[amazon-seller] ON HOLD — not running.\n{ON_HOLD_REASON}\n")
        return 0

    run_date = _parse_date(args.date) if args.date else None

    try:
        from amazon_seller_download import AmazonSellerDownloadError, run_amazon_seller_download

        dest = run_amazon_seller_download(
            config_path=args.config.expanduser(),
            run_date=run_date,
            dest_path=args.output,
            skip_postprocess=args.skip_postprocess,
        )
        print(f"\nAmazon seller download completed: {dest}")
        return 0
    except AmazonSellerDownloadError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
