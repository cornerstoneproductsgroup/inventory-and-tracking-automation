"""Launch installed Chrome with the normal profile for Amazon Seller Central (CDP)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent


def _ensure_inventory_on_path() -> None:
    inv = _SCRIPT_DIR.parent / "Inventory Submissions"
    if inv.is_dir() and str(inv) not in sys.path:
        sys.path.insert(0, str(inv))


def _log(msg: str) -> None:
    print(f"[amazon-seller] {msg}", flush=True)


def pick_seller_central_page(context, *, home_url: str):
    for pg in context.pages:
        try:
            url = (pg.url or "").lower()
            if "sellercentral.amazon.com" in url:
                pg.bring_to_front()
                return pg
        except Exception:
            continue
    if context.pages:
        pg = context.pages[0]
        pg.bring_to_front()
        return pg
    return context.new_page()


def goto_seller_central_home(page, home_url: str) -> None:
    current = (page.url or "").strip()
    _log(f"Active tab: {current!r}")
    if "sellercentral.amazon.com" in current.lower() and "signin" not in current.lower():
        if "/ap/" not in current.lower() or "sellercentral" in current.lower():
            _log(f"Already on Seller Central: {current}")
            return

    last_err: Exception | None = None
    for attempt in range(1, 4):
        try:
            _log(f"Navigating to {home_url} (attempt {attempt}/3)…")
            page.goto(home_url, wait_until="domcontentloaded", timeout=120_000)
            url = (page.url or "").lower()
            if "sellercentral.amazon.com" in url:
                _log(f"Seller Central loaded: {page.url}")
                return
        except Exception as exc:
            last_err = exc
            _log(f"WARN: navigation attempt {attempt} failed: {exc}")
        import time

        time.sleep(1.0)

    raise RuntimeError(
        f"Browser stayed on {page.url!r}; could not open Seller Central. {last_err}"
    )


def connect_system_chrome(
    playwright,
    *,
    home_url: str,
    port: int,
    log_dir: Path | None = None,
):
    """
    Attach Playwright to installed Chrome using the same User Data folder as daily use.
    Closes Chrome briefly if needed, then reopens with remote debugging.
    """
    _ensure_inventory_on_path()
    from automation.ups_chrome_launch import (
        cdp_endpoint_ready,
        connect_playwright_cdp,
        launch_browser_for_cdp,
    )

    if cdp_endpoint_ready(port, timeout_s=1.0):
        _log(f"Chrome debug port {port} already open — attaching to your running browser.")
    else:
        _log(
            "Starting Chrome with your normal profile (same cookies/login as daily use). "
            "Chrome will close briefly, then reopen."
        )
        launch_browser_for_cdp(
            home_url=home_url,
            port=port,
            log_dir=log_dir or _SCRIPT_DIR,
        )

    browser = connect_playwright_cdp(playwright, port)
    if not browser.contexts:
        raise RuntimeError(f"Chrome on port {port} has no browser contexts.")
    context = browser.contexts[0]
    page = pick_seller_central_page(context, home_url=home_url)
    goto_seller_central_home(page, home_url)
    return browser, page
