"""
One Playwright browser session for CommerceHub (Rithum):

1. Log in once (Lowe's config selectors / profile).
2. Submit inventory update (Lowe's + Home Depot IBL), unless --skip-inventory.
3. Home Depot quickship tracking (UPS CSV).
4. Home Depot quickinvoice.
5. Lowe's workflows from config (ship to store, ship to customer, invoice).
6. Home Depot Special Orders tracking (thdso; skips when queue empty).

SPS / Tractor Supply is a different site — run separately (e.g. run_full_workflow.py).
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
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
            "--lowes-config",
            type=Path,
            default=_LOWES_DIR / "config.example.json",
            help="Path to Lowe's JSON config.",
        )
        args = parser.parse_args()

        if not args.lowes_config.is_file():
            print(f"Config not found: {args.lowes_config}")
            return 1

        settings = load_settings()
        config = copy.deepcopy(load_config(args.lowes_config))
        delays = config["rithum"].setdefault("login_delays_ms", {})
        for key in list(delays.keys()):
            delays[key] = max(120, int(delays[key]) // 2)

        automation = LowesTrackingAutomation(config)
        automation.load_csv_index()
        step_errors: list[str] = []

        def _run_step(title: str, fn) -> None:
            try:
                fn()
            except Exception as exc:
                msg = f"{title}: {exc}"
                print(f"WARN: {msg}")
                step_errors.append(msg)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=settings.headless, slow_mo=0)
            context = browser.new_context()
            page = context.new_page()
            page.set_default_timeout(settings.timeout_ms)
            try:
                automation.login(page)

                if args.skip_inventory:
                    print("\n=== Rithum inventory skipped (--skip-inventory) ===")
                else:
                    print("\n=== Rithum inventory (Lowe's + Home Depot) ===")
                    _run_step(
                        "Rithum inventory",
                        lambda: run_rithum_inventory_on_authenticated_page(page, settings),
                    )

                if args.skip_depot:
                    print("\n=== Home Depot tracking/invoicing skipped (--skip-depot) ===")
                else:
                    print("\n=== Home Depot tracking ===")
                    _run_step("Depot tracking", lambda: run_depot_tracking_with_page(page))

                    print("\n=== Home Depot invoicing ===")
                    _run_step("Depot invoicing", lambda: run_depot_invoicing_with_page(page))

                if args.skip_lowes:
                    print("\n=== Lowe's workflows skipped (--skip-lowes) ===")
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

                print("\n=== Home Depot Special Orders tracking ===")
                if args.skip_depot and args.skip_lowes:
                    print(
                        "Depot Special Orders: skipped (Depot and Lowe's tracking both disabled)."
                    )
                else:
                    _run_step(
                        "Depot Special Orders tracking",
                        lambda: run_depot_special_order_tracking_with_page(page),
                    )
            finally:
                context.close()
                browser.close()

        print("\nCommerceHub chain complete.")
        print(json.dumps(automation.stats, indent=2))
        if step_errors:
            print("\nCommerceHub completed with warnings:")
            for err in step_errors:
                print(f"  - {err}")
        return 0
    except Exception as exc:
        print(f"CommerceHub chain error: {exc}")
        return 1
    finally:
        os.environ.pop("COMMERCEHUB_CHAIN_FAST", None)
