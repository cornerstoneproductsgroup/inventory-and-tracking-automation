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
    from automation.config import apply_rithum_env_to_lowes_config
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
    config = apply_rithum_env_to_lowes_config(load_config(path))
    user = (config.get("rithum") or {}).get("username") or ""
    _log(
        f"CommerceHub login: RITHUM_USERNAME from Inventory Submissions/.env "
        f"({user!r}) — same as tracking/inventory."
    )
    return LowesTrackingAutomation(config)


def run_pull_orders(
    *,
    order_date: date | None = None,
    skip_commercehub: bool = False,
    skip_sps: bool = False,
    skip_warehouse_print: bool = False,
    skip_warehouse_wait: bool = False,
) -> int:
    from automation.pull_orders_browser import open_pull_orders_browser, persist_sps_session
    from automation.pull_orders_commercehub import login_commercehub_for_pull, pull_commercehub_all
    from automation.pull_orders_sps import pull_sps_all
    from automation.pull_orders_warehouse_print import print_warehouse_files, settle_after_downloads
    from run_sps_tracking import DEFAULT_STORAGE_STATE, ensure_sps_session

    errors: list[str] = []
    commercehub_ok = skip_commercehub
    sps_ok = skip_sps

    if not skip_commercehub:
        _log("=== CommerceHub: packing slips + order CSVs ===")
        try:
            automation = _load_commercehub_automation()
            with sync_playwright() as p:
                browser, context, page, _persistent = open_pull_orders_browser(
                    p, for_sps=False
                )
                context.set_default_timeout(120_000)
                context.set_default_navigation_timeout(120_000)
                try:
                    login_commercehub_for_pull(page, automation)
                    pdfs, csvs = pull_commercehub_all(page, order_date=order_date)
                    _log(
                        f"CommerceHub saved {len(pdfs)} PDF(s) and {len(csvs)} CSV(s)."
                    )
                    commercehub_ok = True
                finally:
                    context.close()
                    if browser is not None:
                        browser.close()
        except Exception as exc:
            msg = f"CommerceHub pull failed: {exc}"
            _log(f"ERROR: {msg}")
            errors.append(msg)
            commercehub_ok = False

    if not skip_sps:
        _log("=== SPS Commerce: Tractor Supply + Grainger ===")
        try:
            headless = (os.environ.get("HEADLESS") or "false").strip().lower() in (
                "1",
                "true",
                "yes",
            )
            state_path = DEFAULT_STORAGE_STATE
            with sync_playwright() as p:
                browser, context, page, persistent = open_pull_orders_browser(
                    p,
                    for_sps=True,
                    storage_state=state_path,
                )
                context.set_default_timeout(120_000)
                context.set_default_navigation_timeout(120_000)
                try:
                    if state_path.is_file():
                        _log(
                            f"SPS: loading session from {state_path} "
                            "(same file as SPS tracking/inventory)."
                        )
                    else:
                        _log(
                            f"SPS: no session file at {state_path}; "
                            "will sign in with SPS_USERNAME/SPS_PASSWORD from .env "
                            "(same as tracking/inventory)."
                        )
                    ensure_sps_session(
                        page,
                        context,
                        state_path,
                        headless=headless,
                        allow_manual=not headless,
                    )
                    pull_sps_all(page, context, order_date=order_date)
                    persist_sps_session(context, state_path, uses_persistent_profile=persistent)
                    sps_ok = True
                finally:
                    context.close()
                    if browser is not None:
                        browser.close()
        except Exception as exc:
            msg = f"SPS pull failed: {exc}"
            _log(f"ERROR: {msg}")
            errors.append(msg)
            sps_ok = False

    pulls_required_ok = commercehub_ok and sps_ok

    if not skip_warehouse_print:
        if not pulls_required_ok:
            _log("=== Warehouse print skipped ===")
            if not skip_commercehub and not commercehub_ok:
                _log(
                    "  CommerceHub pull did not complete — will not print warehouse files "
                    "(avoids printing stale PDFs from a previous day)."
                )
            if not skip_sps and not sps_ok:
                _log(
                    "  SPS pull did not complete — will not print warehouse files "
                    "(avoids printing stale PDFs from a previous day)."
                )
            errors.append("Warehouse print skipped because pull orders did not all succeed.")
        else:
            _log("=== Warehouse print files ===")
            try:
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
