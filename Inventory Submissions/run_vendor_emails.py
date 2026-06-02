"""CLI: send daily vendor-order emails through Outlook."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_DEFAULT_CONFIG = _HERE / "vendor_email_config.json"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Send vendor emails from z- Daily Vendor Orders using Outlook. "
            "Default mode is dry-run (no sends)."
        )
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=_DEFAULT_CONFIG,
        help=f"Path to vendor email JSON config (default: {_DEFAULT_CONFIG.name}).",
    )
    parser.add_argument(
        "--send",
        action="store_true",
        help="Actually send emails. Without this flag, only dry-run preview is shown.",
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help=(
            "Open each message in Outlook (To/CC, body, attachments) without sending. "
            "Use to verify distribution lists / display names resolve correctly."
        ),
    )
    parser.add_argument(
        "--vendor",
        metavar="FOLDER",
        help="Only process this vendor_folder name (exact match, e.g. \"MMR\").",
    )
    parser.add_argument(
        "--no-pause",
        action="store_true",
        help="With --preview, open all messages at once (no Enter between vendors).",
    )
    parser.add_argument(
        "--delay-s",
        type=float,
        default=0.5,
        help="Delay between sends in seconds (default: 0.5).",
    )
    args = parser.parse_args()

    if args.send and args.preview:
        parser.error("Use either --send or --preview, not both.")

    os.chdir(_HERE)
    if str(_HERE) not in sys.path:
        sys.path.insert(0, str(_HERE))

    from automation.outlook_vendor_emailer import VendorEmailError, send_vendor_emails

    try:
        return send_vendor_emails(
            config_path=args.config.resolve(),
            dry_run=not bool(args.send) and not bool(args.preview),
            preview=bool(args.preview),
            vendor_filter=args.vendor,
            preview_pause=not bool(args.no_pause),
            send_delay_s=max(0.0, float(args.delay_s)),
        )
    except VendorEmailError as exc:
        print(f"ERROR: {exc}")
        return 1
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
