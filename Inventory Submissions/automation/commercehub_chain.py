"""
One Playwright browser session for CommerceHub (Rithum):

0. Optional (--with-invoice-reports): Depot + Lowe's invoice reports in the same browser (CDP).
1. Log in once (Lowe's config selectors / profile).
2. Submit inventory update (Lowe's + Home Depot IBL), unless --skip-inventory.
3. Home Depot quickship tracking (UPS CSV).
4. Home Depot quickinvoice.
5. Lowe's workflows from config (ship to store, ship to customer, invoice).
6. Home Depot Special Orders acknowledgment (thdso SKUs in vendor map), tracking + invoicing.

SPS / Tractor Supply is a different site — use run_sps_lane.py or run_full_workflow.py parallel lanes.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from datetime import date
from pathlib import Path

from playwright.sync_api import sync_playwright

_INVENTORY = Path(__file__).resolve().parent.parent
_ROOT = _INVENTORY.parent
_LOWES_DIR = _ROOT / "Lowe's Tracking Automation"


def main() -> int:
    os.chdir(_INVENTORY)
    sys.path.insert(0, str(_LOWES_DIR))
    os.environ["COMMERCEHUB_CHAIN_FAST"] = "1"

    try:
        from automation.config import load_settings
        from automation.depot_rithum_playwright import (
            run_depot_invoicing_with_page,
            run_depot_special_order_invoicing_with_page,
            run_depot_special_order_tracking_with_page,
            run_depot_tracking_with_page,
        )
        from automation.rithum import run_rithum_inventory_on_authenticated_page
        from lowes_tracking_automation import LowesTrackingAutomation, load_config

        parser = argparse.ArgumentParser(
            description="Single CommerceHub login, then inventory, Depot, and Lowe's."
        )
        parser.add_argument(
            "--submit",
            action="store_true",
            help="Submit forms in Lowe's / Depot steps (Rithum inventory runs unless --skip-inventory).",
        )
        parser.add_argument(
            "--skip-inventory",
            action="store_true",
            help="Skip Rithum inventory submission; still run Depot tracking/invoicing and Lowe's workflows.",
        )
        parser.add_argument(
            "--skip-depot",
            action="store_true",
            help="Skip Home Depot tracking and invoicing steps.",
        )
        parser.add_argument(
            "--skip-lowes",
            action="store_true",
            help="Skip Lowe's workflows (tracking/invoicing).",
        )
        parser.add_argument(
            "--skip-special-orders",
            action="store_true",
            help="Skip Home Depot Special Orders (thdso) acknowledgment/tracking/invoicing.",
        )
        parser.add_argument(
            "--lowes-config",
            type=Path,
            default=_LOWES_DIR / "config.example.json",
            help="Path to Lowe's JSON config.",
        )
        parser.add_argument(
            "--with-invoice-reports",
            action="store_true",
            help="After login, run Depot + Lowe's invoice reports in the same browser (no restart).",
        )
        parser.add_argument(
            "--invoice-report-date",
            default=None,
            metavar="DATE",
            help="Invoice report calendar date (YYYY-MM-DD). Default: previous business day.",
        )
        args = parser.parse_args()

        if not args.lowes_config.is_file():
            print(f"Config not found: {args.lowes_config}")
            return 1

        from automation.commercehub_timeouts import (  # noqa: E402
            default_page_timeout_ms,
            navigation_timeout_ms,
        )

        settings = load_settings()
        config = copy.deepcopy(load_config(args.lowes_config))
        delays = config["rithum"].setdefault("login_delays_ms", {})
        for key in list(delays.keys()):
            delays[key] = max(120, int(delays[key]) // 2)

        automation = LowesTrackingAutomation(config)
        automation.load_csv_index()
        step_errors: list[str] = []

        from automation.workflow_run_report import (  # noqa: E402
            log_and_record_skip,
            print_final_summary,
            record_error,
            record_skip,
            report_file_path,
        )

        if report_file_path() is None:
            from automation.workflow_run_report import init_run_report

            init_run_report(_INVENTORY / ".workflow_run_report.jsonl")

        def _run_step(title: str, fn) -> None:
            try:
                fn()
            except Exception as exc:
                msg = f"{title}: {exc}"
                print(f"WARN: {msg}")
                step_errors.append(msg)
                record_error(title, str(exc))

        def _chain_skip(step: str, reason: str) -> None:
            log_and_record_skip(step, reason)

        invoice_report_day: date | None = None
        if args.invoice_report_date:
            inv_dir = _ROOT / "invoice report"
            if str(inv_dir) not in sys.path:
                sys.path.insert(0, str(inv_dir))
            from commercehub_previous_business_day import parse_report_date  # noqa: E402

            invoice_report_day = parse_report_date(args.invoice_report_date.strip())

        cdp_port = int((os.environ.get("COMMERCEHUB_CDP_PORT") or "9333").strip())
        cdp_url = f"http://127.0.0.1:{cdp_port}"

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=settings.headless,
                slow_mo=0,
                args=[f"--remote-debugging-port={cdp_port}"],
            )
            context = browser.new_context()
            page = context.new_page()
            ch_timeout = default_page_timeout_ms()
            ch_nav = navigation_timeout_ms()
            page.set_default_timeout(max(settings.timeout_ms, ch_timeout))
            page.set_default_navigation_timeout(max(settings.timeout_ms, ch_nav))
            try:
                automation.login(page)

                if args.with_invoice_reports:
                    from automation.commercehub_cdp_invoice import run_retail_invoices_via_cdp

                    _run_step(
                        "CommerceHub invoice reports (Depot + Lowe's)",
                        lambda: run_retail_invoices_via_cdp(
                            cdp_url, report_day=invoice_report_day
                        ),
                    )

                if args.skip_inventory:
                    print("\n=== Rithum inventory skipped (--skip-inventory) ===")
                    _chain_skip("Rithum inventory", "Disabled via --skip-inventory")
                else:
                    print("\n=== Rithum inventory (Lowe's + Home Depot) ===")
                    _run_step(
                        "Rithum inventory",
                        lambda: run_rithum_inventory_on_authenticated_page(page, settings),
                    )

                if args.skip_depot:
                    print("\n=== Home Depot tracking/invoicing skipped (--skip-depot) ===")
                    _chain_skip("Depot tracking", "Disabled via --skip-depot")
                    _chain_skip("Depot invoicing", "Disabled via --skip-depot")
                else:
                    print("\n=== Home Depot tracking ===")
                    _run_step("Depot tracking", lambda: run_depot_tracking_with_page(page))

                    print("\n=== Home Depot invoicing ===")
                    _run_step("Depot invoicing", lambda: run_depot_invoicing_with_page(page))

                if args.skip_lowes:
                    print("\n=== Lowe's workflows skipped (--skip-lowes) ===")
                    _chain_skip("Lowe's workflows", "Disabled via --skip-lowes")
                else:
                    print("\n=== Lowe's workflows (all) ===")
                    _run_step(
                        "Lowe's workflows",
                        lambda: automation.run_workflows_after_login(
                            page,
                            do_submit=bool(args.submit),
                            workflow_filter="all",
                        ),
                    )

                print("\n=== Home Depot Special Orders (acknowledge, track, invoice) ===")
                if args.skip_special_orders:
                    print("Depot Special Orders: skipped (--skip-special-orders).")
                    _chain_skip("Depot Special Orders", "Disabled via --skip-special-orders")
                else:
                    _run_step(
                        "Depot Special Orders",
                        lambda: run_depot_special_order_tracking_with_page(page),
                    )
                    print("\n=== Home Depot Special Orders invoicing ===")
                    _run_step(
                        "Depot Special Orders invoicing",
                        lambda: run_depot_special_order_invoicing_with_page(page),
                    )
            finally:
                context.close()
                browser.close()

        print("\nCommerceHub chain complete.")
        print(json.dumps(automation.stats, indent=2))
        if os.environ.get("WORKFLOW_RUN_REPORT_SUPPRESS_SUMMARY") != "1":
            print_final_summary(extra_errors=step_errors, success=not step_errors)
        return 0 if not step_errors else 1
    except Exception as exc:
        print(f"CommerceHub chain error: {exc}")
        return 1
    finally:
        os.environ.pop("COMMERCEHUB_CHAIN_FAST", None)
