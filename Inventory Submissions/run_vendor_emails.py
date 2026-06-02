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
        "--delay-s",
        type=float,
        default=0.5,
        help="Delay between sends in seconds (default: 0.5).",
    )
    args = parser.parse_args()

    os.chdir(_HERE)
    if str(_HERE) not in sys.path:
        sys.path.insert(0, str(_HERE))

    from automation.outlook_vendor_emailer import VendorEmailError, send_vendor_emails

    try:
        return send_vendor_emails(
            config_path=args.config.resolve(),
            dry_run=not bool(args.send),
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
