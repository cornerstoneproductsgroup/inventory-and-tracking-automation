"""Orchestrate CommerceHub + SPS order pulls and warehouse printing."""

from __future__ import annotations

import os
import sys
import time
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

    config_path = (os.environ.get("LOWES_TRACKING_CONFIG") or "").strip()
    if config_path:
        path = Path(config_path)
    else:
        path = None
        for name in ("config.json", "config.example.json"):
            candidate = _LOWES_DIR / name
            if candidate.is_file():
                path = candidate
                break
        if path is None:
            path = _LOWES_DIR / "config.example.json"
    return LowesTrackingAutomation(load_config(path))


def _browser_launch(playwright, *, for_sps: bool = False):
    headless = (os.environ.get("COMMERCEHUB_HEADLESS") or "false").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if for_sps:
        headless = (os.environ.get("HEADLESS") or os.environ.get("COMMERCEHUB_HEADLESS") or "false").strip().lower() in (
            "1",
            "true",
            "yes",
        )
    slow_mo = int(os.environ.get("COMMERCEHUB_SLOW_MO_MS") or "0")
    launch_kwargs: dict = {"headless": headless, "slow_mo": slow_mo}
    if for_sps:
        launch_kwargs["args"] = [
            "--disable-features=BlockThirdPartyCookies,TrackingProtection3pcd",
            "--disable-blink-features=AutomationControlled",
        ]
    return playwright.chromium.launch(**launch_kwargs)


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
    from automation.pull_orders_warehouse_print import print_warehouse_files, settle_after_downloads
    from run_sps_tracking import DEFAULT_STORAGE_STATE, ensure_sps_session

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
                pdfs, csvs = pull_commercehub_all(page, order_date=order_date)
                _log(
                    f"CommerceHub saved {len(pdfs)} PDF(s) and {len(csvs)} CSV(s)."
                )
                context.close()
                browser.close()
        except Exception as exc:
            msg = f"CommerceHub pull failed: {exc}"
            _log(f"ERROR: {msg}")
            errors.append(msg)

    if not skip_sps:
        _log("=== SPS Commerce: Tractor Supply + Grainger ===")
        try:
            headless = (os.environ.get("HEADLESS") or "false").strip().lower() in (
                "1",
                "true",
                "yes",
            )
            with sync_playwright() as p:
                browser = _browser_launch(p, for_sps=True)
                state_path = DEFAULT_STORAGE_STATE
                if state_path.is_file():
                    _log(f"SPS: loading session from {state_path}")
                    context = browser.new_context(
                        accept_downloads=True,
                        storage_state=str(state_path),
                    )
                else:
                    _log(
                        f"SPS: no session file at {state_path}; "
                        "will sign in with SPS_USERNAME/SPS_PASSWORD from .env "
                        "(same as inventory/tracking)."
                    )
                    context = browser.new_context(accept_downloads=True)
                page = context.new_page()
                ensure_sps_session(
                    page,
                    context,
                    state_path,
                    headless=headless,
                    allow_manual=not headless,
                )
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
            if not skip_commercehub or not skip_sps:
                settle_after_downloads()
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
