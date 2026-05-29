"""Orchestrate CommerceHub + SPS order pulls and warehouse printing."""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

from playwright.sync_api import sync_playwright

_HERE = Path(__file__).resolve().parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

# Lowe's automation lives in sibling folder; chain adds it to path before import.
_LOWES_DIR = _HERE.parent / "Lowe's Tracking Automation"


def _log(msg: str) -> None:
    print(f"[pull-orders] {msg}", flush=True)


def _load_commercehub_automation():
    if str(_LOWES_DIR) not in sys.path:
        sys.path.insert(0, str(_LOWES_DIR))
    from lowes_tracking_automation import LowesTrackingAutomation, load_config

    return LowesTrackingAutomation(load_config())


def _browser_launch(playwright):
    headless = (os.environ.get("COMMERCEHUB_HEADLESS") or "false").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    slow_mo = int(os.environ.get("COMMERCEHUB_SLOW_MO_MS") or "0")
    return playwright.chromium.launch(headless=headless, slow_mo=slow_mo)


def run_pull_orders(
    *,
    order_date: date | None = None,
    skip_commercehub: bool = False,
    skip_sps: bool = False,
    skip_warehouse_print: bool = False,
    skip_warehouse_wait: bool = False,
) -> int:
    from automation.pull_orders_commercehub import pull_commercehub_all
    from automation.pull_orders_sps import pull_sps_all
    from automation.pull_orders_warehouse_print import print_warehouse_files
    from run_sps_tracking import (
        DEFAULT_STORAGE_STATE,
        goto_dashboard,
        login_with_env_credentials_then_save,
    )

    errors: list[str] = []

    if not skip_commercehub:
        _log("=== CommerceHub: packing slips + order CSVs ===")
        try:
            automation = _load_commercehub_automation()
            with sync_playwright() as p:
                browser = _browser_launch(p)
                context = browser.new_context(accept_downloads=True)
                page = context.new_page()
                automation.login(page)
                pull_commercehub_all(page, order_date=order_date)
                context.close()
                browser.close()
        except Exception as exc:
            msg = f"CommerceHub pull failed: {exc}"
            _log(f"ERROR: {msg}")
            errors.append(msg)

    if not skip_sps:
        _log("=== SPS Commerce: Tractor Supply + Grainger ===")
        try:
            with sync_playwright() as p:
                browser = _browser_launch(p)
                storage = DEFAULT_STORAGE_STATE if DEFAULT_STORAGE_STATE.is_file() else None
                context = browser.new_context(
                    accept_downloads=True,
                    storage_state=str(storage) if storage else None,
                )
                page = context.new_page()
                goto_dashboard(page)
                if not login_with_env_credentials_then_save(
                    page, context, DEFAULT_STORAGE_STATE
                ):
                    _log("SPS: using saved session or manual sign-in may be required.")
                pull_sps_all(page, context, order_date=order_date)
                context.close()
                browser.close()
        except Exception as exc:
            msg = f"SPS pull failed: {exc}"
            _log(f"ERROR: {msg}")
            errors.append(msg)

    if not skip_warehouse_print:
        _log("=== Warehouse print files ===")
        try:
            print_warehouse_files(
                order_date=order_date,
                skip_wait=skip_warehouse_wait,
            )
        except Exception as exc:
            msg = f"Warehouse print failed: {exc}"
            _log(f"ERROR: {msg}")
            errors.append(msg)

    if errors:
        _log("Completed with errors:")
        for e in errors:
            _log(f"  - {e}")
        return 1

    _log("Pull orders workflow finished successfully.")
    return 0
