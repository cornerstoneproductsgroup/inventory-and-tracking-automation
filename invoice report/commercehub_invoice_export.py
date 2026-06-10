"""
Automate CommerceHub (Rithum) DSM and SPS Commerce (Tractor Supply): invoicing flows with an interactive menu.

On run, choose: (1) All — parallel: CommerceHub (Depot + Lowe's, one browser) and SPS Tractor Supply (second browser);
(2) Depot only, (3) Lowe's only, (4) Tractor Supply (SPS), (5) Depot + Lowe's on CommerceHub only.
Optional CLI: ``python commercehub_invoice_export.py retail`` or env ``COMMERCEHUB_MENU_CHOICE=1`` (all) / ``5`` (retail).
Custom date: ``python commercehub_invoice_export.py all --date 2026-05-23`` or env ``COMMERCEHUB_REPORT_DATE=5/23/2026``.

Requires: pip install -r requirements.txt && playwright install chromium

Secrets: copy .env.example to .env (see python-dotenv).
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time
import traceback
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout
from playwright.async_api import async_playwright

from commercehub_previous_business_day import (
    format_criteria_datetime,
    parse_report_date,
    previous_business_day,
)
from depot_invoice_postprocess import (
    InvoiceExportEmpty,
    process_invoice_download,
    save_tractor_supply_csv,
)
from sps_commerce_flow import (
    load_sps_env_from_inventory_project,
    run_sps_tractor_transactions_and_advanced_search,
    sps_chromium_launch_args,
)

_SCRIPT_DIR = Path(__file__).resolve().parent
_ENV_FILE = _SCRIPT_DIR / ".env"


def load_project_dotenv() -> None:
    """Load ``.env`` from this script's folder (not cwd — shortcuts / Task Scheduler often use another cwd)."""
    load_dotenv(_ENV_FILE)


load_project_dotenv()


def _ms(env_name: str, default: int) -> int:
    raw = (os.environ.get(env_name) or "").strip()
    if not raw:
        return default
    try:
        return max(1_000, int(raw))
    except ValueError:
        return default


# Milliseconds — increase on slow Rithum days via .env (see .env.example).
NAV_TIMEOUT_MS = _ms("COMMERCEHUB_NAV_TIMEOUT_MS", 240_000)  # full page / Order Search → criteria
STEP_TIMEOUT_MS = _ms("COMMERCEHUB_STEP_TIMEOUT_MS", 180_000)  # large UI blocks, results, export modal
LOGIN_TIMEOUT_MS = _ms("COMMERCEHUB_LOGIN_TIMEOUT_MS", 180_000)  # post-password / profile shell
DOWNLOAD_TIMEOUT_MS = _ms("COMMERCEHUB_DOWNLOAD_TIMEOUT_MS", 600_000)  # generate + download export
LIST_SETTLE_MS = _ms("COMMERCEHUB_SAVED_SEARCH_LIST_SETTLE_MS", 3_500)  # saved-search list paint delay


def _invoice_fast() -> bool:
    raw = (os.environ.get("COMMERCEHUB_CHAIN_FAST") or os.environ.get("COMMERCEHUB_INVOICE_FAST") or "")
    return raw.strip().lower() in ("1", "true", "yes")


def _probe_ms(slow_ms: int, fast_ms: int | None = None) -> int:
    if not _invoice_fast():
        return slow_ms
    return fast_ms if fast_ms is not None else min(slow_ms, 2500)


def _step_timeout_ms() -> int:
    return _probe_ms(STEP_TIMEOUT_MS, 12_000)


def _frames_main_first(pg) -> list:
    """Prefer main frame when scanning for export dialog (often in popup or iframe)."""
    frames = list(pg.frames)
    mf = pg.main_frame
    return [mf] + [f for f in frames if f is not mf]


def _invoice_results_ready_timeout_ms() -> int:
    """Wait for CSV / sort links after clicking Search (slow Rithum days need longer)."""
    default = 35_000 if _invoice_fast() else 60_000
    return _ms("COMMERCEHUB_INVOICE_RESULTS_READY_MS", default)


async def _invoice_on_criteria_form(page) -> bool:
    """True when the date-filter criteria form is showing (not finished search results)."""
    try:
        return await page.locator("#Operator1").first.is_visible()
    except PlaywrightError:
        return False


async def _invoice_on_results_page(page) -> bool:
    """True when the Order Search results view is showing (not the criteria form)."""
    if await _invoice_on_criteria_form(page):
        return False
    sort = page.locator('a[href*="sortSearchResults.do"]')
    try:
        if await sort.count() and await sort.first.is_visible():
            return True
    except PlaywrightError:
        pass
    try:
        hdr = page.get_by_text(re.compile(r"search\s+results", re.I))
        if await hdr.count() and await hdr.first.is_visible():
            return True
    except PlaywrightError:
        pass
    return False


async def _invoice_results_ready_in_frame(fr) -> bool:
    """True when this frame shows exportable invoice search results."""
    csv = fr.locator('a[href*="linkOpen"]').filter(has_text=re.compile(r"csv", re.I))
    try:
        if await csv.count() and await csv.first.is_visible():
            return True
    except PlaywrightError:
        pass
    export = fr.get_by_role("link", name=re.compile(r"export.*csv|^\s*csv\s*$", re.I))
    try:
        if await export.count() and await export.first.is_visible():
            return True
    except PlaywrightError:
        pass
    po_sort = fr.locator('a[href*="sortSearchResults.do"]').filter(
        has_text=re.compile(r"PO\s+Number", re.I)
    )
    try:
        if await po_sort.count() and await po_sort.first.is_visible():
            return True
    except PlaywrightError:
        pass
    sort_any = fr.locator('a[href*="sortSearchResults.do"]')
    try:
        if await sort_any.count() and await sort_any.first.is_visible():
            no_msg = fr.get_by_text(
                re.compile(
                    r"no\s+(records|results|orders|matching)|0\s+record|did\s+not\s+match",
                    re.I,
                )
            )
            if await no_msg.count() and await no_msg.first.is_visible():
                return False
            data_rows = fr.locator("table tr").filter(has=fr.locator("td"))
            if await data_rows.count() >= 1:
                return True
    except PlaywrightError:
        pass
    return False


async def _invoice_results_have_export_controls(page) -> bool:
    for fr in _frames_main_first(page):
        try:
            if await _invoice_results_ready_in_frame(fr):
                return True
        except PlaywrightError:
            continue
    return False


async def _invoice_explicit_no_results(page) -> bool:
    no_msg = page.get_by_text(
        re.compile(
            r"no\s+(records|results|orders|matching)|0\s+record|did\s+not\s+match",
            re.I,
        )
    )
    try:
        return bool(await no_msg.count()) and await no_msg.first.is_visible()
    except PlaywrightError:
        return False


async def _wait_for_invoice_results_state(
    page, timeout_ms: int | None = None
) -> Literal["ready", "empty", "timeout"]:
    """
    After Search: wait for export controls (has rows) or an explicit no-results message.
    """
    wait_ms = timeout_ms if timeout_ms is not None else _invoice_results_ready_timeout_ms()
    deadline = time.monotonic() + max(2000, wait_ms) / 1000.0
    while time.monotonic() < deadline:
        if await _invoice_on_criteria_form(page):
            await asyncio.sleep(0.3)
            continue
        if await _invoice_explicit_no_results(page):
            return "empty"
        if await _invoice_results_have_export_controls(page):
            return "ready"
        await asyncio.sleep(0.3)
    return "timeout"


async def _search_results_empty(page, *, after_search: bool = False) -> bool:
    """
    True when CommerceHub search results are loaded and there is nothing to export.

    When ``after_search`` is False, never treat the criteria form or a loading page as empty.
    """
    if not after_search:
        if await _invoice_on_criteria_form(page):
            return False
        if not await page.locator('a[href*="sortSearchResults.do"]').count():
            return False
        if not await _invoice_explicit_no_results(page):
            return False
        return not await _invoice_results_have_export_controls(page)

    state = await _wait_for_invoice_results_state(page)
    if state == "ready":
        return False
    if state == "empty":
        return True
    if await _invoice_results_have_export_controls(page):
        return False
    if await _invoice_on_results_page(page) and not await _invoice_explicit_no_results(page):
        _log(
            "WARN: Search Results page is visible with data but export controls were slow; "
            "continuing export flow."
        )
        return False
    _log(
        "WARN: Search results not confirmed within "
        f"{_invoice_results_ready_timeout_ms() // 1000}s; retrying search once…"
    )
    return True


HOME_URL = "https://dsm.commercehub.com/dsm/gotoHome.do"

# Lowe's saved search (execute); override with COMMERCEHUB_LOWE_SAVED_SEARCH_URL if id/name differs.
LOWE_SAVED_SEARCH_URL_DEFAULT = (
    "https://dsm.commercehub.com/dsm/executeSavedSearch.do?"
    "standard=false&name=Lowe%26apos%3Bs+Invoice+Batch+Print+by+Date&id=107248005"
)

_LOG_PATH = Path(__file__).resolve().parent / "commercehub_run.log"


def _log(msg: str) -> None:
    line = f"{datetime.now(timezone.utc).isoformat()} {msg}\n"
    _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line)
    print(msg, flush=True)


def _record_invoice_skip(step: str, reason: str) -> None:
    try:
        inv = Path(__file__).resolve().parent.parent / "Inventory Submissions"
        if inv.is_dir() and str(inv) not in sys.path:
            sys.path.insert(0, str(inv))
        from automation.workflow_run_report import record_skip

        record_skip(step, reason)
    except Exception:
        pass


_MENU_KEYS = {"1": "all", "2": "depot", "3": "lowes", "4": "tractor", "5": "retail"}


def prompt_run_menu() -> str:
    print()
    print("Select invoicing run:")
    print("  1 - All (parallel: CommerceHub Depot+Lowe's, and SPS Tractor)")
    print("  2 - Depot only (CommerceHub)")
    print("  3 - Lowe's only (CommerceHub)")
    print("  4 - Tractor Supply only (SPS Commerce)")
    print("  5 - Depot + Lowe's only (CommerceHub, one browser)")
    print()
    while True:
        choice = input("Enter choice (1-5): ").strip().lower()
        if choice in _MENU_KEYS:
            return _MENU_KEYS[choice]
        if choice in ("all", "depot", "lowes", "tractor", "retail"):
            return choice
        print("Invalid choice — enter 1, 2, 3, 4, or 5.", flush=True)


def peel_report_date_args(argv: list[str]) -> tuple[list[str], date | None]:
    """Remove ``--date`` / ``--report-date`` and return remaining argv plus optional custom day."""
    remaining: list[str] = []
    report_day: date | None = None
    i = 0
    while i < len(argv):
        token = argv[i].strip()
        if token in ("--date", "--report-date"):
            if i + 1 >= len(argv):
                raise ValueError(f"{token} requires a date (YYYY-MM-DD or MM/DD/YYYY).")
            report_day = parse_report_date(argv[i + 1])
            i += 2
            continue
        remaining.append(argv[i])
        i += 1
    return remaining, report_day


def resolve_report_day(argv: list[str]) -> tuple[list[str], date | None]:
    """Custom report day from CLI flags or COMMERCEHUB_REPORT_DATE env."""
    argv, report_day = peel_report_date_args(argv)
    if report_day is not None:
        return argv, report_day
    env_raw = (os.environ.get("COMMERCEHUB_REPORT_DATE") or "").strip()
    if env_raw:
        return argv, parse_report_date(env_raw)
    return argv, None


def resolve_run_mode(argv: list[str]) -> str:
    """CLI arg, COMMERCEHUB_MENU_CHOICE, or interactive menu."""
    load_project_dotenv()
    env_c = (os.environ.get("COMMERCEHUB_MENU_CHOICE") or "").strip()
    if env_c in _MENU_KEYS:
        return _MENU_KEYS[env_c]
    for a in argv:
        s = a.strip().lower()
        if s in ("all", "depot", "lowes", "tractor", "retail"):
            return s
        if s in _MENU_KEYS:
            return _MENU_KEYS[s]
    return prompt_run_menu()


def _env(name: str, default: str | None = None) -> str:
    v = os.environ.get(name, default)
    if v is None or v.strip() == "":
        example = _SCRIPT_DIR / ".env.example"
        if not _ENV_FILE.is_file():
            extra = f" Create {_ENV_FILE} (copy from {example})."
        else:
            extra = f" Add {name} to {_ENV_FILE} (see {example})."
        raise RuntimeError(f"Missing required environment variable: {name}.{extra}")
    return v.strip()


_POST_LOGIN_SELECTOR = 'a.application-identity-item, [data-test="dsmMenu-orders"]'


async def _login(page, username: str, password: str) -> None:
    _log("Opening CommerceHub login page…")
    await page.goto(HOME_URL, wait_until="domcontentloaded")

    await page.locator("#username").wait_for(state="visible", timeout=60_000)
    _log("Entering username…")
    await page.locator("#username").fill(username)
    await page.locator("button._button-login-id, button[data-action-button-primary='true']").first.click()

    await page.locator("#password").wait_for(state="visible", timeout=60_000)
    _log("Entering password…")
    await page.locator("#password").fill(password)
    await page.locator("button._button-login-password").click()

    # Full-page redirects here destroy the JS context; do not poll locator.count() mid-navigation.
    _log("Submitted credentials; waiting for sign-in to finish (profile chooser or home)…")
    try:
        await page.wait_for_selector(_POST_LOGIN_SELECTOR, state="visible", timeout=LOGIN_TIMEOUT_MS)
    except PlaywrightTimeout:
        snap = Path(__file__).resolve().parent / "debug_login_timeout.png"
        await page.screenshot(path=str(snap), full_page=True)
        raise RuntimeError(
            "Timed out after username/password: never reached home menu or profile chooser. "
            f"Screenshot: {snap}"
        ) from None

    home_shell = page.locator('[data-test="dsmMenu-orders"]').first
    profile_row = page.locator("a.application-identity-item").first
    try:
        await profile_row.wait_for(state="visible", timeout=3_000)
        _log("Sign-in complete: profile chooser is visible.")
    except PlaywrightTimeout:
        await home_shell.wait_for(state="visible", timeout=45_000)
        _log("Sign-in complete: home shell loaded (Orders menu).")


async def _pick_profile(page, profile_text: str, profile_url: str | None) -> None:
    if profile_url:
        _log("Opening profile via COMMERCEHUB_PROFILE_URL")
        await page.goto(profile_url, wait_until="domcontentloaded")
        await page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
        await page.locator('[data-test="dsmMenu-orders"]').first.wait_for(state="visible", timeout=NAV_TIMEOUT_MS)
        return

    needle = profile_text.strip()
    if not needle:
        raise RuntimeError("COMMERCEHUB_PROFILE_TEXT is empty; set it or use COMMERCEHUB_PROFILE_URL.")

    home_shell = page.locator('[data-test="dsmMenu-orders"]').first
    # Search lives under Orders; the link is often hidden until the menu opens — do not use it as "home ready".
    # Identity picker uses these anchors (see Rithum / CommerceHub DSM HTML).
    profile_link = page.locator("a.application-identity-item").filter(
        has_text=re.compile(re.escape(needle), re.I)
    )

    _log("Choosing DSM profile (skip if already on home)…")
    try:
        await home_shell.wait_for(state="visible", timeout=5_000)
        _log("Already on home; no profile click needed.")
        return
    except PlaywrightTimeout:
        pass

    try:
        await profile_link.first.wait_for(state="visible", timeout=NAV_TIMEOUT_MS)
    except PlaywrightTimeout:
        try:
            await home_shell.wait_for(state="visible", timeout=5_000)
            _log("Home loaded while waiting for profile row; no profile click needed.")
            return
        except PlaywrightTimeout:
            snap = Path(__file__).resolve().parent / "debug_profile_timeout.png"
            await page.screenshot(path=str(snap), full_page=True)
            raise RuntimeError(
                f"Could not find profile row matching {needle!r}. Screenshot: {snap}. "
                "Set COMMERCEHUB_PROFILE_URL to the full handleLogin.do?identityKey=… link."
            ) from None

    _log(f"Clicking profile match for: {needle!r}")
    try:
        await profile_link.first.click()
    except PlaywrightError as e:
        if "Execution context was destroyed" not in str(e) and "navigation" not in str(e).lower():
            raise
        _log("Navigation after profile click; waiting for home…")

    await page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
    await home_shell.wait_for(state="visible", timeout=NAV_TIMEOUT_MS)
    _log("Profile selected; DSM home ready.")


async def _open_order_search(page) -> None:
    direct = (os.environ.get("COMMERCEHUB_ORDER_SEARCH_URL") or "").strip()
    if direct:
        await page.goto(direct, wait_until="domcontentloaded")
        await page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
        return

    _log("Opening Orders menu, then Search…")
    orders = page.locator('[data-test="dsmMenu-orders"]').first
    await orders.wait_for(state="visible", timeout=_step_timeout_ms())
    await orders.hover()
    await asyncio.sleep(0.35)

    search = page.locator('a[data-test="dsmMenu-orders-search"]')
    try:
        await search.first.wait_for(state="visible", timeout=5_000)
    except PlaywrightTimeout:
        _log("Search not visible after hover; clicking Orders…")
        await orders.click()
        await asyncio.sleep(0.35)

    await search.first.wait_for(state="visible", timeout=_probe_ms(30_000, 12_000))
    await search.first.click()
    _log("Waiting for Order Search / criteria page after Search click…")
    await page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)


async def _maybe_select_saved_search(page, saved_search_name: str) -> None:
    """
    Orders → Search often opens a saved-search list. Pick the named search before criteria/results.
    """
    if (os.environ.get("COMMERCEHUB_SKIP_SAVED_SEARCH_LIST") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        _log("Skipping saved-search list (COMMERCEHUB_SKIP_SAVED_SEARCH_LIST).")
        return

    name = (saved_search_name or "").strip()
    if not name:
        _log("COMMERCEHUB_SAVED_SEARCH_NAME is empty; skip saved-search list step.")
        return

    probe = _probe_ms(5_000, 900)
    settle_ms = _probe_ms(LIST_SETTLE_MS, 700)
    if _invoice_fast():
        _log("Invoice fast-empty mode enabled (COMMERCEHUB_CHAIN_FAST).")

    op = page.locator("#Operator1")
    results = page.locator('a[href*="sortSearchResults.do"]')
    search_btn = page.locator('button[data-test="Search"]')

    try:
        await op.first.wait_for(state="visible", timeout=probe)
        _log("Already on criteria form; no saved search to click.")
        return
    except PlaywrightTimeout:
        pass
    try:
        await results.first.wait_for(state="visible", timeout=probe)
        if await _search_results_empty(page, after_search=True):
            _log("Already on empty search results; no saved search to click.")
            return
        _log("Already on search results; no saved search to click.")
        return
    except PlaywrightTimeout:
        pass
    try:
        await search_btn.first.wait_for(state="visible", timeout=probe)
        _log("Criteria page has Search button; no saved-search list step.")
        return
    except PlaywrightTimeout:
        pass

    _log(f"Waiting {settle_ms} ms for saved-search list to render…")
    await asyncio.sleep(settle_ms / 1000.0)

    _log(f"Selecting saved search matching: {name!r}")
    rx = re.compile(re.escape(name), re.I)
    click_timeout = _step_timeout_ms()
    has_link = (await page.get_by_role("link", name=rx).count() > 0) or (
        await page.locator("a").filter(has_text=rx).count() > 0
    )
    if not has_link:
        if await _search_results_empty(page, after_search=True):
            _log(f"No saved search link for {name!r}; page already has empty results.")
            return
        _log(f"Saved search {name!r} not listed; opening criteria form directly.")
        await _ensure_invoice_criteria_form(page)
        return

    last_err: Exception | None = None
    max_attempts = 2 if _invoice_fast() else 3
    for attempt in range(1, max_attempts + 1):
        _log(f"Saved-search pick attempt {attempt}/{max_attempts}…")
        try:
            by_role = page.get_by_role("link", name=rx)
            await by_role.first.wait_for(state="visible", timeout=click_timeout)
            await by_role.first.scroll_into_view_if_needed()
            await asyncio.sleep(0.2 if _invoice_fast() else 0.35)
            await by_role.first.click(timeout=click_timeout)
            last_err = None
            break
        except PlaywrightTimeout as e:
            last_err = e
        except PlaywrightError as e:
            last_err = e

        try:
            row_link = page.locator("tr").filter(has_text=rx).locator("a").first
            await row_link.wait_for(state="visible", timeout=click_timeout)
            await row_link.scroll_into_view_if_needed()
            await asyncio.sleep(0.2 if _invoice_fast() else 0.35)
            await row_link.click(timeout=click_timeout)
            last_err = None
            break
        except PlaywrightTimeout as e:
            last_err = e
        except PlaywrightError as e:
            last_err = e

        try:
            loose = page.locator("a").filter(has_text=rx).first
            await loose.wait_for(state="visible", timeout=click_timeout)
            await loose.scroll_into_view_if_needed()
            await asyncio.sleep(0.2 if _invoice_fast() else 0.35)
            await loose.click(timeout=click_timeout)
            last_err = None
            break
        except PlaywrightTimeout as e:
            last_err = e
        except PlaywrightError as e:
            last_err = e

        if attempt < max_attempts:
            await asyncio.sleep(0.4 if _invoice_fast() else 0.8)

    if last_err is not None:
        snap = Path(__file__).resolve().parent / "debug_saved_search_list.png"
        await page.screenshot(path=str(snap), full_page=True)
        raise RuntimeError(
            f"Could not click saved search matching {name!r} after 3 tries. Last error: {last_err!r}. "
            f"Screenshot: {snap}. Set COMMERCEHUB_SAVED_SEARCH_NAME to the exact list label, or "
            "COMMERCEHUB_ORDER_SEARCH_URL to open this search directly."
        ) from last_err

    _log("Waiting for selected search to load (criteria or results)…")
    await page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
    await page.wait_for_selector(
        '#Operator1, a[href*="sortSearchResults.do"], button[data-test="Search"]',
        state="visible",
        timeout=_probe_ms(NAV_TIMEOUT_MS, 20_000),
    )
    _log("Saved search is open.")


async def _ensure_invoice_criteria_form(page) -> None:
    """
    Order Search sometimes resumes on Search Results (no #Operator1). From there,
    open Search Criteria so date filters can be applied.
    """
    op = page.locator("#Operator1")
    try:
        await op.wait_for(state="visible", timeout=_probe_ms(10_000, 2_500))
        _log("Invoice criteria form already open.")
        return
    except PlaywrightTimeout:
        _log("Criteria form (#Operator1) not visible yet; checking for Search Results…")

    results_marker = page.locator('a[href*="sortSearchResults.do"]')
    on_results = False
    try:
        await results_marker.first.wait_for(state="visible", timeout=_probe_ms(5_000, 1_500))
        on_results = await results_marker.first.is_visible()
    except PlaywrightTimeout:
        on_results = False

    if not on_results:
        _log("No sortSearchResults link; waiting for criteria form…")
        await op.wait_for(state="visible", timeout=_probe_ms(NAV_TIMEOUT_MS, 15_000))
        return

    _log("On Search Results; navigating to Search Criteria to set dates…")
    criteria_link = (
        page.get_by_role("link", name=re.compile(r"search\s+criteria", re.I))
        .or_(page.locator('a[href*="searchCriteria"]'))
        .or_(page.locator('a[href*="SearchCriteria"]'))
        .or_(page.locator('a[href*="displaySearchCriteria"]'))
        .or_(page.locator('a[href*="editSearch"]'))
    )
    try:
        await criteria_link.first.wait_for(state="visible", timeout=_step_timeout_ms())
    except PlaywrightTimeout:
        snap = Path(__file__).resolve().parent / "debug_no_criteria_link.png"
        await page.screenshot(path=str(snap), full_page=True)
        raise RuntimeError(
            "On Search Results but could not find a Search Criteria link to edit filters. "
            f"Screenshot: {snap}. Set COMMERCEHUB_ORDER_SEARCH_URL to the criteria page URL if needed."
        ) from None

    await criteria_link.first.click()
    await page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
    await op.wait_for(state="visible", timeout=NAV_TIMEOUT_MS)
    _log("Search Criteria form is open.")


async def _set_invoice_criteria(page, report_day) -> None:
    await _ensure_invoice_criteria_form(page)
    await page.locator("#Operator1").select_option("DT_GTE")
    await page.locator("#Operator2").select_option("DT_LTE")

    start_s = format_criteria_datetime(report_day, end_of_day=False)
    end_s = format_criteria_datetime(report_day, end_of_day=True)

    await page.locator("#Edit1").fill(start_s)
    await page.locator("#Edit2").fill(end_s)

    # Open end-date calendar and force 11 PM (value 23) per CommerceHub UI.
    # Prefer DSM's stable id; tr:has(#Edit2) can match both row calendars (strict mode violation).
    end_cal = page.locator("#Edit2CalendarIcon")
    if await end_cal.count() == 0:
        end_cal = page.locator("img.icon-calendar").nth(1)
    await end_cal.click()
    await page.locator("#hourSelect").wait_for(state="visible", timeout=25_000)
    await page.locator("#hourSelect").select_option("23")
    for label in ("OK", "Done", "Apply", "Set"):
        btn = page.get_by_role("button", name=re.compile(f"^{label}$", re.I))
        if await btn.count():
            await btn.first.click()
            break
    else:
        await page.keyboard.press("Escape")


async def _run_search(page) -> None:
    if await _invoice_results_have_export_controls(page):
        _log("Search results already visible; skipping Search click.")
        await page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
        return

    btn = page.locator('button[data-test="Search"]')
    try:
        await btn.first.wait_for(state="visible", timeout=_probe_ms(10_000, 3_000))
    except PlaywrightTimeout:
        if await _invoice_on_results_page(page):
            state = await _wait_for_invoice_results_state(page)
            if state in ("ready", "timeout") and not await _invoice_explicit_no_results(page):
                _log("On Search Results without Search button; continuing with loaded results.")
                return
        _log("Search button not visible; opening Search Criteria…")
        await _ensure_invoice_criteria_form(page)
        await btn.first.wait_for(state="visible", timeout=_step_timeout_ms())

    await btn.first.click()
    _log("Waiting for search results…")
    await page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)


async def _confirm_search_results_or_skip(
    page, report_day, retailer_label: str
) -> bool:
    """
    Return True when results are ready to export; False when confirmed empty (skip).
    Retries Search once on timeout before skipping.
    """
    if not await _search_results_empty(page, after_search=True):
        return True
    if await _invoice_results_have_export_controls(page):
        _log(f"{retailer_label}: results visible on recheck; continuing.")
        return True
    _log(f"{retailer_label}: no results on first search pass; retrying once…")
    await asyncio.sleep(1.0 if _invoice_fast() else 2.0)
    await _ensure_invoice_criteria_form(page)
    await _run_search(page)
    if not await _search_results_empty(page, after_search=True):
        return True
    _log(f"{retailer_label}: no invoices for {report_day} — skipping export and print.")
    _record_invoice_skip(f"{retailer_label} invoice report", "No invoices for report day")
    return False


async def _sort_by_po_number(page) -> None:
    _log("Sorting results by PO Number (Order)…")
    link = (
        page.locator('a[href*="sortSearchResults.do"]')
        .filter(has_text=re.compile(r"PO\s+Number", re.I))
        .first
    )
    try:
        await link.wait_for(state="visible", timeout=_probe_ms(5_000, 1_500))
    except PlaywrightTimeout:
        sorted_hdr = page.get_by_text(re.compile(r"sorted\s+by:\s*PO\s+Number", re.I))
        if await sorted_hdr.count() and await sorted_hdr.first.is_visible():
            _log("Results already sorted by PO Number; skipping sort click.")
            return
        raise
    await link.click()
    await page.wait_for_load_state("domcontentloaded", timeout=STEP_TIMEOUT_MS)


async def _open_lowes_saved_search(page) -> None:
    url = (os.environ.get("COMMERCEHUB_LOWE_SAVED_SEARCH_URL") or "").strip()
    if not url:
        url = LOWE_SAVED_SEARCH_URL_DEFAULT
    _log("Opening Lowe's saved invoice search…")
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)


async def _frame_with_export_checkbox(pg) -> object:
    """Return Frame (or Page.main_frame) that contains the export modal fields."""
    for fr in _frames_main_first(pg):
        try:
            await fr.locator('input[name="includeCurrencyCode"]').wait_for(
                state="visible", timeout=5_000
            )
            return fr
        except PlaywrightTimeout:
            continue
    raise RuntimeError(
        "Export dialog: could not find input[name=includeCurrencyCode] in any frame. "
        "If CSV opens a new window, ensure popups are allowed for this site."
    )


async def _export_excel(
    page, download_dir: Path, *, local_filename_stem: str | None = None
) -> Path:
    csv = (
        page.locator('a[href*="linkOpen"]')
        .filter(has_text=re.compile(r"csv", re.I))
        .or_(page.get_by_role("link", name=re.compile(r"export.*csv", re.I)))
        .first
    )
    await csv.wait_for(state="visible", timeout=STEP_TIMEOUT_MS)

    export_pg = None
    async with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as dl_info:
        try:
            async with page.expect_popup(timeout=STEP_TIMEOUT_MS) as pop_ev:
                await csv.click()
            export_pg = await pop_ev.value
            await export_pg.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
            _log("Export Search opened in a popup window.")
        except PlaywrightTimeout:
            # Click already ran; DSM often uses window.open for Export Search.
            _log(
                f"No popup within {STEP_TIMEOUT_MS // 1000}s after CSV click; "
                "assuming export UI on this tab (iframes checked next)."
            )

        work_page = export_pg if export_pg is not None else page
        if export_pg is None:
            await asyncio.sleep(0.75)

        root = await _frame_with_export_checkbox(work_page)

        include_cc = root.locator('input[name="includeCurrencyCode"]')
        await include_cc.wait_for(state="visible", timeout=STEP_TIMEOUT_MS)
        if await include_cc.is_checked():
            await include_cc.uncheck()

        excel = root.locator('input[name="excel"]')
        if not await excel.is_checked():
            await excel.check()

        await root.locator('input[data-test="form-export-button"]').click()

    download = await dl_info.value
    download_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(download.suggested_filename).suffix or ".csv"
    if local_filename_stem:
        dest = download_dir / f"{local_filename_stem}{suffix}"
    else:
        dest = download_dir / download.suggested_filename
    await download.save_as(str(dest))

    if export_pg is not None:
        try:
            if not export_pg.is_closed():
                await export_pg.close()
        except Exception:
            pass

    return dest


async def _failure_screenshot(context, page) -> None:
    snap = Path(__file__).resolve().parent / "debug_failure.png"
    candidates = []
    if page is not None:
        candidates.append(page)
    for p in context.pages:
        if p not in candidates:
            candidates.append(p)
    for tgt in candidates:
        try:
            if tgt.is_closed():
                continue
        except Exception:
            continue
        try:
            await tgt.screenshot(path=str(snap), full_page=True)
            _log(f"Failure screenshot: {snap}")
            return
        except Exception:
            continue
    _log("Could not capture failure screenshot.")


def _postprocess_retail_export(
    downloaded: Path, report_day, retailer: str, *, retailer_label: str
) -> Path | None:
    """Build workbook + print; return None on empty day or unreadable export (do not fail chain)."""
    try:
        out = process_invoice_download(downloaded, report_day, retailer)
        _log(f"{retailer_label} workbook: {out}")
        return out
    except InvoiceExportEmpty as exc:
        _log(f"{retailer_label}: {exc} — skipping post-process and print.")
        _record_invoice_skip(f"{retailer_label} invoice report", "No invoice rows in export")
        return None
    except RuntimeError as exc:
        msg = str(exc)
        if "Could not find header row" in msg or "looks like HTML" in msg:
            _log(f"WARN: {retailer_label} export format unexpected — {msg}")
            try:
                preview = downloaded.read_text(encoding="utf-8", errors="replace")[:400]
                _log(f"  File preview: {preview!r}")
            except Exception:
                pass
            _record_invoice_skip(f"{retailer_label} invoice report", msg[:200])
            return None
        raise


async def _prepare_page_for_cdp_invoices(page) -> None:
    """CDP attach may land on a non-search page after chain login — open home first."""
    try:
        url = (page.url or "").lower()
    except Exception:
        url = ""
    if "gotohome.do" in url or "gotoview" in url:
        return
    _log("Invoice CDP: navigating to CommerceHub home before Order Search…")
    await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    await page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)


async def _run_depot_invoice_flow(page, download_dir: Path, report_day) -> Path | None:
    _log("Depot invoicing: Order Search → saved search → export…")
    await _open_order_search(page)
    _default_saved = "Home Depot Invoice Batch Print by Date"
    if "COMMERCEHUB_SAVED_SEARCH_NAME" in os.environ:
        saved_name = os.environ["COMMERCEHUB_SAVED_SEARCH_NAME"].strip()
    else:
        saved_name = _default_saved
    await _maybe_select_saved_search(page, saved_name)
    await _set_invoice_criteria(page, report_day)
    await _run_search(page)
    if not await _confirm_search_results_or_skip(page, report_day, "Depot"):
        return None
    await _sort_by_po_number(page)
    path = await _export_excel(page, download_dir, local_filename_stem="commercehub_depot")
    _log(f"Downloaded export: {path}")
    return _postprocess_retail_export(path, report_day, "depot", retailer_label="Depot")


async def _run_lowes_invoice_flow(page, download_dir: Path, report_day) -> Path | None:
    _log("Lowe's invoicing: Order Search → Lowe's saved search → export…")
    await _open_order_search(page)
    await _open_lowes_saved_search(page)
    lowes_saved = (
        os.environ.get("COMMERCEHUB_LOWE_SAVED_SEARCH_NAME")
        or "Lowe's Invoice Batch Print by Date"
    ).strip()
    await _maybe_select_saved_search(page, lowes_saved)
    await _set_invoice_criteria(page, report_day)
    await _run_search(page)
    if not await _confirm_search_results_or_skip(page, report_day, "Lowe's"):
        return None
    await _sort_by_po_number(page)
    path_l = await _export_excel(page, download_dir, local_filename_stem="commercehub_lowes")
    _log(f"Downloaded Lowe's export: {path_l}")
    return _postprocess_retail_export(path_l, report_day, "lowes", retailer_label="Lowe's")


async def _run_sps_tractor_phase(
    context,
    *,
    nav_timeout_ms: int,
    report_day: date,
    download_dir: Path,
) -> Path | None:
    load_project_dotenv()
    load_sps_env_from_inventory_project(override=False)
    sps_user = (os.environ.get("SPS_USERNAME") or "").strip()
    sps_pass = (os.environ.get("SPS_PASSWORD") or "").strip()
    _log(
        f"SPS env check: project .env path={_ENV_FILE} (exists={_ENV_FILE.is_file()}), "
        f"SPS_USERNAME={'set' if sps_user else 'EMPTY'}, SPS_PASSWORD={'set' if sps_pass else 'EMPTY'}."
    )
    if not sps_user or not sps_pass:
        raise RuntimeError(
            "SPS_USERNAME or SPS_PASSWORD is still empty after loading env. "
            f"Put them in {_ENV_FILE} (exact names SPS_USERNAME and SPS_PASSWORD), "
            "or set COMMERCEHUB_SPS_DOTENV to another .env that contains them."
        )
    raw = await run_sps_tractor_transactions_and_advanced_search(
        context,
        report_day=report_day,
        download_dir=download_dir,
        nav_timeout_ms=nav_timeout_ms,
        download_timeout_ms=DOWNLOAD_TIMEOUT_MS,
        log=_log,
    )
    if raw is None:
        _log("Tractor Supply (SPS): no matching invoices for the report day — no report saved.")
        return None
    out, review_notes = save_tractor_supply_csv(raw, report_day)
    _log(f"Tractor Supply (SPS): saved {out}")
    if review_notes:
        _log(f"Tractor Supply (SPS): {len(review_notes)} item(s) to review before QuickBooks:")
        for note in review_notes:
            _log(f"  [tractor:review] {note}")
    try:
        from depot_excel_print import print_landscape_with_gridlines

        print_landscape_with_gridlines(out)
    except Exception as e:
        import traceback

        _log(f"[invoice:tractor] Excel print step failed (workbook was saved): {e}")
        traceback.print_exc()
    try:
        raw.unlink(missing_ok=True)
    except OSError:
        pass
    return out


async def _run_tractor_standalone_browser(
    *,
    download_dir: Path,
    headless: bool,
    report_day: date,
) -> Path | None:
    """SPS Tractor Supply in its own Chromium instance (parallel with CommerceHub when mode is ``all``)."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=sps_chromium_launch_args(),
        )
        context = await browser.new_context(accept_downloads=True)
        try:
            return await _run_sps_tractor_phase(
                context,
                nav_timeout_ms=NAV_TIMEOUT_MS,
                report_day=report_day,
                download_dir=download_dir,
            )
        except Exception:
            await _failure_screenshot(context, None)
            raise
        finally:
            await context.close()
            await browser.close()
            _log("SPS Tractor browser closed.")


async def _run_commercehub_invoice_browser(
    *,
    run_depot: bool,
    run_lowes: bool,
    download_dir: Path,
    headless: bool,
    report_day: date,
) -> tuple[Path | None, Path | None]:
    """One CommerceHub session: optional Depot and/or Lowe's flows on the same ``page``."""
    if not run_depot and not run_lowes:
        return None, None
    username = _env("COMMERCEHUB_USERNAME")
    password = _env("COMMERCEHUB_PASSWORD")
    profile_text = os.environ.get("COMMERCEHUB_PROFILE_TEXT", "Cornerstone Products Group")
    profile_url = (os.environ.get("COMMERCEHUB_PROFILE_URL") or "").strip() or None

    depot_path: Path | None = None
    lowes_path: Path | None = None
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=sps_chromium_launch_args(),
        )
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()
        try:
            await _login(page, username, password)
            await _pick_profile(page, profile_text.strip(), profile_url)
            if run_depot:
                depot_path = await _run_depot_invoice_flow(page, download_dir, report_day)
            if run_lowes:
                lowes_path = await _run_lowes_invoice_flow(page, download_dir, report_day)
            return depot_path, lowes_path
        except Exception:
            await _failure_screenshot(context, page)
            raise
        finally:
            await context.close()
            await browser.close()
            _log("CommerceHub browser closed.")


async def run_export(
    mode: str = "all", *, report_day: date | None = None
) -> tuple[Path | None, Path | None, Path | None]:
    load_project_dotenv()
    mode = mode.strip().lower()
    allowed = frozenset({"all", "depot", "lowes", "tractor", "retail"})
    if mode not in allowed:
        raise ValueError(f"Invalid run mode {mode!r}; expected one of {sorted(allowed)}")

    _log(f"--- Run start (mode: {mode}) ---")
    _log(
        f"Timeouts (ms): NAV={NAV_TIMEOUT_MS} STEP={STEP_TIMEOUT_MS} "
        f"LOGIN={LOGIN_TIMEOUT_MS} DOWNLOAD={DOWNLOAD_TIMEOUT_MS} LIST_SETTLE={LIST_SETTLE_MS}"
    )

    download_dir = Path(os.environ.get("COMMERCEHUB_DOWNLOAD_DIR", "./downloads")).resolve()
    headless = os.environ.get("COMMERCEHUB_HEADLESS", "true").lower() in ("1", "true", "yes")
    day = report_day if report_day is not None else previous_business_day()
    if report_day is not None:
        _log(f"Report day: {day.isoformat()} (custom date)")
    else:
        _log(f"Report day: {day.isoformat()} (previous business day)")

    if mode == "tractor":
        tractor_path = await _run_tractor_standalone_browser(
            download_dir=download_dir,
            headless=headless,
            report_day=day,
        )
        return (None, None, tractor_path)

    if mode == "all":
        _log(
            "Parallel: CommerceHub (Depot then Lowe's, one browser) + "
            "SPS Commerce / Tractor Supply (second browser)."
        )
        (depot_path, lowes_path), tractor_path = await asyncio.gather(
            _run_commercehub_invoice_browser(
                run_depot=True,
                run_lowes=True,
                download_dir=download_dir,
                headless=headless,
                report_day=day,
            ),
            _run_tractor_standalone_browser(
                download_dir=download_dir,
                headless=headless,
                report_day=day,
            ),
        )
        return depot_path, lowes_path, tractor_path

    if mode == "retail":
        d, l = await _run_commercehub_invoice_browser(
            run_depot=True,
            run_lowes=True,
            download_dir=download_dir,
            headless=headless,
            report_day=day,
        )
        return d, l, None

    if mode == "depot":
        d, l = await _run_commercehub_invoice_browser(
            run_depot=True,
            run_lowes=False,
            download_dir=download_dir,
            headless=headless,
            report_day=day,
        )
        return d, l, None

    if mode == "lowes":
        d, l = await _run_commercehub_invoice_browser(
            run_depot=False,
            run_lowes=True,
            download_dir=download_dir,
            headless=headless,
            report_day=day,
        )
        return d, l, None

    raise AssertionError(f"unhandled mode {mode!r}")


def main(argv: list[str] | None = None) -> None:
    argv = list(argv if argv is not None else sys.argv[1:])
    try:
        argv, report_day = resolve_report_day(argv)
        mode = resolve_run_mode(argv)
        depot_out, lowes_out, tractor_out = asyncio.run(run_export(mode, report_day=report_day))
        if depot_out is not None:
            _log(f"Done Depot: {depot_out}")
        elif mode in ("depot", "retail", "all"):
            _log("Depot: finished — no invoices for the report day (0 results).")
        if lowes_out is not None:
            _log(f"Done Lowe's: {lowes_out}")
        elif mode in ("lowes", "retail", "all"):
            _log("Lowe's: finished — no invoices for the report day (0 results).")
        if tractor_out is not None:
            _log(f"Done Tractor Supply: {tractor_out}")
        elif mode in ("tractor", "all") and tractor_out is None:
            _log("Tractor Supply (SPS): finished — no invoices for the report day (0 results).")
    except Exception as e:  # noqa: BLE001 — surface any failure to scheduler logs
        _log(f"ERROR: {e}")
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
