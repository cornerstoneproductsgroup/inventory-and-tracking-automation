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


CSV_PATH = Path(
    r"\\rygarcorp.com\shares\Cornerstone\Dot Com Packing Slips\zzz - Worldship Shipment Files\Export Info\UPS_CSV_EXPORT.csv"
)
DASHBOARD_URL = "https://commerce.spscommerce.com/fulfillment/dashboard/"
TRANSACTIONS_LIST_URL = "https://commerce.spscommerce.com/fulfillment/transactions/list/"
_HERE = Path(__file__).resolve().parent
DEFAULT_STORAGE_STATE = _HERE / "sps_playwright_storage.json"


def normalize_po(value: str) -> str:
    return re.sub(r"\D", "", (value or "").strip())


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
    return [page, *page.frames]


def click_first_visible(page: Page, selectors: list[str], *, timeout_ms: int = 10000) -> bool:
    for sel in selectors:
        for ctx in _contexts(page):
            loc = ctx.locator(sel)
            if loc.count() == 0:
                continue
            try:
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


def _raise_if_cookie_or_auth_wall(page: Page) -> None:
    try:
        body = page.content().lower()
    except Exception:
        body = ""
    if "cookies are disabled" in body or "enable all cookies" in body:
        raise RuntimeError(
            "SPS Commerce reports cookies are disabled in this browser profile. "
            "Enable cookies for Chromium / Playwright, then retry. "
            f"Dashboard: {DASHBOARD_URL}"
        )
    if _looks_logged_out(page.url):
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
                loc = ctx.locator(sel)
                if loc.count() == 0:
                    continue
                try:
                    if loc.first.is_visible():
                        return
                except Exception:
                    continue
        page.wait_for_timeout(200)
    raise RuntimeError("Transactions page did not become ready in time.")


def interactive_login_then_save(page: Page, context: BrowserContext, storage_path: Path) -> None:
    """Open SPS, let the user complete login in the browser, then persist storage_state."""
    start_url = (os.environ.get("SPS_URL") or "").strip() or "https://commerce.spscommerce.com"
    try:
        from automation.config import load_settings

        loaded = load_settings().sps_url
        if (loaded or "").strip():
            start_url = loaded.strip()
    except Exception:
        pass

    page.goto(start_url, wait_until="domcontentloaded", timeout=120_000)
    print(
        f"\n>>> Interactive SPS login\n"
        f">>> Complete sign-in in the browser (started from: {start_url}).\n"
        f">>> When you are fully logged into SPS Commerce (home or dashboard), return to this console.\n"
    )
    input(">>> Press Enter here to save the session and continue with tracking…\n")
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(storage_path))
    print(f">>> Saved session file: {storage_path}\n")


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
    page.goto(TRANSACTIONS_LIST_URL, wait_until="domcontentloaded", timeout=120_000)
    # SPS transactions UI appears to need a real settle window before controls are reliable.
    page.wait_for_timeout(10_000)
    _raise_if_cookie_or_auth_wall(page)
    wait_for_transactions_page_ready(page, timeout_ms=45_000)
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
        clear_click_blockers(page)
        # Strict fast path first: no broad scans.
        fast_clicked = False
        for ctx in _contexts(page):
            for sel in (
                "xpath=//button[normalize-space()='Advanced Search']",
                "button:has-text('Advanced Search')",
            ):
                loc = ctx.locator(sel)
                if loc.count() == 0:
                    continue
                try:
                    loc.first.click(timeout=1200)
                    fast_clicked = True
                    break
                except Exception:
                    try:
                        loc.first.click(timeout=1200, force=True)
                        fast_clicked = True
                        break
                    except Exception:
                        continue
            if fast_clicked:
                break

        if not fast_clicked:
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
        clear_click_blockers(page)
        print("STEP 1.2 done.")
    else:
        print("STEP 1.2 skipped: Advanced Search already open.")

    print("STEP 1.3: Select Workflow = Shipment...")
    set_workflow_ready_for_shipment(page, workflow_selector)
    print("STEP 1.3 done.")

    print("STEP 1.4: Click Search...")
    click_advanced_search_button(page)
    page.wait_for_load_state("domcontentloaded")
    print("STEP 1.4 done.")


def set_workflow_ready_for_shipment(page: Page, workflow_selector: str) -> None:
    clear_click_blockers(page)

    def _shipment_selected() -> bool:
        checks = [
            "xpath=//*[contains(@id,'_tag-') and normalize-space()='Shipment']",
            "xpath=//*[contains(@class,'tag') and normalize-space()='Shipment']",
            "xpath=//span[normalize-space()='Shipment']",
        ]
        for sel in checks:
            for ctx in _contexts(page):
                loc = ctx.locator(sel)
                if loc.count() == 0:
                    continue
                try:
                    if loc.first.is_visible():
                        return True
                except Exception:
                    continue
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

    # Fast path with explicit option selection.
    if _type_and_choose_fast() and _shipment_selected():
        return

    # Step 1: Open the "Workflows Ready For" multiselect/picker.
    opened = click_first_visible(
        page,
        [
            workflow_selector,
            "input[placeholder*='Workflow']",
            "xpath=//*[contains(normalize-space(.), 'Workflows Ready For')]/following::*[self::input or self::div][1]",
            "xpath=//*[contains(normalize-space(.), 'Select Workflow Document')][1]",
        ],
        timeout_ms=2_000,
    )
    if not opened:
        raise RuntimeError("Could not open the 'Workflows Ready For' field.")

    clear_click_blockers(page)

    # Step 2: Type Shipment into the active field and choose from list.
    typed = False
    for ctx in _contexts(page):
        loc = ctx.locator(workflow_selector)
        if loc.count() == 0:
            continue
        try:
            fld = loc.first
            fld.wait_for(state="visible", timeout=1_500)
            fld.click(force=True)
            fld.fill("Shipment")
            click_first_visible(
                page,
                [
                    "li[role='option']:has-text('Shipment')",
                    "[role='option']:has-text('Shipment')",
                    "xpath=//span[normalize-space()='Shipment']",
                ],
                timeout_ms=900,
            )
            typed = True
            break
        except Exception:
            continue
    if not typed:
        # Fallback: use keyboard into focused element.
        page.keyboard.type("Shipment", delay=30)

    # Step 3: Commit selection with keyboard first, then explicit option click.
    try:
        page.keyboard.press("ArrowDown")
        page.keyboard.press("Enter")
    except Exception:
        pass

    click_first_visible(
        page,
        [
            "li[role='option']:has-text('Shipment')",
            "[role='option']:has-text('Shipment')",
            "span:has-text('Shipment')",
        ],
        timeout_ms=900,
    )

    if not _shipment_selected():
        raise RuntimeError("Could not select 'Shipment' from Workflows Ready For list.")


def click_advanced_search_button(page: Page) -> None:
    clear_click_blockers(page)
    # Fast path on exact selector.
    for ctx in _contexts(page):
        loc = ctx.locator("button[data-testid='advSearchBottomSearchButton']")
        if loc.count() == 0:
            continue
        try:
            btn = loc.first
            btn.wait_for(state="visible", timeout=1200)
            btn.click(timeout=800)
            print("Clicked Search via data-testid fast path.")
            return
        except Exception:
            try:
                btn.click(timeout=800, force=True)
                print("Clicked Search via data-testid force path.")
                return
            except Exception:
                pass

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
    date_input = page.locator("[data-testid='asn.header.shipment.shippedDate-input_date_input']")
    date_input.first.wait_for(state="visible", timeout=60000)
    try:
        date_input.first.click(timeout=2000)
        date_input.first.fill(date_text)
    except Exception:
        clear_click_blockers(page)
        date_input.first.click(timeout=2000, force=True)
        date_input.first.fill(date_text)
    print(f"Set ASN shipped date to {date_text}")


def fill_tracking_on_asn(page: Page, tracking_by_po: dict[str, str]) -> tuple[int, int]:
    clear_click_blockers(page)
    po_links = page.locator(
        "a.text-truncate.d-block[href*='/fulfillment/transactions/document/'], "
        "a[href*='/fulfillment/transactions/document/']"
    )
    total = po_links.count()
    filled = 0
    for i in range(total):
        clear_click_blockers(page)
        po_raw = po_links.nth(i).inner_text().strip()
        po = normalize_po(po_raw)
        if not po:
            continue
        tracking = tracking_by_po.get(po)
        if not tracking:
            print(f"ASN row {i}: no tracking for PO {po}")
            continue
        tracking_input = page.locator(f"[data-testid='asn.order.{i}.packInfo.0.trackingNumber-input__input']")
        if tracking_input.count() == 0:
            print(f"ASN row {i}: tracking input not found for PO {po}")
            continue
        try:
            tracking_input.first.click(timeout=1500)
            tracking_input.first.fill(tracking)
        except Exception:
            clear_click_blockers(page)
            tracking_input.first.click(timeout=1500, force=True)
            tracking_input.first.fill(tracking)
        filled += 1
    print(f"ASN tracking filled: {filled}/{total} rows")
    return filled, total


def select_all_asn_orders(page: Page) -> None:
    clear_click_blockers(page)
    # Prefer header checkbox in ASN table.
    if click_first_visible(
        page,
        [
            "label.sps-checkable__label[for*='_ctrl']",
            "thead label.sps-checkable__label",
            "xpath=(//label[contains(@class,'sps-checkable__label')])[1]",
        ],
        timeout_ms=8000,
    ):
        return
    raise RuntimeError("Could not click ASN 'select all' checkbox.")


def send_documents(page: Page) -> None:
    clear_click_blockers(page)
    if not click_first_visible(
        page,
        [
            "button:has(i.sps-icon-paper-plane)",
            "[role='button']:has(i.sps-icon-paper-plane)",
            "xpath=//*[contains(@class,'sps-icon-paper-plane')]/ancestor::*[self::button or @role='button'][1]",
        ],
        timeout_ms=10000,
    ):
        raise RuntimeError("Could not click 'Send Documents' button.")

    clear_click_blockers(page)
    if not click_first_visible(
        page,
        [
            "button[data-testid='modalOkBtn'][title='Continue']",
            "button[data-testid='modalOkBtn']:has-text('Continue')",
        ],
        timeout_ms=10000,
    ):
        raise RuntimeError("Could not click modal 'Continue'.")


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
            browser = p.chromium.launch(headless=bool(args.headless))
            state_path = Path(args.storage_state)
            if state_path.is_file():
                print(f"Using Playwright storage state: {state_path}")
                context = browser.new_context(storage_state=str(state_path))
            elif args.interactive_login:
                print(
                    "Interactive login: opening SPS — sign in in the browser; session will be saved for next runs."
                )
                context = browser.new_context()
            else:
                print(
                    "NOTE: No storage state file — starting a fresh browser profile (not logged in to SPS).\n"
                    f"      Expected: {state_path}\n"
                    "      Run SPS inventory once (saves session after login), or re-run with --interactive-login."
                )
                context = browser.new_context()
            page = context.new_page()
            try:
                print("STEP 1: Open SPS transactions and apply Advanced Search workflow filter...")
                if not state_path.is_file() and args.interactive_login:
                    interactive_login_then_save(page, context, state_path)
                open_ready_for_shipment(page)
                print(f"After Transactions/Advanced Search: {page.url}")
                print("STEP 2: Scan Open rows across pages (Next Page) and match POs against CSV...")
                stats = select_orders_with_tracking(page, tracking_by_po)
                if stats.rows_checked == 0:
                    print(
                        "No SPS orders matched CSV tracking (or no PO rows on page). Nothing to send.\n"
                        "Tip: re-run with --keep-open-seconds 30 to inspect the page, or --pause-on-empty "
                        "to hold until Enter."
                    )
                    if not args.headless:
                        try:
                            page.wait_for_timeout(8000)
                        except Exception:
                            pass
                    if args.pause_on_empty:
                        input("Press Enter to close the browser…")
                    exit_code = 0
                else:
                    print("STEP 3: Create ASN from selected rows...")
                    open_create_new_asn(page)
                    print("STEP 4: Fill shipped date + tracking values...")
                    fill_asn_date(page)
                    filled, total = fill_tracking_on_asn(page, tracking_by_po)
                    if filled == 0:
                        print("No ASN rows were filled with tracking; stopping.")
                        exit_code = 1
                    else:
                        select_all_asn_orders(page)
                        if args.submit:
                            send_documents(page)
                            print(f"Send Documents submitted (filled {filled}/{total} ASN rows).")
                        else:
                            print("Dry run: skipping Send Documents. Re-run with --submit to finalize.")
                        exit_code = 0
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
