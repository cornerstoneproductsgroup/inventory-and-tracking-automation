from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Browser, BrowserContext, Page, TimeoutError, sync_playwright
from automation.config import load_settings


CSV_PATH = Path(
    r"\\rygarcorp.com\shares\Cornerstone\Dot Com Packing Slips\zzz - Worldship Shipment Files\Export Info\UPS_CSV_EXPORT.csv"
)
DASHBOARD_URL = "https://commerce.spscommerce.com/fulfillment/dashboard/"
TRANSACTIONS_LIST_URL = "https://commerce.spscommerce.com/fulfillment/transactions/list/"
_HERE = Path(__file__).resolve().parent
DEFAULT_STORAGE_STATE = _HERE / "sps_playwright_storage.json"


def normalize_po(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    # Tractor Supply POs are 10 digits; prefer first explicit 10-digit run.
    m = re.search(r"\b(\d{10})\b", text)
    if m:
        return m.group(1)
    # Fallback: strip non-digits and keep first 10 digits only.
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 10:
        return digits[:10]
    return digits


def load_tracking_map(csv_path: Path) -> dict[str, str]:
    if not csv_path.is_file():
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    rows: list[list[str]] | None = None
    for enc in ("utf-8-sig", "latin1"):
        try:
            with csv_path.open("r", newline="", encoding=enc) as f:
                rows = list(csv.reader(f))
            break
        except UnicodeDecodeError:
            continue

    if not rows:
        return {}

    out: dict[str, str] = {}
    for row in rows:
        if len(row) < 2:
            continue
        po = normalize_po(row[0])
        tracking = (row[1] or "").strip().split()[0]
        if not po or not tracking:
            continue
        out.setdefault(po, tracking)
    return out


def _contexts(page: Page):
    live = [page]
    for f in page.frames:
        try:
            if f.is_detached():
                continue
        except Exception:
            continue
        live.append(f)
    return live


def click_first_visible(page: Page, selectors: list[str], *, timeout_ms: int = 10000) -> bool:
    for sel in selectors:
        for ctx in _contexts(page):
            try:
                loc = ctx.locator(sel)
                if loc.count() == 0:
                    continue
                target = loc.first
                target.wait_for(state="visible", timeout=timeout_ms)
                try:
                    target.scroll_into_view_if_needed()
                except Exception:
                    pass
                try:
                    target.click(timeout=timeout_ms)
                except Exception:
                    # Some SPS cards have inner overlays; force click as fallback.
                    target.click(timeout=timeout_ms, force=True)
                return True
            except Exception:
                continue
    return False


def clear_click_blockers(page: Page) -> None:
    """Best-effort removal of modal/backdrop overlays that intercept clicks."""
    # Try common close controls first.
    click_first_visible(
        page,
        [
            "button[aria-label='Close']",
            "button[title='Close']",
            "button:has-text('Close')",
            "button:has-text('Dismiss')",
            "button:has-text('Got it')",
            "button:has-text('OK')",
            "button:has-text('Continue')",
            "[data-testid='modalCancelBtn']",
            "[data-testid='modalOkBtn']",
            "xpath=//*[self::button or @role='button'][contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'close')]",
        ],
        timeout_ms=1200,
    )
    # ESC often closes SPS drawers/modals.
    for _ in range(2):
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(200)
        except Exception:
            pass
    # Remove common known backdrops if still present.
    for sel in (
        ".sps-modal__overlay",
        ".sps-overlay",
        ".sps-drawer__overlay",
        ".ReactModal__Overlay",
    ):
        try:
            for ctx in _contexts(page):
                try:
                    loc = ctx.locator(sel)
                    n = min(loc.count(), 8)
                    for i in range(n):
                        try:
                            node = loc.nth(i)
                            if node.is_visible():
                                node.evaluate("el => el.remove()")
                        except Exception:
                            continue
                except Exception:
                    continue
        except Exception:
            continue


def _looks_logged_out(url: str) -> bool:
    u = (url or "").lower()
    return any(
        x in u
        for x in (
            "login",
            "signin",
            "/auth",
            "sso",
            "okta",
            "microsoftonline",
            "adfs",
        )
    )


def _is_login_page_visible(page: Page) -> bool:
    """Detect SPS/IdP sign-in page by URL or visible login controls."""
    try:
        if _looks_logged_out(page.url):
            return True
    except Exception:
        pass
    login_selectors = (
        "input[name='username']",
        "input[name='password']",
        "input[type='password']",
        "button._button-login-id",
        "button._button-login-password",
        "button:has-text('Sign in')",
        "button:has-text('Log in')",
    )
    for sel in login_selectors:
        try:
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                return True
        except Exception:
            continue
    return False


def _looks_authenticated_sps(page: Page) -> bool:
    """Best-effort check that user is logged into SPS (not on sign-in)."""
    try:
        url = (page.url or "").lower()
    except Exception:
        url = ""
    if _looks_logged_out(url):
        return False
    # Common authenticated routes.
    if any(x in url for x in ("/fulfillment/", "/dashboard/", "/transactions/")):
        return True
    # If we're on SPS domain and no login controls are visible, treat as authenticated.
    if "commerce.spscommerce.com" in url and not _is_login_page_visible(page):
        return True
    # UI markers that usually exist only when authenticated.
    for sel in (
        "a[data-testid='dashboard_tab']",
        "a[href*='/fulfillment/transactions/list/']",
        "button:has-text('Advanced Search')",
        "button[data-testid='advSearchBottomSearchButton']",
    ):
        for ctx in _contexts(page):
            try:
                loc = ctx.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    return True
            except Exception:
                continue
    return False


def _raise_if_cookie_or_auth_wall(page: Page) -> None:
    try:
        body = page.content().lower()
    except Exception:
        body = ""
    if "cookies are disabled" in body or "enable all cookies" in body:
        # Only treat as fatal if the cookie warning is visibly rendered.
        cookie_wall_visible = False
        for sel in (
            "text=/cookies are disabled/i",
            "text=/enable all cookies/i",
            "text=/enable cookies/i",
        ):
            try:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    cookie_wall_visible = True
                    break
            except Exception:
                continue
        if cookie_wall_visible:
            raise RuntimeError(
                "SPS Commerce reports cookies are disabled in this browser profile. "
                "Enable cookies for Chromium / Playwright, then retry. "
                f"Dashboard: {DASHBOARD_URL}"
            )
    if _looks_logged_out(page.url) or _is_login_page_visible(page):
        raise RuntimeError(
            "Not logged into SPS Commerce in this browser session (redirected to sign-in). "
            "Fix: run SPS inventory once (it saves a session file after login), or run tracking with "
            "`--interactive-login` once to sign in and create the file, or pass `--storage-state` to a saved JSON.\n"
            f"  Default session file: {DEFAULT_STORAGE_STATE}\n"
            "  See also: https://commerce.spscommerce.com/fulfillment/dashboard/"
        )


def wait_for_transactions_page_ready(page: Page, *, timeout_ms: int = 45_000) -> None:
    """
    Ensure transactions UI is fully interactive before advanced-search actions.
    This prevents racing immediately after initial navigation.
    """
    deadline = time.monotonic() + timeout_ms / 1000.0
    ready_selectors = [
        "button:has-text('Advanced Search')",
        "input[placeholder*='Search here for a document']",
        "button[data-testid='advSearchBottomSearchButton']",
        "table",
        "tbody",
    ]
    while time.monotonic() < deadline:
        _raise_if_cookie_or_auth_wall(page)
        for sel in ready_selectors:
            for ctx in _contexts(page):
                try:
                    loc = ctx.locator(sel)
                    if loc.count() == 0:
                        continue
                    if loc.first.is_visible():
                        return
                except Exception:
                    # SPS often re-mounts frames during navigation; ignore transient detach races.
                    continue
        page.wait_for_timeout(200)
    raise RuntimeError("Transactions page did not become ready in time.")


def _load_sps_login_settings() -> tuple[str, str, str, int]:
    start_url = (os.environ.get("SPS_URL") or "").strip() or "https://commerce.spscommerce.com"
    username = ""
    password = ""
    timeout_ms = 30_000
    try:
        settings = load_settings()
        if (settings.sps_url or "").strip():
            start_url = settings.sps_url.strip()
        username = (settings.sps_username or "").strip()
        password = (settings.sps_password or "").strip()
        timeout_ms = int(settings.timeout_ms)
    except Exception:
        pass
    return start_url, username, password, timeout_ms


def _perform_sps_login(page: Page, username: str, password: str, timeout_ms: int) -> bool:
    """Deterministic SPS login flow (same sequence as working inventory script)."""
    if not username or not password:
        return False
    per_attempt_timeout = max(8_000, min(timeout_ms, 35_000))
    for attempt in range(1, 4):
        try:
            # Username step.
            user = page.locator("input[name='username']").first
            user.wait_for(state="visible", timeout=per_attempt_timeout)
            user.click(timeout=2_000)
            user.fill("")
            user.fill(username)
            next_btn = page.locator("button._button-login-id").first
            if next_btn.count() > 0:
                try:
                    next_btn.click(timeout=4_000)
                except Exception:
                    next_btn.click(timeout=4_000, force=True)
            else:
                user.press("Enter", timeout=2_000)

            # Password step.
            pwd = page.locator("input[name='password']").first
            pwd.wait_for(state="visible", timeout=per_attempt_timeout)
            pwd.click(timeout=2_000)
            pwd.fill("")
            pwd.fill(password)
            submit_btn = page.locator("button._button-login-password").first
            if submit_btn.count() > 0:
                try:
                    submit_btn.click(timeout=4_000)
                except Exception:
                    submit_btn.click(timeout=4_000, force=True)
            else:
                pwd.press("Enter", timeout=2_000)

            page.wait_for_load_state("domcontentloaded", timeout=per_attempt_timeout)
            if _looks_authenticated_sps(page) or not _is_login_page_visible(page):
                if attempt > 1:
                    print(f"SPS credential submit succeeded on attempt {attempt}.")
                return True
        except Exception:
            pass

        # Recover for next try: many IdP pages need a clean reload.
        if attempt < 3:
            try:
                page.goto("https://commerce.spscommerce.com", wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(500)
            except Exception:
                pass
    return False


def _wait_for_authenticated_sps(page: Page, timeout_ms: int = 120_000) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        if _looks_authenticated_sps(page):
            return True
        page.wait_for_timeout(400)
    return False


def interactive_login_then_save(page: Page, context: BrowserContext, storage_path: Path) -> None:
    """Try .env login first; allow manual completion fallback; then save session."""
    start_url, username, password, timeout_ms = _load_sps_login_settings()
    page.goto(start_url, wait_until="domcontentloaded", timeout=120_000)

    attempted_env = _perform_sps_login(page, username, password, timeout_ms)
    if attempted_env:
        print(">>> Attempted SPS login from .env credentials.")
    else:
        print(">>> Could not complete SPS login from .env automatically; waiting for manual sign-in.")

    if not _wait_for_authenticated_sps(page, timeout_ms=180_000):
        print(
            ">>> Complete SPS sign-in in the browser (including MFA if prompted), then press Enter."
        )
        input(">>> Press Enter once SPS is fully logged in...\n")
        if not _wait_for_authenticated_sps(page, timeout_ms=30_000):
            raise RuntimeError("SPS login was not detected after manual sign-in.")

    page.goto(TRANSACTIONS_LIST_URL, wait_until="domcontentloaded", timeout=120_000)
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(storage_path))
    print(f">>> Saved session file: {storage_path}\n")


def login_with_env_credentials_then_save(
    page: Page,
    context: BrowserContext,
    storage_path: Path,
    *,
    timeout_ms: int = 120_000,
) -> bool:
    """Attempt SPS login from .env only (no manual pause), then save session."""
    start_url, username, password, settings_timeout = _load_sps_login_settings()
    effective_timeout = int(timeout_ms or settings_timeout)
    try:
        page.goto(start_url, wait_until="domcontentloaded", timeout=120_000)
    except Exception as ex:
        print(f"Could not open SPS login URL for auto-login: {ex}")
        return False

    if not _perform_sps_login(page, username, password, effective_timeout):
        print("SPS auto-login could not complete username/password submit from .env.")
        return False
    if not _wait_for_authenticated_sps(page, timeout_ms=120_000):
        print("SPS auto-login did not reach authenticated state (MFA/SSO may still be required).")
        return False

    page.goto(TRANSACTIONS_LIST_URL, wait_until="domcontentloaded", timeout=120_000)
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(storage_path))
    print(f"SPS auto-login succeeded; saved session: {storage_path}")
    return True


def goto_dashboard(page: Page) -> None:
    page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=120_000)
    page.wait_for_timeout(800)
    _raise_if_cookie_or_auth_wall(page)
    # SPA: wait for product bar / dashboard tab before continuing.
    try:
        page.locator("a[data-testid='dashboard_tab']").first.wait_for(state="visible", timeout=120_000)
    except Exception:
        page.locator("a[href*='/fulfillment/dashboard/']").filter(has_text=re.compile(r"Dashboard", re.I)).first.wait_for(
            timeout=30_000
        )
    _raise_if_cookie_or_auth_wall(page)


def open_ready_for_shipment(page: Page) -> None:
    # Use only the Transactions + Advanced Search path.
    open_ready_for_shipment_via_advanced_search(page)


def open_ready_for_shipment_via_advanced_search(page: Page) -> None:
    print("STEP 1.1: Open transactions list...")
    last_err: Exception | None = None
    for attempt in (1, 2):
        try:
            # Stay on the transactions route; do not detour to dashboard.
            page.goto(TRANSACTIONS_LIST_URL, wait_until="domcontentloaded", timeout=120_000)
            _raise_if_cookie_or_auth_wall(page)
            wait_for_transactions_page_ready(page, timeout_ms=90_000)
            last_err = None
            break
        except Exception as ex:
            last_err = ex
            # If UI is already present on transactions, continue instead of rerouting.
            try:
                on_transactions = "/fulfillment/transactions/list/" in (page.url or "").lower()
                if on_transactions:
                    for sel in (
                        "button:has-text('Advanced Search')",
                        "input[data-testid='advancedSearchWorkflowsMultiselect__option-list-input']",
                    ):
                        for ctx in _contexts(page):
                            loc = ctx.locator(sel)
                            if loc.count() > 0:
                                last_err = None
                                break
                        if last_err is None:
                            break
                    if last_err is None:
                        break
            except Exception:
                pass
            print(f"STEP 1.1 attempt {attempt} failed: {ex}")
            clear_click_blockers(page)
            page.wait_for_timeout(800)
    if last_err is not None:
        raise RuntimeError(f"Could not open transactions list: {last_err}")
    clear_click_blockers(page)
    print(f"STEP 1.1 done: {page.url}")

    workflow_selector = "input[data-testid='advancedSearchWorkflowsMultiselect__option-list-input']"
    workflow_input = page.locator(workflow_selector)

    # Sometimes advanced filters are already open; skip the toggle click in that case.
    already_open = False
    try:
        if workflow_input.count() > 0:
            workflow_input.first.wait_for(state="visible", timeout=3000)
            already_open = True
    except Exception:
        already_open = False

    if not already_open:
        print("STEP 1.2: Open Advanced Search...")
        # Ultra-fast deterministic click first.
        fast_clicked = click_first_visible(
            page,
            [
                "xpath=//button[normalize-space()='Advanced Search']",
                "button:has-text('Advanced Search')",
            ],
            timeout_ms=1200,
        )
        if not fast_clicked:
            clear_click_blockers(page)
            # Minimal fallback only when strict fast path fails.
            if not click_first_visible(
                page,
                [
                    "button:has-text('Advanced Search')",
                    "[role='button']:has-text('Advanced Search')",
                    "a:has-text('Advanced Search')",
                    "text=/advanced\\s*search/i",
                ],
                timeout_ms=3000,
            ):
                raise RuntimeError(
                    "Could not click 'Advanced Search' on transactions page "
                    "(and advanced search fields were not already visible)."
                )
        print("STEP 1.2 done.")
    else:
        print("STEP 1.2 skipped: Advanced Search already open.")

    print("STEP 1.3: Select Workflow = Shipment...")
    ensure_workflow_shipment_selected(page, workflow_selector)
    print("STEP 1.3 done.")

    print("STEP 1.4: Click Search...")
    click_advanced_search_button(page)
    page.wait_for_load_state("domcontentloaded")
    print("STEP 1.4 done.")


def set_workflow_ready_for_shipment(page: Page, workflow_selector: str) -> None:
    def _clear_existing_workflow_tags() -> None:
        # Remove any pre-selected workflow chips (e.g., Acknowledgment) so only Shipment remains.
        for _ in range(8):
            removed = False
            for sel in (
                "button[aria-label*='Remove']",
                "button[title*='Remove']",
                "[data-testid*='remove']",
                "i.sps-icon-close",
                "i.sps-icon-x",
                "[class*='tag'] [class*='close']",
                "[class*='chip'] [class*='close']",
            ):
                for ctx in _contexts(page):
                    loc = ctx.locator(sel)
                    n = min(loc.count(), 6)
                    for i in range(n):
                        node = loc.nth(i)
                        try:
                            if not node.is_visible():
                                continue
                            try:
                                node.click(timeout=600)
                            except Exception:
                                node.click(timeout=600, force=True)
                            removed = True
                            page.wait_for_timeout(60)
                            break
                        except Exception:
                            continue
                    if removed:
                        break
                if removed:
                    break
            if not removed:
                break

    def _shipment_selected() -> bool:
        """
        Confirm Shipment is actually selected in the workflow multiselect.
        Avoid false positives from open dropdown suggestion rows.
        """
        # 1) Prefer explicit selected-token patterns used by SPS multiselect widgets.
        selected_token_checks = [
            "xpath=//*[contains(@id,'_tag-') and normalize-space()='Shipment']",
            "xpath=//*[contains(@class,'tag') and normalize-space()='Shipment']",
            "xpath=//*[contains(@class,'chip') and normalize-space()='Shipment']",
            "xpath=//*[contains(@class,'selected') and normalize-space()='Shipment']",
            "xpath=//*[contains(@class,'multi') and contains(@class,'value') and normalize-space()='Shipment']",
        ]
        for sel in selected_token_checks:
            for ctx in _contexts(page):
                loc = ctx.locator(sel)
                if loc.count() == 0:
                    continue
                try:
                    if loc.first.is_visible():
                        return True
                except Exception:
                    continue
        # Do NOT trust raw wrapper text/input value; it can include transient dropdown matches.
        return False

    def _type_and_choose_fast() -> bool:
        for ctx in _contexts(page):
            loc = ctx.locator(workflow_selector)
            if loc.count() == 0:
                continue
            try:
                fld = loc.first
                fld.wait_for(state="visible", timeout=1200)
                fld.click(timeout=600)
                fld.fill("Shipment", timeout=700)
                page.wait_for_timeout(60)
                # Explicitly choose from the suggestion list.
                if click_first_visible(
                    page,
                    [
                        "li[role='option']:has-text('Shipment')",
                        "[role='option']:has-text('Shipment')",
                        "xpath=//span[normalize-space()='Shipment']",
                    ],
                    timeout_ms=700,
                ):
                    return True
                # Keyboard fallback.
                fld.press("ArrowDown", timeout=500)
                fld.press("Enter", timeout=500)
                return True
            except Exception:
                continue
        return False

    def _open_workflow_picker() -> bool:
        return click_first_visible(
            page,
            [
                workflow_selector,
                "input[placeholder*='Workflow']",
                "xpath=//*[contains(normalize-space(.), 'Workflows Ready For')]/following::*[self::input or self::div][1]",
                "xpath=//*[contains(normalize-space(.), 'Select Workflow Document')][1]",
            ],
            timeout_ms=2_000,
        )

    def _type_shipment_into_field() -> bool:
        for ctx in _contexts(page):
            loc = ctx.locator(workflow_selector)
            if loc.count() == 0:
                continue
            try:
                fld = loc.first
                fld.wait_for(state="visible", timeout=1_200)
                try:
                    fld.click(timeout=800)
                except Exception:
                    fld.click(timeout=800, force=True)
                # Clear robustly so stale text doesn't block option matching.
                try:
                    fld.fill("", timeout=700)
                except Exception:
                    pass
                page.keyboard.press("Control+A")
                page.keyboard.press("Delete")
                fld.type("Shipment", delay=20)
                return True
            except Exception:
                continue
        return False

    def _pick_shipment_option() -> bool:
        # Try deterministic option click first.
        if click_first_visible(
            page,
            [
                "li[role='option']:has-text('Shipment')",
                "[role='option']:has-text('Shipment')",
                "xpath=//span[normalize-space()='Shipment']",
            ],
            timeout_ms=1_000,
        ):
            return True
        # Then keyboard selection as fallback.
        try:
            page.keyboard.press("ArrowDown")
            page.keyboard.press("Enter")
            return True
        except Exception:
            return False

    # Ensure previous workflow filters are cleared first.
    _clear_existing_workflow_tags()

    # Try a deterministic fast path first.
    if _type_and_choose_fast() and _shipment_selected():
        return

    # Robust retry loop: re-open picker, type Shipment, pick option, verify selected.
    for attempt in range(1, 6):
        clear_click_blockers(page)
        if not _open_workflow_picker():
            page.wait_for_timeout(200)
            continue
        page.wait_for_timeout(120)

        typed = _type_shipment_into_field()
        if not typed:
            # Last-resort typing into focused element.
            try:
                page.keyboard.type("Shipment", delay=25)
                typed = True
            except Exception:
                typed = False
        if not typed:
            page.wait_for_timeout(200)
            continue

        _pick_shipment_option()
        page.wait_for_timeout(160)

        # Clicking outside often commits multiselect chips in SPS widgets.
        try:
            page.keyboard.press("Tab")
        except Exception:
            pass
        page.wait_for_timeout(120)

        if _shipment_selected():
            print(f"Workflow filter confirmed as Shipment on attempt {attempt}.")
            return

        # Reset and retry: clear chips/tags that may have partially selected values.
        _clear_existing_workflow_tags()
        page.wait_for_timeout(120)

    raise RuntimeError("Could not reliably select 'Shipment' from Workflows Ready For list.")


def ensure_workflow_shipment_selected(page: Page, workflow_selector: str) -> None:
    """
    Final pre-search guard: ensure Shipment chip/tag exists before clicking Search.
    """
    # Reuse the same robust setter, then verify once more.
    set_workflow_ready_for_shipment(page, workflow_selector)

    checks = [
        "xpath=//*[contains(@id,'_tag-') and normalize-space()='Shipment']",
        "xpath=//*[contains(@class,'tag') and normalize-space()='Shipment']",
        "xpath=//*[contains(@class,'chip') and normalize-space()='Shipment']",
        "xpath=//*[contains(@class,'selected') and normalize-space()='Shipment']",
    ]
    for sel in checks:
        for ctx in _contexts(page):
            loc = ctx.locator(sel)
            if loc.count() == 0:
                continue
            try:
                if loc.first.is_visible():
                    return
            except Exception:
                continue
    raise RuntimeError("Shipment workflow was not selected right before Search.")


def click_advanced_search_button(page: Page) -> None:
    # Fast path on exact selector.
    for ctx in _contexts(page):
        loc = ctx.locator("button[data-testid='advSearchBottomSearchButton']")
        if loc.count() == 0:
            continue
        try:
            btn = loc.first
            btn.wait_for(state="visible", timeout=1200)
            btn.click(timeout=600)
            print("Clicked Search via data-testid fast path.")
            return
        except Exception:
            try:
                btn.click(timeout=600, force=True)
                print("Clicked Search via data-testid force path.")
                return
            except Exception:
                pass

    clear_click_blockers(page)

    selectors = [
        "button[data-testid='advSearchBottomSearchButton']",
        "button[data-testid='advSearchBottomSearchButton'][title='Search']",
        "button[type='submit'][title='Search']",
        "button:has-text('Search')",
    ]
    for sel in selectors:
        for ctx in _contexts(page):
            loc = ctx.locator(sel)
            if loc.count() == 0:
                continue
            try:
                btn = loc.first
                btn.wait_for(state="visible", timeout=2_500)
                try:
                    btn.click(timeout=1_200)
                except Exception:
                    clear_click_blockers(page)
                    btn.click(timeout=1_200, force=True)
                print(f"Clicked Search via fallback selector: {sel}")
                return
            except Exception:
                continue
    raise RuntimeError("Could not click Advanced Search 'Search' button.")


def _open_next_tracked_order_from_results(
    page: Page,
    tracking_by_po: dict[str, str],
    processed_po: set[str],
) -> tuple[str, str] | None:
    """
    Scan POs from transactions results first, then cross-check each PO in CSV map.
    Opens the first matching, unprocessed PO and returns (po, tracking).
    """
    max_pages = 80
    for page_idx in range(1, max_pages + 1):
        clear_click_blockers(page)
        wait_for_order_link_count(page, timeout_ms=45_000)
        for ctx in _contexts(page):
            links = ctx.locator("a.text-truncate[href*='/fulfillment/transactions/document/']")
            if links.count() == 0:
                links = ctx.locator("a[href*='/fulfillment/transactions/document/']")
            for i in range(links.count()):
                link = links.nth(i)
                try:
                    if not link.is_visible():
                        continue
                except Exception:
                    continue
                try:
                    po = normalize_po(link.inner_text().strip())
                except Exception:
                    po = ""
                if not po or po in processed_po:
                    continue
                tracking = tracking_by_po.get(po)
                if not tracking:
                    continue
                row = link.locator("xpath=ancestor::tr[1]")
                if row.count() == 0:
                    row = link.locator("xpath=ancestor::*[@role='row'][1]")
                if row.count() > 0:
                    try:
                        row_text = row.inner_text().strip()
                        if re.search(r"\bopen\b", row_text, flags=re.I) is None:
                            continue
                    except Exception:
                        pass
                try:
                    link.scroll_into_view_if_needed()
                except Exception:
                    pass
                try:
                    link.click(timeout=2500)
                except Exception:
                    try:
                        link.evaluate(
                            "(el) => { el.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true})); }"
                        )
                    except Exception:
                        link.click(timeout=2500, force=True)
                page.wait_for_load_state("domcontentloaded")
                page.wait_for_timeout(500)
                return po, tracking
        if not _go_next_results_page(page):
            break
        print(f"No CSV-tracked PO on results page {page_idx}; checking next page...")
    return None


def _click_workflow_new(page: Page, workflow_name: str) -> None:
    clear_click_blockers(page)
    # Prefer workflow-rail-local "New" (e.g., Billing -> New) to avoid clicking wrong section.
    selectors = [
        f"xpath=//*[contains(@class,'workflow') or @data-testid='workflow']//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{workflow_name.lower()}')]/following::button[@data-testid='createNewBtn' and @title='New'][1]",
        f"xpath=//*[normalize-space()='{workflow_name}']/following::button[@data-testid='createNewBtn' and @title='New'][1]",
        f"xpath=//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{workflow_name.lower()}')]/following::button[@data-testid='createNewBtn' and @title='New'][1]",
        "button[data-testid='createNewBtn'][title='New']",
    ]

    for sel in selectors:
        for ctx in _contexts(page):
            try:
                loc = ctx.locator(sel)
                if loc.count() == 0:
                    continue
                btn = loc.first
                btn.wait_for(state="visible", timeout=6_000)
                try:
                    btn.scroll_into_view_if_needed()
                except Exception:
                    pass
                try:
                    btn.click(timeout=2_000)
                except Exception:
                    try:
                        btn.evaluate(
                            "(el) => { el.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true})); }"
                        )
                    except Exception:
                        btn.click(timeout=2_000, force=True)
                return
            except Exception:
                continue
    raise RuntimeError(f"Could not click Workflow '{workflow_name}' New button.")


def _wait_for_asn_document_ready(page: Page, timeout_ms: int = 60_000) -> bool:
    """Wait until we're on a loaded ASN document page where Billing->New should exist."""
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        url = (page.url or "").lower()
        if "/fulfillment/transactions/document/" in url:
            for sel in (
                "text=/advance\\s+ship\\s+notice/i",
                "text=/asn/i",
                "xpath=//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'billing')]",
                "button[data-testid='createNewBtn'][title='New']",
            ):
                for ctx in _contexts(page):
                    try:
                        loc = ctx.locator(sel)
                        if loc.count() > 0 and loc.first.is_visible():
                            return True
                    except Exception:
                        continue
        page.wait_for_timeout(300)
    return False


def _wait_for_invoice_modal_ready(page: Page, timeout_ms: int = 20_000) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        for sel in (
            "text=Invoice from ASN",
            "label.sps-checkable__label:has-text('Invoice from ASN')",
            "button[data-testid='modalOkBtn'][title='Create New']",
        ):
            for ctx in _contexts(page):
                try:
                    loc = ctx.locator(sel)
                    if loc.count() > 0 and loc.first.is_visible():
                        return True
                except Exception:
                    continue
        page.wait_for_timeout(220)
    return False


def _create_asn_for_open_order(page: Page, tracking: str, *, submit: bool) -> bool:
    _click_workflow_new(page, "Shipment")
    if not click_first_visible(
        page,
        [
            "button[data-testid='modalOkBtn'][title='Create New']",
            "button[data-testid='modalOkBtn']:has-text('Create New')",
        ],
        timeout_ms=10_000,
    ):
        raise RuntimeError("Could not click Create New for Shipment.")
    wait_for_asn_form_ready(page, timeout_ms=90_000)
    fill_asn_date(page)

    tracking_filled = False
    for ctx in _contexts(page):
        inputs = ctx.locator("input[data-testid*='trackingNumber-input__input'], input[aria-label='Carrier Tracking #']")
        for i in range(inputs.count()):
            inp = inputs.nth(i)
            try:
                if not inp.is_visible():
                    continue
            except Exception:
                continue
            if _fill_tracking_input(inp, tracking):
                tracking_filled = True
                break
        if tracking_filled:
            break
    if not tracking_filled:
        raise RuntimeError("Could not fill ASN tracking input for current order.")

    if submit:
        send_documents(page)
        # Ensure post-send ASN document page is actually ready before invoice step.
        if not _wait_for_asn_document_ready(page, timeout_ms=70_000):
            raise RuntimeError("ASN send completed but ASN document page did not become ready.")
    else:
        print("Dry run: ASN created/filled but not sent (--submit not set).")
    return True


def _create_invoice_from_asn_for_open_order(page: Page, *, submit: bool) -> bool:
    if not _wait_for_asn_document_ready(page, timeout_ms=70_000):
        raise RuntimeError("ASN page not ready for Billing -> New invoice creation.")

    # Billing->New can be flaky/overlaid; retry until invoice modal appears.
    opened_modal = False
    for _ in range(5):
        try:
            _click_workflow_new(page, "Billing")
        except Exception:
            pass
        page.wait_for_timeout(400)
        if _wait_for_invoice_modal_ready(page, timeout_ms=4_000):
            opened_modal = True
            break
    if not opened_modal:
        raise RuntimeError("Could not open Billing New modal for Invoice from ASN.")

    click_first_visible(
        page,
        [
            "label.sps-checkable__label:has-text('Invoice from ASN')",
            "text=Invoice from ASN",
            "xpath=//*[contains(normalize-space(.), 'Invoice from ASN')]",
        ],
        timeout_ms=8_000,
    )
    if not click_first_visible(
        page,
        [
            "button[data-testid='modalOkBtn'][title='Create New']",
            "button[data-testid='modalOkBtn']:has-text('Create New')",
        ],
        timeout_ms=10_000,
    ):
        raise RuntimeError("Could not click Create New for Invoice from ASN.")

    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(1000)
    if submit:
        send_documents(page)
    else:
        print("Dry run: Invoice from ASN opened but not sent (--submit not set).")
    return True


def process_orders_individually(page: Page, tracking_by_po: dict[str, str], *, submit: bool) -> tuple[int, int]:
    """
    Per-PO flow:
    Transactions + Advanced Search (Shipment) -> open PO -> create/send ASN ->
    create/send Invoice-from-ASN -> return to transactions for next PO.
    """
    attempted = 0
    completed = 0
    processed_po: set[str] = set()
    max_iterations = max(1, len(tracking_by_po))
    for _ in range(max_iterations):
        open_ready_for_shipment(page)
        picked = _open_next_tracked_order_from_results(page, tracking_by_po, processed_po)
        if picked is None:
            print("No additional Shipment/Open POs on transactions pages matched CSV tracking.")
            break
        po, tracking = picked
        attempted += 1
        processed_po.add(po)
        print(f"\n=== PO {po}: matched from transactions list -> CSV tracking; processing ===")
        try:
            _create_asn_for_open_order(page, tracking, submit=submit)
            _create_invoice_from_asn_for_open_order(page, submit=submit)
            completed += 1
            print(f"PO {po}: completed.")
        except Exception as ex:
            print(f"PO {po}: failed ({ex}); continuing to next PO.")
            continue
    return completed, attempted


def _count_doc_id_cells(page: Page) -> int:
    n = 0
    for ctx in _contexts(page):
        n += ctx.locator("td[data-testid='doc-id__cell'], td[role='cell'][data-testid='doc-id__cell']").count()
    return n


def wait_for_order_link_count(page: Page, *, timeout_ms: int = 120_000) -> int:
    """Wait for SPS document-id cells (PO column) after search."""
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        clear_click_blockers(page)
        n = _count_doc_id_cells(page)
        if n > 0:
            for ctx in _contexts(page):
                loc = ctx.locator("td[data-testid='doc-id__cell'], td[role='cell'][data-testid='doc-id__cell']")
                if loc.count() == 0:
                    continue
                try:
                    loc.first.wait_for(state="attached", timeout=3_000)
                    return n
                except Exception:
                    continue
        page.wait_for_timeout(200)
    return _count_doc_id_cells(page)


def _po_from_doc_id_cell(cell) -> str:
    """PO from td title, inner document link, or cell text."""
    try:
        title = (cell.get_attribute("title") or "").strip()
        if title:
            po = normalize_po(title)
            if len(po) >= 9:
                return po
    except Exception:
        pass
    try:
        link = cell.locator("a.text-truncate[href*='/fulfillment/transactions/document/']").first
        if link.count() > 0:
            po = normalize_po(link.inner_text().strip())
            if len(po) >= 9:
                return po
    except Exception:
        pass
    try:
        po = normalize_po(cell.inner_text().strip())
        if len(po) >= 9:
            return po
    except Exception:
        pass
    return ""


@dataclass
class SelectionStats:
    rows_seen: int = 0
    rows_matched: int = 0
    rows_checked: int = 0
    open_rows: int = 0


def _go_next_results_page(page: Page) -> bool:
    """Click Next Page if enabled. Selections on prior pages remain checked."""
    clear_click_blockers(page)
    for ctx in _contexts(page):
        btn = ctx.locator("button[title='Next Page']:has(i.sps-icon-chevron-right), button[title='Next Page']").first
        if btn.count() == 0:
            continue
        try:
            if not btn.is_enabled():
                return False
            if (btn.get_attribute("disabled") or "").strip():
                return False
            if (btn.get_attribute("aria-disabled") or "").strip().lower() == "true":
                return False
        except Exception:
            return False
        try:
            btn.click(timeout=2000, force=True)
        except Exception:
            try:
                btn.click(timeout=2000)
            except Exception:
                return False
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(800)
        print("Clicked Next Page on results list.")
        return True
    return False


def _select_orders_with_tracking_on_current_page(
    page: Page, tracking_by_po: dict[str, str], seen_po: set[str]
) -> SelectionStats:
    stats = SelectionStats()
    clear_click_blockers(page)
    wait_for_order_link_count(page, timeout_ms=45_000)
    total = _count_doc_id_cells(page)
    stats.rows_seen = total
    print(f"  Doc-id cells on this page: {total}")

    for ctx in _contexts(page):
        cells = ctx.locator("td[data-testid='doc-id__cell'], td[role='cell'][data-testid='doc-id__cell']")
        for i in range(cells.count()):
            clear_click_blockers(page)
            cell = cells.nth(i)
            try:
                cell.scroll_into_view_if_needed()
            except Exception:
                pass
            try:
                if not cell.is_visible():
                    continue
            except Exception:
                continue

            po = _po_from_doc_id_cell(cell)
            if not po:
                continue

            row = cell.locator("xpath=ancestor::tr[1]")
            if row.count() == 0:
                row = cell.locator("xpath=ancestor::*[@role='row'][1]")
            if row.count() == 0:
                continue
            try:
                row_text = row.inner_text().strip()
            except Exception:
                continue

            status_is_open = False
            try:
                status_is_open = row.locator("td").filter(has_text=re.compile(r"^\s*Open\s*$", re.I)).count() > 0
            except Exception:
                status_is_open = False
            if not status_is_open:
                status_is_open = re.search(r"\bopen\b", row_text, flags=re.I) is not None
            if not status_is_open:
                continue

            stats.open_rows += 1

            tracking = tracking_by_po.get(po)
            if not tracking:
                continue
            if po in seen_po:
                continue

            seen_po.add(po)
            stats.rows_matched += 1

            checkbox_inputs = row.locator("input[type='checkbox']")
            checkbox_labels = row.locator("label.sps-checkable__label")
            try:
                if checkbox_inputs.count() > 0:
                    inp = checkbox_inputs.first
                    if not inp.is_checked():
                        if checkbox_labels.count() > 0:
                            try:
                                checkbox_labels.first.click(timeout=1000)
                            except Exception:
                                clear_click_blockers(page)
                                checkbox_labels.first.click(timeout=1000, force=True)
                        else:
                            try:
                                inp.check(timeout=1000)
                            except Exception:
                                clear_click_blockers(page)
                                inp.click(timeout=1000, force=True)
                    stats.rows_checked += 1
                elif checkbox_labels.count() > 0:
                    try:
                        checkbox_labels.first.click(timeout=1000)
                    except Exception:
                        clear_click_blockers(page)
                        checkbox_labels.first.click(timeout=1000, force=True)
                    stats.rows_checked += 1
            except Exception as ex:
                print(f"Could not select checkbox for PO {po}: {ex}")

    return stats


def select_orders_with_tracking(page: Page, tracking_by_po: dict[str, str]) -> SelectionStats:
    """Paginate results: process each page, then Next Page until disabled."""
    seen_po: set[str] = set()
    agg = SelectionStats()
    max_pages = 80
    for page_idx in range(1, max_pages + 1):
        print(f"STEP 2.{page_idx}: Scan Open rows on results page {page_idx}...")
        clear_click_blockers(page)
        part = _select_orders_with_tracking_on_current_page(page, tracking_by_po, seen_po)
        agg.rows_seen += part.rows_seen
        agg.rows_matched += part.rows_matched
        agg.rows_checked += part.rows_checked
        agg.open_rows += part.open_rows
        print(
            f"  Page {page_idx} summary: doc_cells={part.rows_seen}, open_rows={part.open_rows}, "
            f"matched={part.rows_matched}, checked={part.rows_checked}"
        )
        if not _go_next_results_page(page):
            print(f"STEP 2 done after {page_idx} page(s).")
            break

    print(
        f"PO match results (all pages): open_rows={agg.open_rows}, matched={agg.rows_matched}, "
        f"checked={agg.rows_checked}, doc_cells_seen_total={agg.rows_seen}, unique_po_checked={len(seen_po)}"
    )
    if agg.rows_seen == 0:
        print(f"Current URL (no doc-id cells found): {page.url}")
    return agg


def set_results_per_page_100(page: Page) -> None:
    """Expand list size so we don't miss rows on page 2+."""
    def _current_page_size_value() -> str:
        # Exact SPS controls from provided DOM.
        for ctx in _contexts(page):
            try:
                v = ctx.locator("span[data-testid='undefined-value']").first
                if v.count() > 0 and v.is_visible():
                    txt = (v.inner_text() or "").strip()
                    if txt:
                        return txt
            except Exception:
                pass
            try:
                c = ctx.locator("div[data-testid='undefined-dropctrl']").first
                if c.count() > 0 and c.is_visible():
                    t = (c.get_attribute("title") or "").strip()
                    if t:
                        return t
            except Exception:
                pass
        return ""

    def _pick_100_from_open_menu() -> bool:
        # Try explicit SPS option-id pattern first.
        if click_first_visible(
            page,
            [
                "a[role='option'][id*='options-option-2']:has-text('100')",
                "a.sps-option-list__option[role='option']:has-text('100')",
                "a[data-testid*='option-list-option']:has-text('100')",
                "li[id*='options-option']:has-text('100')",
                "div[id*='options-option']:has-text('100')",
                "[role='option']:has-text('100')",
                "xpath=//*[contains(@id,'options-option') and normalize-space()='100']",
                "xpath=//*[@role='option' and normalize-space()='100']",
            ],
            timeout_ms=2500,
        ):
            return True
        # Explicit forced-click pass for overlay-heavy SPS menus.
        for ctx in _contexts(page):
            loc = ctx.locator("a[role='option'][id*='options-option']:has-text('100')")
            if loc.count() == 0:
                continue
            try:
                loc.first.click(timeout=1200, force=True)
                return True
            except Exception:
                continue
        # Keyboard fallback for opened dropdown.
        try:
            page.keyboard.press("End")
            page.keyboard.press("Enter")
            return True
        except Exception:
            return False

    for attempt in range(1, 4):
        current = _current_page_size_value()
        if current == "100":
            print("Results-per-page already 100 (verified).")
            return
        clear_click_blockers(page)
        opened = False
        # Deterministic path: iterate SPS listboxes and use their own dropctrl.
        for ctx in _contexts(page):
            listboxes = ctx.locator("div[role='listbox'].sps-select")
            for i in range(min(listboxes.count(), 8)):
                lb = listboxes.nth(i)
                try:
                    if not lb.is_visible():
                        continue
                    ctrl = lb.locator("div[data-testid='undefined-dropctrl'], div.sps-select__dropctrl").first
                    if ctrl.count() == 0:
                        continue
                    try:
                        v = (ctrl.get_attribute("title") or "").strip()
                    except Exception:
                        v = ""
                    # Prioritize controls that look like page-size selectors.
                    if v not in ("25", "50", "100", ""):
                        continue
                    try:
                        ctrl.click(timeout=1200)
                    except Exception:
                        ctrl.click(timeout=1200, force=True)
                    opened = True
                    break
                except Exception:
                    continue
            if opened:
                break
        if not opened:
            opened = click_first_visible(
                page,
                [
                    "div[data-testid='undefined-dropctrl']",
                    "div.sps-select__dropctrl",
                    "i.sps-icon.sps-icon-chevron-down.sps-select__dropctrl-icon",
                    "[class*='sps-select__dropctrl']",
                ],
                timeout_ms=2000,
            )
        if not opened:
            continue

        clear_click_blockers(page)
        picked = _pick_100_from_open_menu()

        page.wait_for_timeout(400)
        now = _current_page_size_value()
        if picked and now == "100":
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(400)
            print("Set results-per-page to 100 (verified).")
            return
        print(f"Attempt {attempt}: page-size currently '{now or 'unknown'}'")

    print("WARN: Could not verify 100 results-per-page; continuing with current size.")


def open_create_new_asn(page: Page) -> None:
    clear_click_blockers(page)
    if not click_first_visible(
        page,
        [
            "button:has(i.sps-icon-ellipses)",
            "[role='button']:has(i.sps-icon-ellipses)",
            "xpath=//*[contains(@class,'sps-icon-ellipses')]/ancestor::*[self::button or @role='button'][1]",
        ],
        timeout_ms=10000,
    ):
        raise RuntimeError("Could not click bottom ellipses menu.")

    if not click_first_visible(
        page,
        [
            "span:has-text('Create New')",
            "button:has-text('Create New')",
            "[role='menuitem']:has-text('Create New')",
        ],
        timeout_ms=10000,
    ):
        raise RuntimeError("Could not click 'Create New' from actions menu.")

    # Advance Ship Notice (exact SPS test id + label fallback).
    asn_radio = page.locator("input[data-testid='createNewDocSelectPartnerForm24961__radio-input']").first
    try:
        asn_radio.wait_for(state="attached", timeout=6000)
    except Exception:
        pass
    if asn_radio.count() > 0:
        try:
            if not asn_radio.is_checked():
                asn_radio.check(timeout=2000)
        except Exception:
            click_first_visible(
                page,
                [
                    "label.sps-checkable__label:has-text('Advance Ship Notice')",
                    "text=Advance Ship Notice",
                ],
                timeout_ms=3000,
            )
    elif not click_first_visible(
        page,
        [
            "label.sps-checkable__label:has-text('Advance Ship Notice')",
            "text=Advance Ship Notice",
        ],
        timeout_ms=10000,
    ):
        raise RuntimeError("Could not choose 'Advance Ship Notice'.")

    # Auto Fill - Recommended (ensure selected).
    auto_fill_radio = page.locator(
        "input[data-testid='createNewDocSelectCompletionMethodquick_entry__radio-input']"
    ).first
    try:
        auto_fill_radio.wait_for(state="attached", timeout=4000)
    except Exception:
        pass
    auto_fill_selected = False
    if auto_fill_radio.count() > 0:
        try:
            auto_fill_selected = auto_fill_radio.is_checked() or (
                (auto_fill_radio.get_attribute("data-checked") or "").strip().lower() == "checked"
            )
        except Exception:
            auto_fill_selected = False
    if not auto_fill_selected:
        clicked_auto = click_first_visible(
            page,
            [
                "label.sps-checkable__label:has-text('Auto Fill - Recommended')",
                "text=Auto Fill - Recommended",
            ],
            timeout_ms=3000,
        )
        if not clicked_auto and auto_fill_radio.count() > 0:
            try:
                auto_fill_radio.check(timeout=2000)
                clicked_auto = True
            except Exception:
                clicked_auto = False
        if not clicked_auto:
            raise RuntimeError("Could not select 'Auto Fill - Recommended'.")

    if not click_first_visible(
        page,
        [
            "div.sps-button.sps-button--confirm button[data-testid='modalOkBtn'][title='Create New']",
            "button[data-testid='modalOkBtn'][title='Create New']",
            "button[data-testid='modalOkBtn']:has-text('Create New')",
        ],
        timeout_ms=10000,
    ):
        raise RuntimeError("Could not click modal 'Create New'.")
    clear_click_blockers(page)


def fill_asn_date(page: Page) -> None:
    date_text = datetime.now().strftime("%m/%d/%Y")
    clear_click_blockers(page)
    date_input = None
    for ctx in _contexts(page):
        loc = ctx.locator("[data-testid='asn.header.shipment.shippedDate-input_date_input']")
        if loc.count() == 0:
            continue
        date_input = loc
        break
    if date_input is None:
        raise RuntimeError("ASN shipped date input not found.")
    date_input.first.wait_for(state="visible", timeout=60000)
    try:
        date_input.first.click(timeout=2000)
        date_input.first.fill(date_text)
    except Exception:
        clear_click_blockers(page)
        date_input.first.click(timeout=2000, force=True)
        date_input.first.fill(date_text)
    print(f"Set ASN shipped date to {date_text}")


def wait_for_asn_form_ready(page: Page, timeout_ms: int = 90_000) -> None:
    """
    Wait for ASN form controls before attempting date/tracking entry.
    Avoids racing immediately after modal Create New navigation.
    """
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        clear_click_blockers(page)
        for ctx in _contexts(page):
            try:
                ship_date = ctx.locator("[data-testid='asn.header.shipment.shippedDate-input_date_input']").first
                if ship_date.count() > 0 and ship_date.is_visible():
                    return
            except Exception:
                pass
            try:
                # Fallback signal: any ASN order tracking input is attached.
                any_tracking = ctx.locator("input[data-testid*='trackingNumber-input__input']").first
                if any_tracking.count() > 0:
                    return
            except Exception:
                pass
        page.wait_for_timeout(150)
    raise RuntimeError("ASN page did not become ready for ship date / tracking entry in time.")


def _click_asn_order_tab(page: Page, order_idx: int) -> None:
    """For multi-SKU cards, switch from Header -> Order tab for the given ASN row."""
    for ctx in _contexts(page):
        tab = ctx.locator("[data-testid='tab-asn_order']").nth(order_idx)
        if tab.count() == 0:
            continue
        try:
            tab.scroll_into_view_if_needed()
        except Exception:
            pass
        try:
            tab.click(timeout=2000)
            page.wait_for_timeout(200)
            return
        except Exception:
            clear_click_blockers(page)
            try:
                tab.click(timeout=2000, force=True)
                page.wait_for_timeout(200)
                return
            except Exception:
                continue


def _fill_tracking_input(input_loc, tracking: str) -> bool:
    def _looks_filled() -> bool:
        try:
            v = (input_loc.input_value() or "").strip()
            return bool(v)
        except Exception:
            return False

    try:
        input_loc.click(timeout=1200)
        input_loc.fill("")
        input_loc.type(tracking, delay=10)
        if _looks_filled():
            return True
        input_loc.fill(tracking)
        return _looks_filled()
    except Exception:
        try:
            input_loc.click(timeout=1200, force=True)
            input_loc.fill("")
            input_loc.type(tracking, delay=10)
            if _looks_filled():
                return True
            input_loc.fill(tracking)
            if _looks_filled():
                return True
            try:
                # Last resort if input handlers are flaky.
                input_loc.evaluate(
                    "(el, v) => { el.value = v; el.dispatchEvent(new Event('input', {bubbles:true})); el.dispatchEvent(new Event('change', {bubbles:true})); }",
                    tracking,
                )
                return _looks_filled()
            except Exception:
                return False
        except Exception:
            return False


def _fill_pack_pages_for_order(page: Page, order_idx: int, tracking: str) -> int:
    """
    Fill tracking for pack pages in a single ASN order.
    Handles both single-SKU (packInfo.0 only) and multi-SKU cards (packInfo.0..N).
    """
    filled_pages = 0
    visited_pack_indexes: set[int] = set()
    max_pack_pages = 30

    # First fill any currently attached pack inputs for this order.
    attached = None
    for ctx in _contexts(page):
        loc = ctx.locator(
            f"input[data-testid^='asn.order.{order_idx}.packInfo.'][data-testid$='trackingNumber-input__input']"
        )
        if loc.count() > 0:
            attached = loc
            break
    if attached is None:
        attached = page.locator(
            f"input[data-testid^='asn.order.{order_idx}.packInfo.'][data-testid$='trackingNumber-input__input']"
        )
    for i in range(attached.count()):
        inp = attached.nth(i)
        testid = inp.get_attribute("data-testid") or ""
        m = re.search(rf"^asn\.order\.{order_idx}\.packInfo\.(\d+)\.trackingNumber-input__input$", testid)
        pack_idx = int(m.group(1)) if m else i
        if pack_idx in visited_pack_indexes:
            continue
        if _fill_tracking_input(inp, tracking):
            visited_pack_indexes.add(pack_idx)
            filled_pages += 1

    # Then walk multi-SKU internal pack pages via next button and fill newly visible page inputs.
    for _ in range(max_pack_pages):
        next_idx = (max(visited_pack_indexes) + 1) if visited_pack_indexes else 1
        target_next = None
        for ctx in _contexts(page):
            loc = ctx.locator(
                f"input[data-testid='asn.order.{order_idx}.packInfo.{next_idx}.trackingNumber-input__input']"
            )
            if loc.count() > 0:
                target_next = loc
                break
        if target_next is None:
            target_next = page.locator(
                f"input[data-testid='asn.order.{order_idx}.packInfo.{next_idx}.trackingNumber-input__input']"
            )
        if target_next.count() > 0 and next_idx not in visited_pack_indexes:
            if _fill_tracking_input(target_next.first, tracking):
                visited_pack_indexes.add(next_idx)
                filled_pages += 1
                continue

        # If next index is not attached yet, try paging forward inside the ASN order card.
        next_btns = page.locator("button[aria-label='Go to Next Page'][title='Go to Next Page']")
        moved = False
        for b in range(next_btns.count()):
            btn = next_btns.nth(b)
            try:
                if not btn.is_visible() or not btn.is_enabled():
                    continue
            except Exception:
                continue
            try:
                btn.click(timeout=1200)
            except Exception:
                try:
                    btn.click(timeout=1200, force=True)
                except Exception:
                    continue
            page.wait_for_timeout(220)
            moved = True
            break

        if not moved:
            break

        # After paging, fill any newly exposed input for this order.
        newly_filled = False
        now = None
        for ctx in _contexts(page):
            loc = ctx.locator(
                f"input[data-testid^='asn.order.{order_idx}.packInfo.'][data-testid$='trackingNumber-input__input']"
            )
            if loc.count() > 0:
                now = loc
                break
        if now is None:
            now = page.locator(
                f"input[data-testid^='asn.order.{order_idx}.packInfo.'][data-testid$='trackingNumber-input__input']"
            )
        for i in range(now.count()):
            inp = now.nth(i)
            testid = inp.get_attribute("data-testid") or ""
            m = re.search(rf"^asn\.order\.{order_idx}\.packInfo\.(\d+)\.trackingNumber-input__input$", testid)
            pack_idx = int(m.group(1)) if m else i
            if pack_idx in visited_pack_indexes:
                continue
            if _fill_tracking_input(inp, tracking):
                visited_pack_indexes.add(pack_idx)
                filled_pages += 1
                newly_filled = True
        if not newly_filled:
            break

    return filled_pages


def _fill_ship_date_for_card(card, date_text: str) -> bool:
    date_inputs = card.locator(
        "input[data-testid='asn.header.shipment.shippedDate-input_date_input'], "
        "input[placeholder='MM/DD/YYYY']"
    )
    if date_inputs.count() == 0:
        return False
    for i in range(date_inputs.count()):
        inp = date_inputs.nth(i)
        try:
            if not inp.is_visible():
                continue
        except Exception:
            continue
        try:
            inp.click(timeout=1200)
            inp.fill(date_text)
            return True
        except Exception:
            try:
                inp.click(timeout=1200, force=True)
                inp.fill(date_text)
                return True
            except Exception:
                continue
    return False


def _fill_ship_date_by_index(ctx, order_idx: int, date_text: str) -> bool:
    """
    SPS ASN often renders repeated ship-date testids; the row index maps to order card index.
    """
    try:
        inp = ctx.locator("input[data-testid='asn.header.shipment.shippedDate-input_date_input']").nth(order_idx)
        if inp.count() == 0:
            return False
        inp.wait_for(state="visible", timeout=1500)
        try:
            inp.click(timeout=1200)
            inp.fill(date_text)
        except Exception:
            inp.click(timeout=1200, force=True)
            inp.fill(date_text)
        return True
    except Exception:
        return False


def _fill_tracking_for_order_index(ctx, order_idx: int, tracking: str) -> int:
    """
    Deterministic SPS path: asn.order.<idx>.packInfo.<n>.trackingNumber-input__input
    """
    filled = 0
    seen: set[int] = set()
    max_pack = 30

    # Open Order tab for this indexed card when present.
    order_tab_selected = False
    for sel in (
        "[data-testid='tab-asn_order']",
        "div[role='tab'][data-key='order']",
        "div[role='tab']:has-text('Order')",
    ):
        try:
            tab = ctx.locator(sel).nth(order_idx)
            if tab.count() == 0:
                continue
            try:
                tab.click(timeout=1200)
            except Exception:
                tab.click(timeout=1200, force=True)
            order_tab_selected = True
            break
        except Exception:
            continue
    # Extra fallback: try global page tabs by index.
    try:
        page_tabs = ctx.page.locator("[data-testid='tab-asn_order'], div[role='tab'][data-key='order']")
        if page_tabs.count() > order_idx:
            t = page_tabs.nth(order_idx)
            try:
                t.click(timeout=1200)
            except Exception:
                t.click(timeout=1200, force=True)
            order_tab_selected = True
    except Exception:
        pass
    if order_tab_selected:
        ctx.page.wait_for_timeout(220)

    for pack_idx in range(max_pack):
        inp = ctx.locator(
            f"input[data-testid='asn.order.{order_idx}.packInfo.{pack_idx}.trackingNumber-input__input']"
        )
        if inp.count() == 0:
            # Try to page to the next pack section for multi-SKU cards.
            moved = False
            for next_sel in (
                "button[aria-label='Go to Next Page'][title='Go to Next Page']",
                "button[aria-label='Go to Next Page']",
                "button[title='Go to Next Page']",
            ):
                try:
                    next_btn = ctx.locator(next_sel).first
                    if next_btn.count() == 0:
                        continue
                    if not (next_btn.is_visible() and next_btn.is_enabled()):
                        continue
                    try:
                        next_btn.click(timeout=1200)
                    except Exception:
                        next_btn.click(timeout=1200, force=True)
                    ctx.page.wait_for_timeout(220)
                    moved = True
                    break
                except Exception:
                    continue
            if moved:
                inp = ctx.locator(
                    f"input[data-testid='asn.order.{order_idx}.packInfo.{pack_idx}.trackingNumber-input__input']"
                )
            if inp.count() == 0:
                if pack_idx > 0:
                    break
                continue
        if pack_idx in seen:
            continue
        if _fill_tracking_input(inp.first, tracking):
            seen.add(pack_idx)
            filled += 1

    return filled


def _fill_tracking_for_card(card, tracking: str) -> int:
    """
    Fill tracking in one ASN card (single-SKU or multi-SKU).
    Walks internal pack-page next arrow when present.
    """
    filled = 0
    seen_keys: set[str] = set()
    max_steps = 30

    # Open Order tab for multi-SKU cards when available.
    panel_ctx = card
    order_tab = card.locator("[role='tab'][data-key='order']").first
    if order_tab.count() == 0:
        # Fallback from inner label to tab container.
        inner = card.locator("[data-testid='tab-asn_order']").first
        if inner.count() > 0:
            parent_tab = inner.locator("xpath=ancestor::*[@role='tab' and @data-key='order'][1]").first
            if parent_tab.count() > 0:
                order_tab = parent_tab
    if order_tab.count() > 0:
        try:
            # JS click helps when the tab is visually present but covered by style layers.
            order_tab.evaluate("el => el.click()")
        except Exception:
            try:
                order_tab.click(timeout=1600, force=True)
            except Exception:
                pass
        card.page.wait_for_timeout(260)
        try:
            panel_id = (order_tab.get_attribute("aria-controls") or "").strip()
        except Exception:
            panel_id = ""
        if panel_id:
            panel = card.page.locator(f"[id='{panel_id}']").first
            if panel.count() > 0:
                panel_ctx = panel

    for _ in range(max_steps):
        inputs = panel_ctx.locator(
            "input[data-testid*='trackingNumber-input__input'], input[aria-label='Carrier Tracking #']"
        )
        new_fill = False
        for i in range(inputs.count()):
            inp = inputs.nth(i)
            try:
                if not inp.is_visible():
                    continue
            except Exception:
                continue
            key = (inp.get_attribute("data-testid") or "") or (inp.get_attribute("id") or f"idx:{i}")
            if key in seen_keys:
                continue
            if _fill_tracking_input(inp, tracking):
                seen_keys.add(key)
                filled += 1
                new_fill = True

        # Click the "next page" that belongs to the tracking/pack section.
        moved = False
        next_candidates = []
        if inputs.count() > 0:
            anchor = inputs.first
            near = anchor.locator(
                "xpath=ancestor::*[.//input[@aria-label='Carrier Tracking #']][1]//button[@aria-label='Go to Next Page']"
            )
            next_candidates.append(near)
        next_candidates.append(panel_ctx.locator("button[aria-label='Go to Next Page'][title='Go to Next Page']"))
        next_candidates.append(panel_ctx.locator("button[aria-label='Go to Next Page']"))
        for cand in next_candidates:
            try:
                for j in range(cand.count()):
                    btn = cand.nth(j)
                    try:
                        if not btn.is_visible() or not btn.is_enabled():
                            continue
                    except Exception:
                        continue
                    try:
                        btn.click(timeout=1200)
                    except Exception:
                        try:
                            btn.evaluate("el => el.click()")
                        except Exception:
                            btn.click(timeout=1200, force=True)
                    card.page.wait_for_timeout(260)
                    moved = True
                    break
                if moved:
                    break
            except Exception:
                continue
        if not moved:
            break
        if not new_fill:
            # If we moved but didn't fill anything in this iteration, continue one more
            # cycle to allow the newly paged input to attach.
            card.page.wait_for_timeout(180)

    return filled


def fill_tracking_on_asn(page: Page, tracking_by_po: dict[str, str]) -> tuple[int, int, set[str]]:
    clear_click_blockers(page)
    date_text = datetime.now().strftime("%m/%d/%Y")
    total = 0
    filled_rows = 0
    filled_po: set[str] = set()
    seen_po: set[str] = set()

    for ctx in _contexts(page):
        po_links = ctx.locator("[data-testid='sourceDocumentLink'] a[href*='/fulfillment/transactions/document/']")
        if po_links.count() == 0:
            po_links = ctx.locator(
                "a.text-truncate.d-block[href*='/fulfillment/transactions/document/'], "
                "a[href*='/fulfillment/transactions/document/']"
            )
        for i in range(po_links.count()):
            clear_click_blockers(page)
            link = po_links.nth(i)
            try:
                po_raw = link.inner_text().strip()
            except Exception:
                continue
            po = normalize_po(po_raw)
            if not po or po in seen_po:
                continue
            seen_po.add(po)
            total += 1

            tracking = tracking_by_po.get(po)
            if not tracking:
                print(f"ASN row {total - 1}: no tracking for PO {po}")
                continue

            # Scope all interactions to this PO's card to avoid row-index mismatches.
            card = link.locator(
                "xpath=ancestor::*["
                ".//*[@data-testid='sourceDocumentLink'] and "
                "("
                ".//*[@data-testid='tab-asn_order'] "
                "or .//input[contains(@data-testid,'trackingNumber-input__input')] "
                "or .//input[@aria-label='Carrier Tracking #'] "
                "or .//input[@data-testid='asn.header.shipment.shippedDate-input_date_input']"
                ")"
                "][1]"
            ).first
            if card.count() == 0:
                card = link.locator(
                    "xpath=ancestor::div[.//*[@data-testid='tab-asn_order'] "
                    "or .//input[contains(@data-testid,'trackingNumber-input__input')] "
                    "or .//input[@aria-label='Carrier Tracking #']][1]"
                ).first
            if card.count() == 0:
                print(f"ASN row {total - 1}: card context not found for PO {po}")
                continue

            date_ok = _fill_ship_date_for_card(card, date_text) or _fill_ship_date_by_index(ctx, i, date_text)
            track_count = _fill_tracking_for_card(card, tracking)
            if track_count == 0:
                print(f"ASN row {total - 1}: tracking input not found for PO {po}")
                continue
            filled_rows += 1
            filled_po.add(po)
            print(
                f"ASN row {total - 1}: ship_date={'yes' if date_ok else 'no'}, "
                f"filled tracking on {track_count} pack page(s) (PO {po})"
            )

    print(f"ASN tracking filled: {filled_rows}/{total} rows")
    return filled_rows, total, filled_po


def select_asn_orders_for_pos(page: Page, po_set: set[str]) -> int:
    """Select ASN document rows (left checkbox) for each PO in po_set."""
    def _card_is_selected(card) -> bool:
        cb = card.locator("input[type='checkbox']").first
        try:
            if cb.count() > 0 and cb.is_checked():
                return True
        except Exception:
            pass
        for sel in (
            "[aria-checked='true']",
            "label.sps-checkable__label[class*='checked']",
            ".sps-checkable--checked",
            "[data-checked='checked']",
        ):
            try:
                marker = card.locator(sel).first
                if marker.count() > 0 and marker.is_visible():
                    return True
            except Exception:
                continue
        return False

    selected = 0
    if not po_set:
        return 0
    for ctx in _contexts(page):
        links = ctx.locator("[data-testid='sourceDocumentLink'] a[href*='/fulfillment/transactions/document/']")
        if links.count() == 0:
            continue
        for i in range(links.count()):
            link = links.nth(i)
            try:
                po = normalize_po(link.inner_text().strip())
            except Exception:
                continue
            if po not in po_set:
                continue
            # Same card scope as data entry: checkbox sits on the document strip (SHORT / select row).
            card = link.locator(
                "xpath=ancestor::*[.//*[@data-testid='sourceDocumentLink']][1]"
            ).first
            if card.count() == 0:
                card = link.locator("xpath=ancestor::*[@role='row'][1]").first
            if card.count() == 0:
                continue
            try:
                link.scroll_into_view_if_needed()
            except Exception:
                pass
            checkbox = card.locator("input[type='checkbox']").first
            label = card.locator("label.sps-checkable__label").first
            try:
                if _card_is_selected(card):
                    selected += 1
                    continue
                if checkbox.count() > 0:
                    if not _card_is_selected(card):
                        try:
                            checkbox.check(timeout=1500)
                        except Exception:
                            checkbox.evaluate("el => el.click()")
                    if _card_is_selected(card):
                        selected += 1
                elif label.count() > 0 and not _card_is_selected(card):
                    try:
                        label.click(timeout=1500)
                    except Exception:
                        label.click(timeout=1500, force=True)
                    if _card_is_selected(card):
                        selected += 1
            except Exception:
                try:
                    if label.count() > 0 and not _card_is_selected(card):
                        label.evaluate("el => el.click()")
                        if _card_is_selected(card):
                            selected += 1
                except Exception:
                    continue
    return selected


def _count_asn_document_rows(page: Page) -> int:
    n = 0
    for ctx in _contexts(page):
        n += ctx.locator("[data-testid='sourceDocumentLink'] a[href*='/fulfillment/transactions/document/']").count()
    return n


def _count_checked_asn_row_checkboxes(page: Page) -> int:
    """Count document-level row checkboxes that are checked (one per Source Document link)."""
    checked = 0
    for ctx in _contexts(page):
        links = ctx.locator("[data-testid='sourceDocumentLink'] a[href*='/fulfillment/transactions/document/']")
        for i in range(links.count()):
            link = links.nth(i)
            card = link.locator("xpath=ancestor::*[.//*[@data-testid='sourceDocumentLink']][1]").first
            if card.count() == 0:
                continue
            cb = card.locator("input[type='checkbox']").first
            try:
                if cb.count() > 0 and cb.is_checked():
                    checked += 1
                    continue
            except Exception:
                pass
            # SPS sometimes reflects checked state on wrappers/labels instead of native input state.
            for sel in (
                "[aria-checked='true']",
                "label.sps-checkable__label[class*='checked']",
                ".sps-checkable--checked",
                "[data-checked='checked']",
            ):
                try:
                    marker = card.locator(sel).first
                    if marker.count() > 0 and marker.is_visible():
                        checked += 1
                        break
                except Exception:
                    continue
    return checked


def select_all_asn_orders(page: Page) -> None:
    """Click the master Select-all at top of DATA ENTRY list (not per-document controls)."""
    clear_click_blockers(page)
    try:
        page.evaluate("window.scrollTo(0, 0)")
    except Exception:
        pass
    page.wait_for_timeout(200)

    master_selectors = [
        "input[type='checkbox'][aria-label*='Select all' i]",
        "input[type='checkbox'][title*='Select all' i]",
        "input[type='checkbox'][name*='checkall' i]",
        "input[type='checkbox'][id*='checkall' i]",
        "[role='toolbar'] input[type='checkbox']",
        "header input[type='checkbox']",
        "xpath=(//*[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'data entry')]//ancestor::*[1]//input[@type='checkbox'])[1]",
    ]
    for sel in master_selectors:
        for ctx in _contexts(page):
            try:
                loc = ctx.locator(sel)
                if loc.count() == 0:
                    continue
                node = loc.first
                try:
                    if not node.is_visible():
                        continue
                except Exception:
                    pass
                try:
                    node.scroll_into_view_if_needed()
                except Exception:
                    pass
                try:
                    node.click(timeout=1500)
                except Exception:
                    try:
                        node.evaluate("el => el.click()")
                    except Exception:
                        node.click(timeout=1500, force=True)
                return
            except Exception:
                continue

    # Text-based "Select all" control (button or label).
    for sel in (
        "button:has-text('Select all')",
        "button:has-text('Select All')",
        "[role='button']:has-text('Select all')",
        "label:has-text('Select all')",
        "span:has-text('Select all')",
    ):
        if click_first_visible(page, [sel], timeout_ms=2000):
            return

    raise RuntimeError("Could not click ASN master 'Select all' checkbox.")


def ensure_all_asn_rows_selected_for_send(page: Page, filled_po: set[str], expected_filled: int) -> None:
    """
    Select every ASN document row before bulk send: try master select-all, verify,
    then tick any missing rows by PO.
    """
    doc_rows = _count_asn_document_rows(page)
    if doc_rows == 0:
        raise RuntimeError("No ASN document rows found to select.")

    # Do not rely on master select-all alone: explicitly select rows for every filled PO.
    # (Some SPS pages treat top select as current-row only.)
    try:
        select_all_asn_orders(page)
        print("Clicked master Select all (top).")
    except Exception as ex:
        print(f"WARN: master Select all failed ({ex}); continuing with PO-level selection.")

    n = select_asn_orders_for_pos(page, filled_po)
    print(f"Ensured selection on {n} ASN row(s) by PO.")
    page.wait_for_timeout(250)
    checked = _count_checked_asn_row_checkboxes(page)

    if checked < expected_filled:
        # Last gate: if bulk Send is enabled, proceed even if checkbox state is not introspectable.
        send_ready = False
        for sel in (
            "button[data-testid='dataEntry_document-actions-send'][title='Send Document']",
            "button[data-testid='dataEntry_document-actions-send']",
        ):
            for ctx in _contexts(page):
                try:
                    btn = ctx.locator(sel).first
                    if btn.count() == 0 or not btn.is_visible():
                        continue
                    if btn.is_enabled():
                        send_ready = True
                        break
                except Exception:
                    continue
            if send_ready:
                break
        if not send_ready:
            raise RuntimeError(
                f"ASN selection incomplete: {checked}/{doc_rows} document checkboxes checked "
                f"(expected at least {expected_filled} filled rows)."
            )
        print(
            f"WARN: checkbox verification showed {checked}/{doc_rows}, but Send is enabled; proceeding."
        )
    print(f"ASN send selection ready: {checked}/{doc_rows} document row(s) checked.")


def send_documents(page: Page) -> None:
    def _click_visible_with_retries(selectors: list[str], *, tries: int = 4, timeout_ms: int = 2500) -> bool:
        for _ in range(tries):
            clear_click_blockers(page)
            for sel in selectors:
                for ctx in _contexts(page):
                    try:
                        loc = ctx.locator(sel)
                        if loc.count() == 0:
                            continue
                        btn = loc.first
                        btn.wait_for(state="visible", timeout=timeout_ms)
                        try:
                            if not btn.is_enabled():
                                continue
                        except Exception:
                            pass
                        try:
                            btn.scroll_into_view_if_needed()
                        except Exception:
                            pass
                        try:
                            btn.click(timeout=timeout_ms)
                        except Exception:
                            try:
                                btn.evaluate("el => el.click()")
                            except Exception:
                                btn.click(timeout=timeout_ms, force=True)
                        return True
                    except Exception:
                        continue
            page.wait_for_timeout(250)
        return False

    # Send (use exact SPS selector first).
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    except Exception:
        pass
    page.wait_for_timeout(300)
    send_clicked = _click_visible_with_retries(
        [
            "button[data-testid='dataEntry_document-actions-send'][title='Send Document']",
            "button[data-testid='dataEntry_document-actions-send']",
            "div.sps-button.sps-button--icon > button[data-testid='dataEntry_document-actions-send']",
            "button:has(i.sps-icon-paper-plane)",
        ],
        tries=5,
        timeout_ms=3000,
    )
    if not send_clicked:
        raise RuntimeError("Could not click Send Document button.")

    # Continue confirmation modal:
    # Use a modal-specific click path first (no aggressive blocker clearing / ESC),
    # then fallback to generic retries.
    page.wait_for_timeout(300)

    def _click_continue_modal_direct() -> bool:
        continue_selectors = [
            "button[data-testid='modalOkBtn'][title='Continue']",
            "div.sps-button.sps-button--key > button[data-testid='modalOkBtn'][title='Continue']",
            "button[data-testid='modalOkBtn']:has-text('Continue')",
        ]
        for _ in range(8):
            for sel in continue_selectors:
                for ctx in _contexts(page):
                    try:
                        loc = ctx.locator(sel)
                        if loc.count() == 0:
                            continue
                        btn = loc.first
                        btn.wait_for(state="visible", timeout=1200)
                        try:
                            btn.scroll_into_view_if_needed()
                        except Exception:
                            pass
                        try:
                            btn.click(timeout=1500)
                        except Exception:
                            try:
                                btn.evaluate(
                                    "(el) => { el.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true})); }"
                                )
                            except Exception:
                                btn.click(timeout=1500, force=True)
                        return True
                    except Exception:
                        continue
            page.wait_for_timeout(220)
        return False

    continue_clicked = _click_continue_modal_direct()
    if not continue_clicked:
        continue_clicked = _click_visible_with_retries(
            [
                "button[data-testid='modalOkBtn'][title='Continue']",
                "div.sps-button.sps-button--key > button[data-testid='modalOkBtn'][title='Continue']",
                "button[data-testid='modalOkBtn']:has-text('Continue')",
            ],
            tries=6,
            timeout_ms=3000,
        )
    if not continue_clicked:
        # SPS can auto-dismiss/auto-advance quickly after Send; don't fail if modal is gone
        # and we are clearly on a post-send document view.
        page.wait_for_timeout(1500)
        modal_still_visible = False
        for sel in (
            "button[data-testid='modalOkBtn'][title='Continue']",
            "button[data-testid='modalOkBtn']:has-text('Continue')",
        ):
            for ctx in _contexts(page):
                try:
                    loc = ctx.locator(sel)
                    if loc.count() > 0 and loc.first.is_visible():
                        modal_still_visible = True
                        break
                except Exception:
                    continue
            if modal_still_visible:
                break
        if modal_still_visible:
            raise RuntimeError("Could not click modal Continue button.")

        # Accept success when modal is absent and page looks like a loaded document page.
        url_ok = "/fulfillment/transactions/document/" in (page.url or "").lower()
        document_marker = False
        for sel in (
            "text=/advance\\s+ship\\s+notice/i",
            "text=/invoice\\s+from\\s+asn/i",
            "button[data-testid='createNewBtn'][title='New']",
        ):
            for ctx in _contexts(page):
                try:
                    loc = ctx.locator(sel)
                    if loc.count() > 0 and loc.first.is_visible():
                        document_marker = True
                        break
                except Exception:
                    continue
            if document_marker:
                break
        if url_ok or document_marker:
            print("WARN: Continue modal was not clickable/visible; proceeding (post-send page detected).")
            return
        raise RuntimeError("Could not click modal Continue button.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="SPS Commerce Tractor Supply tracking automation after inventory submission."
    )
    parser.add_argument(
        "--csv-path",
        default=str(CSV_PATH),
        help=f"Path to UPS CSV export. Default: {CSV_PATH}",
    )
    parser.add_argument(
        "--submit",
        action="store_true",
        help="Actually send documents. Omit for dry run (fills/selects but does not send).",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser headless (default false).",
    )
    parser.add_argument(
        "--storage-state",
        default=os.environ.get("SPS_PLAYWRIGHT_STORAGE", str(DEFAULT_STORAGE_STATE)),
        help=(
            "Path to Playwright storage_state JSON from a logged-in SPS session. "
            f"Default file: {DEFAULT_STORAGE_STATE} (set SPS_PLAYWRIGHT_STORAGE to override)."
        ),
    )
    parser.add_argument(
        "--keep-open-seconds",
        type=int,
        default=0,
        help="Keep the browser open this many seconds before exit (debugging). Default 0.",
    )
    parser.add_argument(
        "--pause-on-empty",
        action="store_true",
        help="If no orders matched CSV tracking, keep browser open until you press Enter (stdin).",
    )
    parser.add_argument(
        "--interactive-login",
        action="store_true",
        help="If no storage state file exists, open SPS, wait for you to log in in the browser, then save it.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.headless and args.interactive_login:
        print("ERROR: --interactive-login needs a visible browser. Omit --headless.")
        return 1
    csv_path = Path(args.csv_path)
    exit_code = 1
    browser: Browser | None = None
    context: BrowserContext | None = None
    page: Page | None = None
    try:
        tracking_by_po = load_tracking_map(csv_path)
        print(f"Loaded {len(tracking_by_po)} PO->tracking entries from {csv_path}")
        if not tracking_by_po:
            print("No tracking rows loaded from CSV; stopping.")
            return 1

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=bool(args.headless),
                args=[
                    "--disable-features=BlockThirdPartyCookies,TrackingProtection3pcd",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            state_path = Path(args.storage_state)
            if args.interactive_login:
                print(
                    "Interactive login: opening SPS — sign in in the browser first; "
                    "session will be saved for next runs."
                )
                context = browser.new_context()
            elif state_path.is_file():
                print(f"Using Playwright storage state: {state_path}")
                context = browser.new_context(storage_state=str(state_path))
            else:
                print(
                    "NOTE: No storage state file — starting a fresh browser profile (not logged in to SPS).\n"
                    f"      Expected: {state_path}\n"
                    "      Run SPS inventory once (saves session after login), or re-run with --interactive-login."
                )
                context = browser.new_context()
            page = context.new_page()
            did_auto_interactive_login = False
            try:
                print("STEP 1: Open SPS transactions and apply Advanced Search workflow filter...")
                # Proactive login preflight: if saved session is stale, prompt login before workflow steps.
                if not args.headless and not args.interactive_login:
                    try:
                        page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=60_000)
                        page.wait_for_timeout(400)
                    except Exception:
                        pass
                    if _is_login_page_visible(page):
                        print("Detected SPS sign-in page during preflight; attempting .env auto-login first.")
                        if not login_with_env_credentials_then_save(page, context, state_path):
                            print("Auto-login did not complete; opening interactive login.")
                            interactive_login_then_save(page, context, state_path)
                        did_auto_interactive_login = True
                if args.interactive_login:
                    interactive_login_then_save(page, context, state_path)
                open_ready_for_shipment(page)
                print(f"After Transactions/Advanced Search: {page.url}")
                print("STEP 2: Process each PO individually (Shipment then Invoice from ASN)...")
                completed, attempted = process_orders_individually(page, tracking_by_po, submit=bool(args.submit))
                print(f"Individual PO flow completed: {completed}/{attempted} processed.")
                exit_code = 0 if completed > 0 else 1
            except RuntimeError as ex:
                msg = str(ex)
                auth_issue = (
                    "not logged into sps commerce" in msg.lower()
                    or "redirected to sign-in" in msg.lower()
                    or (page is not None and _is_login_page_visible(page))
                )
                can_retry_interactive = (
                    auth_issue
                    and not args.headless
                    and not args.interactive_login
                    and not did_auto_interactive_login
                )
                if can_retry_interactive:
                    did_auto_interactive_login = True
                    print(
                        "Detected expired/missing SPS login session. "
                        "Retrying once with .env auto-login, then interactive login if needed..."
                    )
                    if not login_with_env_credentials_then_save(page, context, state_path):
                        interactive_login_then_save(page, context, state_path)
                    open_ready_for_shipment(page)
                    print(f"After Transactions/Advanced Search: {page.url}")
                    print("STEP 2: Process each PO individually (Shipment then Invoice from ASN)...")
                    completed, attempted = process_orders_individually(page, tracking_by_po, submit=bool(args.submit))
                    print(f"Individual PO flow completed: {completed}/{attempted} processed.")
                    exit_code = 0 if completed > 0 else 1
                else:
                    raise
            finally:
                hold_ms = max(0, int(args.keep_open_seconds)) * 1000
                if exit_code != 0 and hold_ms == 0 and not args.headless:
                    hold_ms = 30_000
                if page is not None and hold_ms > 0:
                    print(f"Holding browser open for {hold_ms // 1000}s (--keep-open-seconds / error hold)…")
                    try:
                        page.wait_for_timeout(hold_ms)
                    except Exception:
                        pass
                try:
                    if context is not None:
                        context.close()
                finally:
                    if browser is not None:
                        browser.close()
    except TimeoutError as ex:
        print(f"Playwright timeout: {ex}")
        return 2
    except Exception as ex:
        print(f"Error: {ex}")
        return 3
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
