"""UPS.com — void all shipments for a given day via Shipping History."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, sync_playwright

from automation.ups_credentials import load_ups_credentials
from automation.ups_popup_dismiss import clear_blocking_overlays
from automation.ups_online_batch_shipping import (
    UpsBatchError,
    _click_any,
    _ensure_ups_tab,
    _is_blank_tab_url,
    _load_config,
    _log,
    _navigate_current_tab,
    _open_browser,
    _page_url,
    _save_session,
    _sel,
    _select_dropdown,
    _timing_ms,
    _ups_login,
    bootstrap_ups_page,
)


DEFAULT_HISTORY_URL = "https://www.ups.com/ship/history?loc=en_US"


@dataclass(frozen=True)
class UpsVoidResult:
    ship_date: date
    voided: int
    skipped: int
    failed: int
    pages: int


def _void_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    raw = cfg.get("void")
    return raw if isinstance(raw, dict) else {}


def _history_url(cfg: dict[str, Any]) -> str:
    return str(_void_cfg(cfg).get("history_url") or DEFAULT_HISTORY_URL).strip()


def _on_shipping_history_page(page: Page) -> bool:
    return "ship/history" in (_page_url(page) or "").lower()


def _open_shipping_history_via_shipping_menu(page: Page, cfg: dict[str, Any]) -> bool:
    """Shipping tab → View Shipping History (primary nav after login)."""
    shipping_tab = (
        _sel(cfg, "shipping_tab")
        or "button[aria-controls='subsection-shipping'], button:has-text('Shipping')"
    )
    view_history = (
        _sel(cfg, "view_shipping_history")
        or "span:text-is('View Shipping History'), span:has-text('View Shipping History'), a:has-text('View Shipping History')"
    )
    if not _click_any(page, shipping_tab, label="Shipping tab"):
        return False
    page.wait_for_timeout(_timing_ms(cfg, "micro_pause_ms", "UPS_MICRO_PAUSE_MS", 700))
    return _click_any(page, view_history, label="View Shipping History")


def _open_shipping_history_via_profile_menu(page: Page, cfg: dict[str, Any]) -> bool:
    if not _click_any(page, _sel(cfg, "user_profile") or "#user-profile", label="User profile"):
        return False
    page.wait_for_timeout(_timing_ms(cfg, "micro_pause_ms", "UPS_MICRO_PAUSE_MS", 500))
    return _click_any(
        page,
        _sel(cfg, "shipping_history")
        or "span.main-text:text-is('Shipping History'), a:has-text('Shipping History')",
        label="Shipping History (profile menu)",
    )


def _open_shipping_history(page: Page, cfg: dict[str, Any]) -> Page:
    page = _ensure_ups_tab(page, cfg)
    if _is_blank_tab_url(_page_url(page)):
        page = _navigate_current_tab(
            page,
            _history_url(cfg),
            label="Blank tab — opening Shipping History",
        )
    if _on_shipping_history_page(page):
        _log("Already on Shipping History.")
        return page

    clear_blocking_overlays(page, cfg, log=_log)

    opened = False
    if _open_shipping_history_via_shipping_menu(page, cfg):
        opened = True
        _log("Opened Shipping History via Shipping -> View Shipping History.")
    elif _open_shipping_history_via_profile_menu(page, cfg):
        opened = True
        _log("Opened Shipping History via user profile menu.")

    if not opened:
        page = _navigate_current_tab(
            page,
            _history_url(cfg),
            label="Opening Shipping History (direct URL)",
        )
    else:
        try:
            page.wait_for_url("**/ship/history**", timeout=60_000)
        except Exception:
            _log("WARN: Shipping History URL not detected after menu click — waiting for load.")

    page.wait_for_load_state("domcontentloaded", timeout=60_000)
    page.wait_for_timeout(_timing_ms(cfg, "micro_pause_ms", "UPS_MICRO_PAUSE_MS", 800))
    clear_blocking_overlays(page, cfg, log=_log)
    if not _on_shipping_history_page(page):
        raise UpsBatchError(
            f"Could not open Shipping History (current URL: {_page_url(page)!r}). "
            "Check selectors shipping_tab and view_shipping_history in ups_batch.json."
        )
    _log(f"Shipping History: {page.url}")
    return page


def _pick_date_in_calendar(page: Page, ship_date: date) -> None:
    if ship_date == date.today():
        today = page.locator(
            ".ups-official_datepicker_today, button.ups-official_datepicker_today"
        ).first
        today.wait_for(state="visible", timeout=12_000)
        today.click()
        return

    day_btn = page.locator(
        "button.ups-official_datepicker_date_chooser_btn"
    ).filter(has_text=str(ship_date.day))
    if day_btn.count() > 0:
        day_btn.first.click()
        return
    raise UpsBatchError(f"Could not pick calendar day for {ship_date.isoformat()}.")


def _filter_history_by_date(page: Page, cfg: dict[str, Any], ship_date: date) -> None:
    _log(f"Filtering Shipping History to {ship_date.month}/{ship_date.day}/{ship_date.year}…")
    if not _click_any(
        page,
        _sel(cfg, "date_range_modify") or "#dateRangeModify",
        label="Modify date range",
    ):
        raise UpsBatchError("Could not click Modify date range.")

    page.wait_for_timeout(_timing_ms(cfg, "micro_pause_ms", "UPS_MICRO_PAUSE_MS", 500))
    _select_dropdown(
        page,
        _sel(cfg, "history_date_range") or "#nwsHistoryDateRangeDropdown",
        label_text="Custom Date Range",
        field="history date range",
    )
    page.wait_for_timeout(_timing_ms(cfg, "micro_pause_ms", "UPS_MICRO_PAUSE_MS", 400))

    calendar_sel = (
        _sel(cfg, "calendar_start")
        or ".ups-icon-calendar, span.icon.ups-icon-calendar"
    )
    page.locator(calendar_sel).first.click()
    page.wait_for_timeout(_timing_ms(cfg, "micro_pause_ms", "UPS_MICRO_PAUSE_MS", 400))
    _pick_date_in_calendar(page, ship_date)

    if not _click_any(
        page,
        _sel(cfg, "date_range_apply") or "#dateRangeApply",
        label="Apply date range",
    ):
        raise UpsBatchError("Could not click Apply on date range.")
    page.wait_for_load_state("domcontentloaded", timeout=60_000)
    page.wait_for_timeout(_timing_ms(cfg, "after_history_filter_ms", "UPS_VOID_AFTER_FILTER_MS", 3000))
    _log("Date filter applied.")


def _set_results_per_page(page: Page, cfg: dict[str, Any], per_page: int = 50) -> None:
    selector = _sel(cfg, "results_per_page") or "#-resultsPerPage, select[name='resultsPerPage']"
    _select_dropdown(
        page,
        selector,
        label_text=str(per_page),
        field="results per page",
    )
    page.wait_for_timeout(_timing_ms(cfg, "after_history_filter_ms", "UPS_VOID_AFTER_FILTER_MS", 2000))
    _log(f"Results per page set to {per_page}.")


def _history_rows(page: Page, cfg: dict[str, Any]):
    selector = (
        _sel(cfg, "history_table_row")
        or "tbody tr:has(.ups-icon-ellipses), table tbody tr"
    )
    return page.locator(selector)


def _row_is_voided(row) -> bool:
    try:
        return row.locator(".ups-icon-dot").count() > 0
    except Exception:
        return False


def _close_row_menu(page: Page) -> None:
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
    except Exception:
        pass


def _void_single_row(page: Page, cfg: dict[str, Any], row, *, dry_run: bool) -> str:
    """Return 'voided', 'skipped', or 'failed'."""
    if _row_is_voided(row):
        return "skipped"

    ellipsis = row.locator(".ups-icon-ellipses").first
    try:
        ellipsis.wait_for(state="visible", timeout=5000)
        ellipsis.click()
    except Exception:
        return "failed"

    page.wait_for_timeout(_timing_ms(cfg, "micro_pause_ms", "UPS_MICRO_PAUSE_MS", 300))
    void_btn = page.locator(_sel(cfg, "action_void") or "#action_void").first
    try:
        void_btn.wait_for(state="visible", timeout=4000)
    except Exception:
        _close_row_menu(page)
        return "skipped"

    if dry_run:
        _log("DRY RUN — would void one shipment.")
        _close_row_menu(page)
        return "voided"

    try:
        void_btn.click()
        if not _click_any(
            page,
            _sel(cfg, "void_yes") or "#nbsVoidShipmentModalYes",
            label="Void Yes",
            timeout_ms=12_000,
        ):
            return "failed"
        page.wait_for_timeout(400)
        _click_any(
            page,
            _sel(cfg, "void_close") or "#nbsVoidShipmentConfirmationModalClose",
            label="Void confirmation Close",
            timeout_ms=12_000,
        )
        page.wait_for_timeout(_timing_ms(cfg, "between_void_ms", "UPS_VOID_BETWEEN_MS", 600))
        return "voided"
    except Exception as exc:
        _log(f"WARN: void failed for row — {exc}")
        _close_row_menu(page)
        return "failed"


def _next_page_available(page: Page, cfg: dict[str, Any]) -> bool:
    selector = (
        _sel(cfg, "pagination_next")
        or "button:has(span:text-is('Next')), a:has(span:text-is('Next'))"
    )
    loc = page.locator(selector).first
    try:
        if not loc.is_visible(timeout=2000):
            return False
    except Exception:
        return False

    disabled = loc.get_attribute("disabled")
    aria_disabled = (loc.get_attribute("aria-disabled") or "").lower()
    classes = (loc.get_attribute("class") or "").lower()
    if disabled is not None or aria_disabled in ("true", "disabled"):
        return False
    if "disabled" in classes or "ups-cta_disabled" in classes:
        return False
    return True


def _goto_next_history_page(page: Page, cfg: dict[str, Any]) -> bool:
    if not _next_page_available(page, cfg):
        return False
    selector = (
        _sel(cfg, "pagination_next")
        or "button:has(span:text-is('Next')), a:has(span:text-is('Next'))"
    )
    page.locator(selector).first.click()
    page.wait_for_load_state("domcontentloaded", timeout=60_000)
    page.wait_for_timeout(_timing_ms(cfg, "history_page_ms", "UPS_VOID_HISTORY_PAGE_MS", 2500))
    return True


def _void_rows_on_current_page(page: Page, cfg: dict[str, Any], *, dry_run: bool) -> tuple[int, int, int]:
    voided = skipped = failed = 0
    rows = _history_rows(page, cfg)
    count = rows.count()
    _log(f"Found {count} row(s) on this page.")
    for i in range(count):
        row = rows.nth(i)
        outcome = _void_single_row(page, cfg, row, dry_run=dry_run)
        if outcome == "voided":
            voided += 1
        elif outcome == "skipped":
            skipped += 1
        else:
            failed += 1
    return voided, skipped, failed


def _void_all_on_history(
    page: Page,
    cfg: dict[str, Any],
    *,
    ship_date: date,
    dry_run: bool,
) -> UpsVoidResult:
    total_voided = total_skipped = total_failed = 0
    page_num = 1
    while True:
        _log(f"Void pass — page {page_num}…")
        v, s, f = _void_rows_on_current_page(page, cfg, dry_run=dry_run)
        total_voided += v
        total_skipped += s
        total_failed += f
        if not _goto_next_history_page(page, cfg):
            _log("No more pages (Next is disabled or hidden).")
            break
        page_num += 1
    return UpsVoidResult(
        ship_date=ship_date,
        voided=total_voided,
        skipped=total_skipped,
        failed=total_failed,
        pages=page_num,
    )


def run_ups_void_shipments(
    *,
    config_path: Path,
    ship_date: date | None = None,
    manual_login: bool = False,
    dry_run: bool = False,
    headless: bool | None = None,
) -> UpsVoidResult:
    cfg = _load_config(config_path)
    target_date = ship_date or date.today()
    browser_cfg = cfg.get("browser", {})
    creds = load_ups_credentials(cfg, optional=False)
    slow_mo = int(browser_cfg.get("slow_mo_ms") or 80)
    if headless is None:
        headless = bool(browser_cfg.get("headless", False))

    leave_open = os.environ.get("UPS_LEAVE_BROWSER_OPEN_AFTER_VOID", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )

    with sync_playwright() as p:
        browser, context, page, persistent, launch_source = _open_browser(
            p, cfg, headless=headless, slow_mo=slow_mo
        )
        try:
            page = bootstrap_ups_page(page, cfg)
            page = _ups_login(
                page, cfg, creds, manual=manual_login, launch_source=launch_source
            )
            page = _ensure_ups_tab(page, cfg)
            page = _open_shipping_history(page, cfg)
            _filter_history_by_date(page, cfg, target_date)
            _set_results_per_page(page, cfg, 50)
            result = _void_all_on_history(
                page, cfg, ship_date=target_date, dry_run=dry_run
            )
            _save_session(context, persistent=persistent)
            _log(
                f"Done — voided {result.voided}, skipped {result.skipped} "
                f"(already void / no Void menu), failed {result.failed}, "
                f"across {result.pages} page(s)."
            )
            return UpsVoidResult(
                ship_date=target_date,
                voided=result.voided,
                skipped=result.skipped,
                failed=result.failed,
                pages=result.pages,
            )
        finally:
            if leave_open:
                _log("Browser left open — close manually when finished.")
            elif browser is not None:
                browser.close()
            else:
                context.close()
