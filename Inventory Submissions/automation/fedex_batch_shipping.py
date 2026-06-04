"""FedEx Shipping Plus batch: upload Lowe's Output CSV, finalize by vendor, save label PDFs."""

from __future__ import annotations

import base64
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from playwright.sync_api import (
    BrowserContext,
    Frame,
    Page,
    TimeoutError as PlaywrightTimeout,
    sync_playwright,
)

from automation.fedex_batch_config import (
    DEFAULT_BATCH_URL,
    DEFAULT_LOGIN_URL,
    STORAGE_STATE,
    label_save_timeout_s,
    lowes_fedex_master_path,
    pdf_page_wait_ms,
    shipment_report_download_timeout_ms,
    upload_poll_interval_s,
    upload_poll_timeout_s,
    vendor_label_pdf_path,
)
from automation.fedex_credentials import FedexCredentials, env_file_path, load_fedex_credentials
from automation.fedex_lowes_csv import LowesCsvSkip, resolve_upload_csv
from automation.fedex_reference import (
    ReferenceOrder,
    group_consecutive_by_vendor,
    reference_to_order,
)
from automation.fedex_upload_state import mark_file_used
from automation.pull_orders_warehouse_print import _resolve_printer, print_pdf_windows
from automation.warehouse_print_vendors import (
    bundled_warehouse_vendors_path,
    is_warehouse_print_vendor,
    load_warehouse_print_vendors,
    order_splitter_watcher_path,
)
from automation.windows_save_as import fill_save_as_dialog


def _log(msg: str) -> None:
    print(f"[fedex] {msg}", flush=True)


class FedexBatchError(Exception):
    pass


@dataclass
class ShipmentRowState:
    index: int
    reference: str
    status: str
    tracking: str
    done: bool
    order: ReferenceOrder


def _load_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _sel(cfg: dict[str, Any], key: str, default: str = "") -> str:
    return (cfg.get("selectors", {}).get(key) or default).strip()


def _click_first(page: Page, selector: str, *, timeout_ms: int = 8000) -> bool:
    if not selector:
        return False
    try:
        loc = page.locator(selector).first
        loc.wait_for(state="visible", timeout=timeout_ms)
        loc.click(timeout=timeout_ms)
        return True
    except Exception:
        return False


def _click_any(
    page: Page,
    selectors: list[str],
    *,
    timeout_ms: int = 12_000,
    label: str = "",
) -> bool:
    """Try selectors in order until one clicks."""
    cleaned = [s.strip() for s in selectors if s and s.strip()]
    if not cleaned:
        return False
    per = max(2500, timeout_ms // len(cleaned))
    for sel in cleaned:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=per)
            try:
                loc.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass
            loc.click(timeout=per)
            if label:
                _log(f"Clicked {label} ({sel!r}).")
            return True
        except Exception as exc:
            if label:
                _log(f"WARN: {label} — {sel!r} failed: {exc}")
    return False


def _after_row_select_wait_ms(cfg: dict[str, Any]) -> int:
    timing = cfg.get("timing") or {}
    raw = (
        str(timing.get("after_row_select_ms") or "")
        or (os.environ.get("FEDEX_AFTER_ROW_SELECT_MS") or "2000")
    ).strip()
    try:
        return max(500, int(raw))
    except ValueError:
        return 2000


def _count_checked_shipment_rows(page: Page) -> int:
    """How many shipment rows appear selected in the table."""
    selectors = (
        "tbody tr.mat-mdc-row input[type='checkbox']:checked",
        "tbody tr.mat-mdc-row mat-checkbox.mat-mdc-checkbox-checked",
        "tbody tr.mat-mdc-row .mat-mdc-checkbox-checked",
    )
    for sel in selectors:
        try:
            n = page.locator(sel).count()
            if n > 0:
                return n
        except Exception:
            continue
    return 0


def _wait_for_row_selection(page: Page, *, expected: int, timeout_ms: int = 12_000) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        checked = _count_checked_shipment_rows(page)
        if checked >= expected:
            return True
        page.wait_for_timeout(250)
    return _count_checked_shipment_rows(page) >= expected


def _finalize_toolbar_selectors(cfg: dict[str, Any]) -> list[str]:
    custom = _sel(cfg, "finalize_toolbar")
    defaults = [
        "button.fdx-c-button:has(span.fdx-c-button__title:text-matches('Finalize', 'i'))",
        "button:has(span.fdx-c-button__title:text-matches('Finalize', 'i'))",
        "button.fdx-c-button:has-text('Finalize')",
        "button:has-text('Finalize')",
        "span.fdx-c-button__title:text-matches('^\\s*Finalize\\s*$', 'i')",
        "[aria-label*='Finalize' i]",
        "text=/^\\s*Finalize\\s*$/i",
    ]
    if custom:
        return [s.strip() for s in custom.split(",") if s.strip()] + defaults
    return defaults


def _finalize_menu_selectors(cfg: dict[str, Any]) -> list[str]:
    custom = _sel(cfg, "finalize_print_manual")
    defaults = [
        ".mat-mdc-menu-item:has(span.mat-mdc-menu-item-text:text-matches('Finalize and print manually', 'i'))",
        "span.mat-mdc-menu-item-text:text-matches('Finalize and print manually', 'i')",
        ".mat-mdc-menu-item:has-text('Finalize and print manually')",
        "button.mat-mdc-menu-item:has-text('Finalize and print manually')",
    ]
    if custom:
        return [s.strip() for s in custom.split(",") if s.strip()] + defaults
    return defaults


def _fedex_peel_blocking_overlays(page: Page) -> None:
    """Best-effort: stop full-page loaders from intercepting OneTrust / login clicks."""
    try:
        peeled = page.evaluate(
            """() => {
            const keepIds = new Set([
                'onetrust-banner-sdk', 'onetrust-consent-sdk', 'onetrust-group-container'
            ]);
            const sel =
                'div[class*="overlay" i], div[class*="scrim" i], div[class*="loader" i], ' +
                'div[class*="backdrop" i], [class*="loading" i][class*="fixed" i], ' +
                '[class*="page-loader" i], [class*="blocking" i]';
            let n = 0;
            for (const el of document.querySelectorAll(sel)) {
                if (!el || keepIds.has(el.id)) continue;
                const st = window.getComputedStyle(el);
                if (st.position !== 'fixed' && st.position !== 'absolute') continue;
                const z = parseInt(st.zIndex || '0', 10);
                if (Number.isNaN(z) || z < 50) continue;
                el.style.pointerEvents = 'none';
                n += 1;
            }
            return n;
        }"""
        )
        if peeled:
            _log(f"Peeled {peeled} blocking overlay layer(s) before cookie/login.")
    except Exception:
        pass


def _cookie_accept_selectors(cfg: dict[str, Any]) -> list[str]:
    custom = _sel(cfg, "cookie_accept")
    out: list[str] = []
    if custom:
        out.extend(s.strip() for s in custom.split(",") if s.strip())
    out.extend(
        [
            "#onetrust-accept-btn-handler",
            "button#onetrust-accept-btn-handler",
            "#onetrust-banner-sdk button#onetrust-accept-btn-handler",
            "button:has-text('ACCEPT ALL COOKIES')",
            "button:has-text('Accept all cookies')",
            "button:has-text('Accept All Cookies')",
        ]
    )
    seen: set[str] = set()
    deduped: list[str] = []
    for sel in out:
        if sel not in seen:
            seen.add(sel)
            deduped.append(sel)
    return deduped


def _timing_ms(cfg: dict[str, Any], key: str, env_key: str, default: int) -> int:
    timing = cfg.get("timing") or {}
    raw = str(timing.get(key) or os.environ.get(env_key) or default).strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return default


def _settle_fedex_page(page: Page, cfg: dict[str, Any], *, reason: str) -> None:
    """Wait for navigation/load to finish before looking for login or batch UI."""
    extra_ms = _timing_ms(cfg, "after_navigation_ms", "FEDEX_AFTER_NAV_MS", 2000)
    _log(f"Waiting for FedEx page to load ({reason})…")
    if extra_ms > 0:
        page.wait_for_timeout(extra_ms)
    try:
        page.wait_for_load_state("domcontentloaded", timeout=90_000)
    except PlaywrightTimeout:
        _log("WARN: domcontentloaded timeout — continuing.")
    try:
        page.wait_for_load_state("networkidle", timeout=35_000)
    except PlaywrightTimeout:
        _log("Page still active (network) — waiting for login or batch UI…")


def _maybe_accept_fedex_cookies(page: Page, cfg: dict[str, Any], *, peel_overlays: bool = True) -> bool:
    """Click FedEx OneTrust 'Accept all cookies' when the banner is shown (optional step)."""
    page.wait_for_timeout(400)
    if peel_overlays:
        _fedex_peel_blocking_overlays(page)
    accepted = False
    for sel in _cookie_accept_selectors(cfg):
        try:
            root = page.locator(sel)
            if root.count() == 0:
                continue
            loc = root.first
            loc.wait_for(state="visible", timeout=2500)
            loc.click(timeout=5000)
            _log(f"Accepted cookies ({sel!r}).")
            accepted = True
            break
        except Exception:
            continue
    if not accepted:
        try:
            loc = page.locator("#onetrust-accept-btn-handler").first
            if loc.count() > 0:
                loc.click(force=True, timeout=4000)
                _log("Accepted cookies (force click).")
                accepted = True
        except Exception:
            pass
    if not accepted:
        try:
            clicked = page.evaluate(
                """() => {
                const btn = document.querySelector('#onetrust-accept-btn-handler');
                if (!btn) return false;
                btn.click();
                return true;
            }"""
            )
            if clicked:
                _log("Accepted cookies (JS click).")
                accepted = True
        except Exception:
            pass
    if accepted:
        post_ms = _timing_ms(cfg, "after_cookie_accept_ms", "FEDEX_AFTER_COOKIE_MS", 2500)
        page.wait_for_timeout(post_ms)
        _settle_fedex_page(page, cfg, reason="after cookie accept")
    return accepted


def _is_batch_page(page: Page) -> bool:
    return page.locator('[data-test-id="files-upload-btn"]').count() > 0


def _login_scopes(page: Page) -> list[Page | Frame]:
    scopes: list[Page | Frame] = [page]
    for frame in page.frames:
        if frame != page.main_frame:
            scopes.append(frame)
    return scopes


def _login_field_locators(
    scope: Page | Frame, cfg: dict[str, Any], *, quick: bool = False
) -> tuple[Any, Any] | None:
    user_sel = _sel(cfg, "username_input", "#username")
    pass_sel = _sel(cfg, "password_input", "#password")
    wait_ms = 1500 if quick else 5000
    user_root = scope.locator(user_sel)
    if user_root.count() == 0:
        return None
    user = user_root.first
    try:
        user.wait_for(state="visible", timeout=wait_ms)
        if not user.is_enabled():
            return None
    except Exception:
        return None
    pw_root = scope.locator(pass_sel)
    if pw_root.count() == 0:
        return None
    pw = pw_root.first
    try:
        pw.wait_for(state="visible", timeout=wait_ms)
        if not pw.is_enabled():
            return None
    except Exception:
        return None
    return user, pw


def _find_login_form(
    page: Page, cfg: dict[str, Any], *, quick: bool = False
) -> tuple[Any, Any, Page | Frame] | None:
    for scope in _login_scopes(page):
        hit = _login_field_locators(scope, cfg, quick=quick)
        if hit:
            user, pw = hit
            return user, pw, scope
    return None


def _wait_for_login_ready(
    page: Page, cfg: dict[str, Any], *, timeout_ms: int = 120_000
) -> tuple[Any, Any, Page | Frame] | None:
    """
    Wait until username/password are visible, enabled, and stable (not a loading flash).
    Returns None if the batch page loaded instead (already signed in).
    """
    _log("Waiting for FedEx login form to be ready…")
    deadline = time.monotonic() + timeout_ms / 1000.0
    stable_hits = 0
    last_url = ""

    while time.monotonic() < deadline:
        if _is_batch_page(page):
            _log("Batch uploads page is ready — login not required.")
            return None

        hit = _find_login_form(page, cfg, quick=True)
        if hit:
            user, pw, scope = hit
            try:
                ready = (
                    user.is_visible()
                    and pw.is_visible()
                    and user.is_enabled()
                    and pw.is_enabled()
                )
            except Exception:
                ready = False
            if ready:
                stable_hits += 1
                if stable_hits >= 3:
                    _log(f"Login form ready ({page.url}).")
                    return user, pw, scope
            else:
                stable_hits = 0
        else:
            stable_hits = 0

        url = page.url
        if url != last_url:
            _log(f"Login page loading… {url}")
            last_url = url
        page.wait_for_timeout(500)

    raise FedexBatchError(
        f"FedEx login form did not become ready within {timeout_ms / 1000:.0f}s "
        f"(last URL: {page.url}). "
        "Use login_url https://www.fedex.com/secure-login/en-us/ in fedex_batch.json."
    )


def _wait_for_login_or_batch(page: Page, cfg: dict[str, Any], *, timeout_ms: int = 90_000) -> str:
    """Return 'batch' if uploads page is ready, 'login' if login form is ready."""
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        if _is_batch_page(page):
            return "batch"
        if _find_login_form(page, cfg, quick=True):
            return "login"
        page.wait_for_timeout(500)
    return ""


def _login_submit_selectors(cfg: dict[str, Any]) -> list[str]:
    custom = _sel(cfg, "login_submit")
    out: list[str] = []
    if custom:
        out.extend(s.strip() for s in custom.split(",") if s.strip())
    out.extend(
        [
            "button:has-text('Log In')",
            "button:has-text('LOG IN')",
            "button:has-text('Sign in')",
            "button.fdx-c-button:has-text('Log In')",
        ]
    )
    seen: set[str] = set()
    deduped: list[str] = []
    for sel in out:
        if sel not in seen and "type='submit'" not in sel.lower():
            seen.add(sel)
            deduped.append(sel)
    return deduped


def _fill_login_field(field: Any, value: str, *, label: str) -> None:
    field.wait_for(state="visible", timeout=60_000)
    for _ in range(30):
        try:
            if field.is_enabled():
                break
        except Exception:
            pass
        pg = getattr(field, "page", None)
        if pg is not None:
            pg.wait_for_timeout(200)
    field.click(timeout=20_000)
    field.fill("")
    page_wait = getattr(field, "page", None)
    if page_wait is not None:
        page_wait.wait_for_timeout(200)
    field.fill(value, timeout=30_000)
    if page_wait is not None:
        page_wait.wait_for_timeout(350)
    try:
        got = (field.input_value() or "").strip()
        want = value.strip()
        if got != want:
            _log(f"WARN: {label} mismatch after fill ({got!r} vs expected); retrying.")
            field.click(timeout=10_000)
            field.fill(value, timeout=30_000)
    except Exception:
        pass


def _click_login_submit(page: Page, scope: Page | Frame, cfg: dict[str, Any]) -> None:
    for sel in _login_submit_selectors(cfg):
        try:
            btn = scope.locator(sel).first
            if scope.locator(sel).count() == 0:
                continue
            btn.wait_for(state="visible", timeout=4000)
            _log(f"Clicking login ({sel!r}).")
            try:
                with page.expect_navigation(timeout=90_000, wait_until="domcontentloaded"):
                    btn.click(timeout=15_000)
            except PlaywrightTimeout:
                btn.click(timeout=15_000)
            return
        except Exception:
            continue
    raise FedexBatchError("Could not find FedEx Log In button on the login page.")


def _wait_for_logged_in(page: Page, cfg: dict[str, Any], *, timeout_ms: int = 120_000) -> None:
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        if _is_batch_page(page):
            _log("Session active — batch uploads page is ready.")
            return
        url = page.url.lower()
        if "shippingplus" in url and "secure-login" not in url:
            _log(f"Logged in — Shipping Plus loaded ({page.url}).")
            return
        if "secure-login" not in url and _find_login_form(page, cfg) is None:
            if "fedex.com" in url:
                _log(f"Logged in — left login page ({page.url}).")
                return
        page.wait_for_timeout(500)
    raise FedexBatchError(
        f"FedEx login did not complete within {timeout_ms / 1000:.0f}s (still at {page.url})."
    )


def _perform_fedex_login(page: Page, cfg: dict[str, Any], creds: FedexCredentials) -> None:
    hit = _wait_for_login_ready(page, cfg)
    if hit is None:
        return
    user, pw, scope = hit
    _log(f"Entering credentials for {creds.username!r}")
    _fill_login_field(user, creds.username, label="username")
    _fill_login_field(pw, creds.password, label="password")
    page.wait_for_timeout(600)
    _click_login_submit(page, scope, cfg)
    _settle_fedex_page(page, cfg, reason="after Log In click")
    _wait_for_logged_in(page, cfg)


def _open_batch_after_login(page: Page, cfg: dict[str, Any]) -> None:
    batch_url = (cfg.get("fedex", {}).get("batch_url") or DEFAULT_BATCH_URL).strip()
    if _is_batch_page(page):
        return
    _log(f"Opening batch uploads: {batch_url}")
    page.goto(batch_url, wait_until="domcontentloaded", timeout=120_000)
    page.wait_for_timeout(1500)
    _maybe_accept_fedex_cookies(page, cfg, peel_overlays=False)
    if not _is_batch_page(page):
        raise FedexBatchError(
            f"Batch uploads page did not load after login (URL: {page.url}). "
            "Confirm the account can access FedEx Shipping Plus batch import."
        )


def _login_if_needed(page: Page, cfg: dict[str, Any], creds: FedexCredentials) -> None:
    batch_url = (cfg.get("fedex", {}).get("batch_url") or DEFAULT_BATCH_URL).strip()
    login_url = (cfg.get("fedex", {}).get("login_url") or DEFAULT_LOGIN_URL).strip()

    _log(f"Opening batch page (will log in if needed): {batch_url}")
    page.goto(batch_url, wait_until="domcontentloaded", timeout=120_000)
    _settle_fedex_page(page, cfg, reason="batch page open")
    _maybe_accept_fedex_cookies(page, cfg)

    if _is_batch_page(page):
        _log("Already on batch uploads page (session active).")
        return

    state = _wait_for_login_or_batch(page, cfg, timeout_ms=90_000)
    if state == "batch":
        _log("Batch uploads page loaded after cookies/redirect.")
        return

    if state == "login":
        _log("Login form on current page — signing in without leaving.")
        _perform_fedex_login(page, cfg, creds)
        _open_batch_after_login(page, cfg)
        return

    _log(f"Opening FedEx login as {creds.username!r}: {login_url}")
    page.goto(login_url, wait_until="domcontentloaded", timeout=120_000)
    _settle_fedex_page(page, cfg, reason="login page open")
    _maybe_accept_fedex_cookies(page, cfg, peel_overlays=False)

    state = _wait_for_login_or_batch(page, cfg, timeout_ms=60_000)
    if state == "batch":
        _log("Batch uploads page ready (skipped separate login).")
        return

    _perform_fedex_login(page, cfg, creds)
    _open_batch_after_login(page, cfg)


def _upload_lowes_csv(page: Page, cfg: dict[str, Any], csv_path: Path) -> None:
    upload_btn = _sel(cfg, "upload_button", '[data-test-id="files-upload-btn"]')
    file_input = _sel(cfg, "file_input", "#uploadFileInput-GENERAL, input[type='file']")
    start_upload = _sel(
        cfg,
        "start_upload_button",
        "button:has-text('Start upload'), .fdx-c-button__title:has-text('Start upload')",
    )

    _log(f"Uploading {csv_path.name}")
    attached = page.locator(file_input).first
    if attached.count() > 0:
        try:
            attached.set_input_files(str(csv_path))
        except Exception:
            with page.expect_file_chooser(timeout=30_000) as fc_info:
                _click_first(page, upload_btn, timeout_ms=15_000)
            fc_info.value.set_files(str(csv_path))
    else:
        with page.expect_file_chooser(timeout=30_000) as fc_info:
            _click_first(page, upload_btn, timeout_ms=15_000)
        fc_info.value.set_files(str(csv_path))

    page.wait_for_timeout(800)
    if not _click_first(page, start_upload, timeout_ms=20_000):
        raise FedexBatchError('Could not click "Start upload" on batch options dialog.')
    _log("Start upload clicked; waiting for batch to process…")


def _parse_ready_count(row) -> int:
    try:
        link = row.locator("a[href*='ready-to-finalize']").first
        if link.count() == 0:
            return 0
        text = (link.inner_text() or "").strip()
        if text.isdigit():
            return int(text)
    except Exception:
        pass
    return 0


def _wait_for_batch_ready(page: Page, csv_basename: str) -> None:
    deadline = time.monotonic() + upload_poll_timeout_s()
    interval = upload_poll_interval_s()
    while time.monotonic() < deadline:
        row = page.locator("tr").filter(has_text=csv_basename).first
        if row.count() > 0:
            ready = _parse_ready_count(row)
            if ready > 0:
                _log(f"Batch {csv_basename!r} ready to finalize: {ready} shipment(s).")
                return
            body = (row.inner_text() or "").lower()
            if "in queue" in body:
                _log("Batch still in queue for upload…")
            elif re.search(r"\d+/\d+", body):
                _log(f"Batch processing… ({body[:80]})")
        else:
            _log(f"Waiting for batch row {csv_basename!r} to appear…")
        page.wait_for_timeout(int(interval * 1000))
    raise FedexBatchError(f"Timed out waiting for batch {csv_basename!r} to be ready to finalize.")


def _open_batch_shipments(page: Page, cfg: dict[str, Any], csv_basename: str) -> None:
    row = page.locator("tr").filter(has_text=csv_basename).first
    if row.count() == 0:
        raise FedexBatchError(f"Batch row not found for {csv_basename!r}")

    ready_link = row.locator("a[href*='ready-to-finalize']").first
    if ready_link.count() > 0:
        ready_link.click(timeout=30_000)
    else:
        row.click(timeout=15_000)
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(2000)
    _log(f"Opened shipment list for {csv_basename!r}")


def _row_reference_text(row) -> str:
    ref_cell = row.locator(
        "td.mat-column-reference, [data-label='Reference'], .cdk-column-reference"
    ).first
    if ref_cell.count() > 0:
        inner = ref_cell.locator('[data-test-id="rowText"]').first
        if inner.count() > 0:
            return (inner.inner_text() or "").strip()
        return (ref_cell.inner_text() or "").strip()
    texts = row.locator('[data-test-id="rowText"]')
    for i in range(texts.count()):
        t = (texts.nth(i).inner_text() or "").strip()
        if re.match(r"^\d{5,}\s+\S", t):
            return t
    return ""


def _row_status_text(row) -> str:
    cell = row.locator(
        "td.mat-column-shipmentDerivedStatus, [data-label='Status'], .cdk-column-shipmentDerivedStatus"
    ).first
    if cell.count() > 0:
        return (cell.inner_text() or "").strip()
    return (row.inner_text() or "")[:120]


def _row_tracking_text(row) -> str:
    cell = row.locator("td.mat-column-trackingId, [data-label='Tracking ID']").first
    if cell.count() > 0:
        link = cell.locator("a[data-test-id='link']").first
        if link.count() > 0:
            return (link.inner_text() or "").strip()
        return (cell.inner_text() or "").strip()
    return ""


def _is_row_done(status: str, tracking: str) -> bool:
    st = (status or "").lower()
    if tracking and re.search(r"\d{10,}", tracking):
        return True
    if "created" in st and "printed" in st:
        return True
    return False


def _scan_shipment_rows(page: Page, cfg: dict[str, Any]) -> list[ShipmentRowState]:
    row_sel = _sel(cfg, "shipment_table_row", "table tbody tr.mat-mdc-row, tr.mat-mdc-row")
    rows = page.locator(row_sel)
    n = rows.count()
    out: list[ShipmentRowState] = []
    for i in range(n):
        row = rows.nth(i)
        try:
            if not row.is_visible():
                continue
        except Exception:
            continue
        ref = _row_reference_text(row)
        if not ref or not re.match(r"^\d{5,}", ref):
            continue
        status = _row_status_text(row)
        tracking = _row_tracking_text(row)
        done = _is_row_done(status, tracking)
        order = reference_to_order(ref)
        out.append(
            ShipmentRowState(
                index=i,
                reference=ref,
                status=status,
                tracking=tracking,
                done=done,
                order=order,
            )
        )
    return out


def _clear_row_selection(page: Page, cfg: dict[str, Any]) -> None:
    clear_sel = _sel(cfg, "clear_selection_button", "button:has-text('CLEAR SELECTION')")
    if clear_sel:
        _click_first(page, clear_sel, timeout_ms=3000)
        page.wait_for_timeout(400)


def _select_rows_for_group(page: Page, cfg: dict[str, Any], group: list[ReferenceOrder]) -> int:
    refs = {g.reference for g in group}
    row_sel = _sel(cfg, "shipment_table_row", "table tbody tr.mat-mdc-row, tr.mat-mdc-row")
    cb_sel = _sel(cfg, "row_checkbox", "input[type='checkbox']")
    _clear_row_selection(page, cfg)
    page.wait_for_timeout(500)
    selected = 0
    rows = page.locator(row_sel)
    for i in range(rows.count()):
        row = rows.nth(i)
        ref = _row_reference_text(row)
        if ref not in refs:
            continue
        try:
            row.scroll_into_view_if_needed(timeout=5000)
        except Exception:
            pass
        page.wait_for_timeout(150)
        clicked = False
        for label_sel in (
            "label.fdx-c-form-group__label[data-test-id='label']",
            "label.fdx-c-form-group__label",
            "mat-checkbox label",
        ):
            label = row.locator(label_sel).first
            try:
                if label.count() > 0 and label.is_visible():
                    label.click(timeout=8000)
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            cb = row.locator(cb_sel).first
            try:
                if cb.count() > 0:
                    if not cb.is_checked():
                        cb.click(timeout=8000)
                    clicked = True
            except Exception:
                try:
                    cb.check(force=True, timeout=8000)
                    clicked = True
                except Exception as exc:
                    _log(f"WARN: could not select row {ref!r}: {exc}")
                    continue
        if clicked:
            selected += 1
            page.wait_for_timeout(200)

    if selected > 0:
        if not _wait_for_row_selection(page, expected=selected, timeout_ms=10_000):
            checked = _count_checked_shipment_rows(page)
            _log(
                f"WARN: expected {selected} selected row(s), UI shows {checked} checked — "
                "continuing after extra wait."
            )
            page.wait_for_timeout(800)
        settle_ms = _after_row_select_wait_ms(cfg)
        _log(f"Waiting {settle_ms}ms for selection toolbar before Finalize…")
        page.wait_for_timeout(settle_ms)

    checked = _count_checked_shipment_rows(page)
    _log(f"Row selection: {selected} matched, {checked} checkbox(es) checked.")
    return selected


def _finalize_and_print_manual(page: Page, cfg: dict[str, Any]) -> None:
    """Open Finalize dropdown → Finalize and print manually → print preview tab."""
    _log("Opening Finalize menu…")
    opened = _click_any(
        page,
        _finalize_toolbar_selectors(cfg),
        timeout_ms=20_000,
        label="Finalize toolbar",
    )
    if not opened:
        try:
            btn = page.locator("button.fdx-c-button").filter(
                has=page.locator(
                    "span.fdx-c-button__title",
                    has_text=re.compile(r"^\s*Finalize\s*$", re.I),
                )
            ).first
            btn.wait_for(state="visible", timeout=8000)
            btn.scroll_into_view_if_needed(timeout=3000)
            btn.click(timeout=12_000)
            _log("Clicked Finalize toolbar (filter fallback).")
            opened = True
        except Exception as exc:
            _log(f"WARN: Finalize filter fallback failed: {exc}")

    if not opened:
        raise FedexBatchError("Could not open FINALIZE menu.")

    page.wait_for_timeout(900)
    try:
        page.locator(".mat-mdc-menu-panel, .mat-menu-panel").first.wait_for(
            state="visible",
            timeout=6000,
        )
    except PlaywrightTimeout:
        _log("WARN: Material menu panel not visible yet; trying menu item click.")

    menu_clicked = False
    try:
        item = page.get_by_role(
            "menuitem",
            name=re.compile(r"Finalize and print manually", re.I),
        ).first
        item.wait_for(state="visible", timeout=10_000)
        item.click(timeout=12_000)
        _log('Clicked "Finalize and print manually" (menuitem role).')
        menu_clicked = True
    except Exception as exc:
        _log(f"WARN: menuitem role click failed: {exc}")

    if not menu_clicked:
        menu_clicked = _click_any(
            page,
            _finalize_menu_selectors(cfg),
            timeout_ms=15_000,
            label="Finalize and print manually",
        )

    if not menu_clicked:
        raise FedexBatchError('Could not click "Finalize and print manually".')

    page.wait_for_timeout(1200)


def _print_preview_candidate_pages(page: Page, context: BrowserContext) -> list[Page]:
    pages = list(context.pages)
    candidates = [p for p in pages if p != page]
    if not candidates:
        candidates = [page]

    def _score(p: Page) -> int:
        url = (p.url or "").lower()
        if "pdf" in url or "print" in url or "blob:" in url:
            return 1
        return 0

    return sorted(candidates, key=_score)


def _close_print_preview_pages(page: Page, context: BrowserContext) -> None:
    for candidate in _print_preview_candidate_pages(page, context):
        if candidate == page:
            continue
        try:
            candidate.close()
        except Exception:
            pass


def _cdp_save_pdf(page: Page, context: BrowserContext, dest: Path) -> bool:
    try:
        page.bring_to_front()
        page.wait_for_load_state("domcontentloaded", timeout=20_000)
    except Exception:
        pass
    page.wait_for_timeout(pdf_page_wait_ms())
    try:
        cdp = context.new_cdp_session(page)
        try:
            cdp.send("Emulation.setEmulatedMedia", {"media": "print", "features": []})
        except Exception:
            pass
        result = cdp.send(
            "Page.printToPDF",
            {"printBackground": True, "preferCSSPageSize": True},
        )
        data = base64.b64decode(result["data"])
        if len(data) < 500 or not data.startswith(b"%PDF"):
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return True
    except Exception:
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            page.pdf(path=str(dest), print_background=True)
            return dest.is_file() and dest.stat().st_size > 500
        except Exception:
            return False


def _capture_print_preview_pdf(page: Page, context: BrowserContext, dest: Path) -> bool:
    for candidate in reversed(_print_preview_candidate_pages(page, context)):
        if _cdp_save_pdf(candidate, context, dest):
            return True
    return False


def _save_print_pdf(page: Page, context: BrowserContext, dest: Path, cfg: dict[str, Any]) -> bool:
    """Save label PDF from print preview tab or native Save dialog."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    _log(f"Saving label PDF → {dest}")

    if _capture_print_preview_pdf(page, context, dest):
        _log(f"Saved via print-to-PDF ({dest.stat().st_size:,} bytes)")
        _close_print_preview_pages(page, context)
        return True

    if bool(cfg.get("label_save", {}).get("use_native_save_dialog", True)):
        if fill_save_as_dialog(dest, timeout_s=label_save_timeout_s()):
            return True

    return dest.is_file() and dest.stat().st_size > 500


def _zebra_label_printer() -> str:
    return _resolve_printer(
        "FEDEX_WAREHOUSE_LABEL_PRINTER",
        "PULL_ORDERS_SOS_LABEL_PRINTER",
        "Zebra ZP 450",
    )


def _print_label_pdf(page: Page, context: BrowserContext, cfg: dict[str, Any], *, vendor: str) -> bool:
    """Capture FedEx print preview to a temp PDF and send to the warehouse Zebra."""
    fd, tmp_name = tempfile.mkstemp(suffix=".pdf", prefix="fedex_warehouse_label_")
    os.close(fd)
    dest = Path(tmp_name)
    printer = _zebra_label_printer()
    _log(f"Warehouse vendor {vendor!r}: printing labels on {printer!r} (not saving to share)")

    try:
        if not _capture_print_preview_pdf(page, context, dest):
            _log(f"WARN: could not capture print preview PDF for {vendor!r}")
            return False
        print_pdf_windows(dest, printer)
        time.sleep(2.0)
        _log(f"Submitted Zebra print job for {vendor!r}")
        return True
    finally:
        _close_print_preview_pages(page, context)
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass


def _process_vendor_groups(
    page: Page,
    context: BrowserContext,
    cfg: dict[str, Any],
    *,
    order_date: date | None,
) -> tuple[int, int]:
    saved_pdfs = 0
    printed_groups = 0
    warehouse_vendors = load_warehouse_print_vendors()
    if warehouse_vendors:
        _log(
            f"Warehouse-print vendors ({len(warehouse_vendors)}): "
            f"{', '.join(sorted(warehouse_vendors))}"
        )
    else:
        _log(
            "WARN: No warehouse-print vendors loaded; all labels save to share. "
            f"Check {bundled_warehouse_vendors_path()} or Order Splitter at "
            f"{order_splitter_watcher_path()}"
        )
    pass_num = 0
    while pass_num < 50:
        pass_num += 1
        states = _scan_shipment_rows(page, cfg)
        pending = [s for s in states if not s.done]
        if not pending:
            _log("All shipment rows finalized (tracking present or status printed).")
            break

        orders = [s.order for s in pending]
        groups = group_consecutive_by_vendor(orders)
        if not groups:
            break

        group = groups[0]
        vendor = group[0].vendor_folder
        refs = [o.reference for o in group]
        skus = ", ".join(o.sku for o in group)
        _log(f"Vendor group {vendor!r}: {len(group)} shipment(s) — SKU(s): {skus}")
        if is_warehouse_print_vendor(vendor):
            _log(
                f"  → Zebra ({_zebra_label_printer()!r}): warehouse-print vendor — "
                "labels will NOT be saved to the share"
            )
        else:
            dest = vendor_label_pdf_path(vendor, order_date)
            _log(f"  → Save PDF: {dest}")

        selected = _select_rows_for_group(page, cfg, group)
        if selected == 0:
            _log(f"WARN: no rows selected for {vendor!r}; skipping group.")
            break

        list_page = page
        _finalize_and_print_manual(page, cfg)
        if is_warehouse_print_vendor(vendor):
            if _print_label_pdf(page, context, cfg, vendor=vendor):
                printed_groups += 1
            else:
                _log(f"WARN: could not print labels on Zebra for {vendor!r}")
        else:
            dest = vendor_label_pdf_path(vendor, order_date)
            if _save_print_pdf(page, context, dest, cfg):
                saved_pdfs += 1
                _log(f"Saved {dest.name}")
            else:
                _log(f"WARN: could not save PDF for {vendor!r}")

        try:
            list_page.bring_to_front()
        except Exception:
            pass
        page.wait_for_timeout(1500)
        page.wait_for_load_state("domcontentloaded")

    return saved_pdfs, printed_groups


def _select_all_checkbox_selectors(cfg: dict[str, Any]) -> list[str]:
    custom = _sel(cfg, "select_all_checkbox")
    out: list[str] = []
    if custom:
        out.extend(s.strip() for s in custom.split(",") if s.strip())
    out.extend(
        [
            "thead label.fdx-c-form-group__label[data-test-id='label']",
            "thead label.fdx-c-form-group__label[for^='fedex-checkbox-']",
            "th.mat-column-selectRow label.fdx-c-form-group__label",
            "thead input[type='checkbox']",
        ]
    )
    seen: set[str] = set()
    deduped: list[str] = []
    for sel in out:
        if sel not in seen:
            seen.add(sel)
            deduped.append(sel)
    return deduped


def _select_all_shipment_rows(page: Page, cfg: dict[str, Any]) -> int:
    """Select every row on the shipment list (header select-all checkbox)."""
    _clear_row_selection(page, cfg)
    page.wait_for_timeout(300)

    for sel in _select_all_checkbox_selectors(cfg):
        try:
            loc = page.locator(sel).first
            if page.locator(sel).count() == 0:
                continue
            loc.wait_for(state="visible", timeout=5000)
            if sel.endswith("input[type='checkbox']"):
                if not loc.is_checked():
                    loc.check(force=True)
            else:
                loc.click(timeout=8000)
            page.wait_for_timeout(600)
            row_sel = _sel(cfg, "shipment_table_row", "table tbody tr.mat-mdc-row, tr.mat-mdc-row")
            cb_sel = _sel(cfg, "row_checkbox", "input[type='checkbox']")
            rows = page.locator(row_sel)
            checked = 0
            for i in range(rows.count()):
                row = rows.nth(i)
                if not _row_reference_text(row):
                    continue
                cb = row.locator(cb_sel).first
                try:
                    if cb.is_checked():
                        checked += 1
                except Exception:
                    pass
            if checked > 0:
                _log(f"Select-all: {checked} shipment row(s) checked.")
                return checked
        except Exception:
            continue

    raise FedexBatchError("Could not select all shipment rows (header checkbox not found).")


def _download_menu_selectors(cfg: dict[str, Any]) -> list[str]:
    custom = _sel(cfg, "shipment_report_menu")
    out: list[str] = []
    if custom:
        out.extend(s.strip() for s in custom.split(",") if s.strip())
    out.extend(
        [
            "span.mat-mdc-menu-item-text:has-text('Shipment report (.xlsx file)')",
            ".mat-mdc-menu-item:has-text('Shipment report (.xlsx file)')",
            "span.mat-mdc-menu-item-text:has-text('Shipment report')",
            "button.mat-mdc-menu-item:has-text('Shipment report')",
        ]
    )
    seen: set[str] = set()
    deduped: list[str] = []
    for sel in out:
        if sel not in seen:
            seen.add(sel)
            deduped.append(sel)
    return deduped


def _download_shipment_report_xlsx(page: Page, cfg: dict[str, Any], dest: Path) -> None:
    """Select all rows, DOWNLOAD → Shipment report (.xlsx), save to fixed master path."""
    dest = dest.resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    timeout_ms = shipment_report_download_timeout_ms()

    selected = _select_all_shipment_rows(page, cfg)
    if selected == 0:
        raise FedexBatchError("No shipment rows to include in the report.")

    download_btn = _sel(
        cfg,
        "download_button",
        (
            "button:has-text('DOWNLOAD'), a:has-text('DOWNLOAD'), "
            "[role='button']:has-text('DOWNLOAD'), .fdx-c-button:has-text('DOWNLOAD')"
        ),
    )
    if not _click_first(page, download_btn, timeout_ms=15_000):
        raise FedexBatchError("Could not click DOWNLOAD on the shipment list.")

    page.wait_for_timeout(700)
    menu_clicked = False
    for sel in _download_menu_selectors(cfg):
        try:
            with page.expect_download(timeout=timeout_ms) as dl_info:
                item = page.locator(sel).first
                item.wait_for(state="visible", timeout=8000)
                item.click(timeout=15_000)
            download = dl_info.value
            menu_clicked = True
            break
        except PlaywrightTimeout:
            continue
        except Exception:
            continue

    if not menu_clicked:
        raise FedexBatchError(
            'Could not download "Shipment report (.xlsx file)" from the DOWNLOAD menu.'
        )

    if dest.exists():
        try:
            dest.unlink()
        except OSError as exc:
            _log(f"WARN: could not remove old master file: {exc}")

    download.save_as(str(dest))
    if not dest.is_file() or dest.stat().st_size < 100:
        raise FedexBatchError(f"Shipment report download failed or file is empty: {dest}")

    _log(f"Saved Lowe's Fedex Master ({dest.stat().st_size:,} bytes) → {dest}")


def _export_shipment_report_for_tracking(page: Page, cfg: dict[str, Any]) -> None:
    dest = lowes_fedex_master_path()
    _log(f"Exporting shipment report for Lowe's tracking → {dest.name}")
    _download_shipment_report_xlsx(page, cfg, dest)


def run_fedex_batch(
    *,
    config_path: Path,
    order_date: date | None = None,
    csv_path: Path | None = None,
    plan_only: bool = False,
    skip_upload: bool = False,
    dry_run: bool = False,
) -> int:
    cfg = _load_config(config_path)
    d = order_date or date.today()

    if not plan_only and not dry_run:
        try:
            creds = load_fedex_credentials(cfg)
            _log(f"FedEx credentials loaded for {creds.username!r} (from {env_file_path()})")
        except ValueError as exc:
            raise FedexBatchError(str(exc)) from exc
    else:
        creds = None

    try:
        upload_csv = resolve_upload_csv(order_date=order_date, explicit_path=csv_path)
    except LowesCsvSkip as skip:
        _log(f"Skipping FedEx batch: newest file {skip.top_filename!r} is not today's Lowe's Output.")
        return 0

    csv_basename = upload_csv.name
    _log(f"Lowe's upload file: {upload_csv}")

    if plan_only or dry_run:
        _log(f"Would upload: {upload_csv}")
        _log(f"Label root: {vendor_label_pdf_path('ExampleVendor', d).parent.parent}")
        _log("Run without --plan-only to open FedEx and process vendor groups.")
        return 0

    # Eagerly load and log warehouse vendors before opening FedEx, so output
    # clearly shows whether Zebra-print vendors were detected on this machine.
    warehouse_vendors = load_warehouse_print_vendors(reload=True)
    if warehouse_vendors:
        _log(
            f"Warehouse-print vendors ({len(warehouse_vendors)}): "
            f"{', '.join(sorted(warehouse_vendors))}"
        )
    else:
        _log(
            "WARN: No warehouse-print vendors loaded; all labels will save to share. "
            f"Check {bundled_warehouse_vendors_path()} or Order Splitter at "
            f"{order_splitter_watcher_path()}"
        )

    browser_cfg = cfg.get("browser", {})
    headless = bool(browser_cfg.get("headless", False))
    slow_mo = int(browser_cfg.get("slow_mo_ms", 0))
    default_timeout = int(browser_cfg.get("default_timeout_ms", 120_000))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, slow_mo=slow_mo)
        storage = STORAGE_STATE if STORAGE_STATE.is_file() else None
        context = browser.new_context(
            accept_downloads=True,
            storage_state=str(storage) if storage else None,
        )
        page = context.new_page()
        page.set_default_timeout(default_timeout)
        try:
            _login_if_needed(page, cfg, creds)

            if not skip_upload:
                _upload_lowes_csv(page, cfg, upload_csv)
                _wait_for_batch_ready(page, csv_basename)
                mark_file_used(csv_basename, note="uploaded to FedEx batch")
            else:
                _log("skip_upload: opening existing batch from table…")

            _open_batch_shipments(page, cfg, csv_basename)
            saved, printed = _process_vendor_groups(page, context, cfg, order_date=d)
            _log(f"Saved {saved} vendor label PDF(s) to share.")
            if printed:
                _log(f"Printed {printed} warehouse vendor group(s) on Zebra.")

            _export_shipment_report_for_tracking(page, cfg)

            if storage or not STORAGE_STATE.parent.exists():
                context.storage_state(path=str(STORAGE_STATE))
                _log(f"Session saved to {STORAGE_STATE}")
        finally:
            context.close()
            browser.close()

    return 0
