"""
Single SPS Commerce browser session: Tractor invoice report (optional), inventory,
then Tractor + Grainger tracking/invoicing — without closing between steps.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _parse_date(raw: str) -> date:
    inv_dir = _HERE.parent / "invoice report"
    if inv_dir.is_dir() and str(inv_dir) not in sys.path:
        sys.path.insert(0, str(inv_dir))
    from commercehub_previous_business_day import parse_report_date

    return parse_report_date(raw.strip())


def main() -> int:
    os.chdir(_HERE)
    if str(_HERE) not in sys.path:
        sys.path.insert(0, str(_HERE))

    parser = argparse.ArgumentParser(description="SPS lane: invoice + inventory + tracking in one browser.")
    parser.add_argument("--skip-inventory", action="store_true")
    parser.add_argument("--skip-tracking", action="store_true")
    parser.add_argument("--skip-grainger", action="store_true")
    parser.add_argument("--skip-tractor", action="store_true", help="Skip Tractor Supply tracking (Grainger-only runs).")
    parser.add_argument(
        "--with-invoice-reports",
        action="store_true",
        help="Run Tractor Supply invoice report after login (same browser via CDP).",
    )
    parser.add_argument("--invoice-report-date", default=None, metavar="DATE")
    parser.add_argument("--submit", action="store_true", help="Submit SPS shipment/invoice documents.")
    parser.add_argument("--interactive-login", action="store_true")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument(
        "--csv-path",
        default=None,
        help="UPS tracking CSV (default: run_sps_tracking.CSV_PATH).",
    )
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    from automation.config import load_sps_settings
    from automation.sps import run_sps_inventory_on_authenticated_page
    from run_sps_tracking import (
        CSV_PATH,
        DEFAULT_STORAGE_STATE,
        run_sps_partner_tracking_on_page,
    )

    settings = load_sps_settings()
    invoice_day: date | None = None
    if args.invoice_report_date:
        try:
            invoice_day = _parse_date(args.invoice_report_date)
        except ValueError as exc:
            print(f"ERROR: {exc}")
            return 1

    csv_path = Path(args.csv_path) if args.csv_path else Path(CSV_PATH)
    state_path = DEFAULT_STORAGE_STATE
    cdp_port = int((os.environ.get("SPS_CDP_PORT") or "9334").strip())
    cdp_url = f"http://127.0.0.1:{cdp_port}"
    step_errors: list[str] = []

    def _run_step(title: str, fn) -> None:
        try:
            fn()
        except Exception as exc:
            msg = f"{title}: {exc}"
            print(f"WARN: {msg}")
            step_errors.append(msg)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=bool(args.headless),
            args=[
                f"--remote-debugging-port={cdp_port}",
                "--disable-features=BlockThirdPartyCookies,TrackingProtection3pcd",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        if args.interactive_login and state_path.is_file():
            try:
                state_path.unlink()
                print(f"Removed old SPS session before interactive login: {state_path}")
            except OSError as exc:
                print(f"Warning: could not remove old session file: {exc}")

        if state_path.is_file() and not args.interactive_login:
            context = browser.new_context(storage_state=str(state_path))
        else:
            context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(settings.timeout_ms)

        try:
            from run_sps_tracking import ensure_sps_session, interactive_login_then_save

            if args.interactive_login:
                interactive_login_then_save(page, context, state_path)
            else:
                ensure_sps_session(
                    page,
                    context,
                    state_path,
                    headless=bool(args.headless),
                    allow_manual=not args.headless,
                )

            if args.with_invoice_reports:
                from automation.sps_cdp_invoice import run_tractor_invoice_via_cdp

                _run_step(
                    "SPS Tractor Supply invoice report",
                    lambda: run_tractor_invoice_via_cdp(cdp_url, report_day=invoice_day),
                )

            if not args.skip_inventory:
                print("\n=== SPS Commerce — Tractor Supply inventory ===")
                _run_step(
                    "SPS inventory",
                    lambda: run_sps_inventory_on_authenticated_page(page, context),
                )
            else:
                print("\n=== SPS inventory skipped ===")

            if not args.skip_tracking:
                if not args.skip_tractor:
                    print("\n=== SPS Commerce — Tractor Supply tracking ===")
                    _run_step(
                        "SPS Tractor tracking",
                        lambda: run_sps_partner_tracking_on_page(
                            page,
                            context,
                            csv_path=csv_path,
                            partner_name="Tractor Supply Dropship",
                            submit=bool(args.submit),
                            storage_path=state_path,
                            headless=bool(args.headless),
                            ensure_session=False,
                        ),
                    )
                if not args.skip_grainger:
                    print("\n=== SPS Commerce — Grainger tracking ===")
                    _run_step(
                        "SPS Grainger tracking",
                        lambda: run_sps_partner_tracking_on_page(
                            page,
                            context,
                            csv_path=csv_path,
                            partner_name="Grainger",
                            submit=bool(args.submit),
                            storage_path=state_path,
                            headless=bool(args.headless),
                            ensure_session=False,
                        ),
                    )
            else:
                print("\n=== SPS tracking skipped ===")

            try:
                context.storage_state(path=str(state_path))
                print(f"Saved SPS session: {state_path}")
            except OSError as exc:
                print(f"Warning: could not save SPS session ({exc})")
        finally:
            context.close()
            browser.close()

    if step_errors:
        print("\nSPS lane completed with warnings:")
        for err in step_errors:
            print(f"  - {err}")
        return 1
    print("\nSPS lane complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
