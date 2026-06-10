"""CLI: send daily vendor-order emails through Outlook."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_DEFAULT_CONFIG = _HERE / "vendor_email_config.json"


def _prompt_vendor_menu(config_path: Path) -> str | None:
    """Return vendor_folder to send, or None for ALL vendors."""
    from automation.outlook_vendor_emailer import VendorEmailError, load_vendor_email_config

    cfg = load_vendor_email_config(config_path)
    names = [entry.vendor_folder for entry in cfg.vendors]
    if not names:
        raise VendorEmailError("No vendors listed in config.")

    print()
    print("=" * 60)
    print("  Vendor Emails — select vendor")
    print("=" * 60)
    print("  0  ALL")
    for i, name in enumerate(names, start=1):
        print(f"  {i:2}  {name}")
    print()

    while True:
        try:
            choice = input(f"Enter 0–{len(names)} (0 = ALL): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            raise SystemExit(130) from None
        if not choice:
            continue
        if choice == "0":
            print("Selected: ALL vendors")
            return None
        try:
            idx = int(choice)
        except ValueError:
            print("Enter a number from the list.")
            continue
        if 1 <= idx <= len(names):
            selected = names[idx - 1]
            print(f"Selected: {selected}")
            return selected
        print(f"Enter a number from 0 to {len(names)}.")


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
        "--menu",
        action="store_true",
        help="Pick ALL or one vendor from a numbered list (used with --send).",
    )
    parser.add_argument(
        "--no-menu",
        action="store_true",
        help="With --send, send to all vendors without showing the vendor menu.",
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
    if args.menu and args.no_menu:
        parser.error("Use either --menu or --no-menu, not both.")
    if args.menu and not args.send:
        parser.error("--menu requires --send.")
    if args.vendor and (args.menu or args.no_menu):
        parser.error("Use --vendor alone, or use --menu / default send menu, not both.")

    os.chdir(_HERE)
    if str(_HERE) not in sys.path:
        sys.path.insert(0, str(_HERE))

    from automation.outlook_vendor_emailer import VendorEmailError, send_vendor_emails

    config_path = args.config.resolve()
    vendor_filter = args.vendor
    show_menu = bool(args.menu) or (bool(args.send) and not args.vendor and not args.no_menu)
    if show_menu:
        vendor_filter = _prompt_vendor_menu(config_path)

    try:
        return send_vendor_emails(
            config_path=config_path,
            dry_run=not bool(args.send) and not bool(args.preview),
            preview=bool(args.preview),
            vendor_filter=vendor_filter,
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
