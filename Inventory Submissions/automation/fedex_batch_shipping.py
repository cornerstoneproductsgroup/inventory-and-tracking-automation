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
    Browser,
    BrowserContext,
    Frame,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeout,
    sync_playwright,
)

from automation.fedex_batch_config import (
    DEFAULT_BATCH_URL,
    DEFAULT_BROWSER_PROFILE_DIR,
    DEFAULT_LOGIN_URL,
    STORAGE_STATE,
    WAREHOUSE_LABEL_QUEUE_DIR,
    label_save_timeout_s,
    resolve_fedex_warehouse_label_printer,
    lowes_fedex_master_path,
    pdf_page_wait_ms,
    shipment_report_download_timeout_ms,
    upload_poll_interval_s,
    upload_poll_timeout_s,
    vendor_label_pdf_path,
    warehouse_after_print_ms,
    warehouse_label_queue_path,
    warehouse_print_pause_ms,
)
from automation.fedex_warehouse_label_watcher import (
    FedexWarehouseLabelWatcher,
    drain_timeout_s,
    warehouse_label_print_mode,
)
from automation.fedex_credentials import FedexCredentials, env_file_path, load_fedex_credentials
from automation.fedex_lowes_csv import LowesCsvSkip, resolve_upload_csv
from automation.fedex_reference import (
    ReferenceOrder,
    group_by_vendor,
    parse_reference,
    reference_to_order,
)
from automation.fedex_upload_state import mark_file_used
from automation.pull_orders_warehouse_print import (
    _printer_name_matches,
    _resolve_printer,
    print_pdf_windows,
)
from automation.warehouse_print_vendors import (
    bundled_warehouse_vendors_path,
    is_warehouse_print_vendor,
    load_warehouse_print_vendors,
    order_splitter_watcher_path,
)
from automation.windows_save_as import fill_save_as_dialog, wait_for_save_as_dialog


def _log(msg: str) -> None:
    print(f"[fedex] {msg}", flush=True)


class FedexBatchError(Exception):
    pass


_REFERENCE_RE = re.compile(r"(\d{5,}\s+.+)")


@dataclass
class ShipmentRowState:
    index: int
    reference: str
    status: str
    tracking: str
    done: bool
    order: ReferenceOrder


def _normalize_reference(ref: str) -> str:
    return re.sub(r"\s+", " ", (ref or "").strip())


def _load_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


_FEDEX_STEALTH_INIT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
"""


def _env_bool(name: str, *, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    if raw in ("0", "false", "no", "off", "disable", "disabled"):
        return False
    return raw in ("1", "true", "yes", "on")


def _resolve_fedex_channel(browser_cfg: dict[str, Any]) -> str | None:
    """Prefer installed Edge/Chrome over Playwright Chromium (FedEx blocks automation)."""
    explicit = (
        (os.environ.get("FEDEX_BROWSER_CHANNEL") or "").strip()
        or str(browser_cfg.get("channel") or "").strip()
    )
    lowered = explicit.lower()
    if lowered in ("chromium", "bundled", "playwright"):
        return None
    if lowered == "auto" or not explicit:
        return "msedge" if os.name == "nt" else "chrome"
    return explicit


def _resolve_fedex_user_data_dir(browser_cfg: dict[str, Any]) -> Path | None:
    """Persistent profile dir — behaves like a normal browser (cookies survive)."""
    disable = (
        (os.environ.get("FEDEX_USE_PERSISTENT_PROFILE") or "").strip().lower()
        in ("0", "false", "no", "off", "disable", "disabled")
        or browser_cfg.get("use_persistent_profile") is False
    )
    if disable:
        return None

    raw = (
        (os.environ.get("FEDEX_USER_DATA_DIR") or "").strip()
        or str(browser_cfg.get("user_data_dir") or "").strip()
    )
    if raw.lower() in ("0", "false", "no", "off", "disable", "disabled"):
        return None
    path = Path(raw) if raw else DEFAULT_BROWSER_PROFILE_DIR
    if not path.is_absolute():
        path = DEFAULT_BROWSER_PROFILE_DIR.parent / path
    return path


def _fedex_launch_args(browser_cfg: dict[str, Any]) -> list[str]:
    extra = browser_cfg.get("args") or []
    if isinstance(extra, str):
        extra = [extra]
    base = [
        "--disable-blink-features=AutomationControlled",
        "--disable-features=BlockThirdPartyCookies,TrackingProtection3pcd",
    ]
    out: list[str] = []
    for arg in [*base, *extra]:
        text = str(arg).strip()
        if text and text not in out:
            out.append(text)
    return out


def _apply_fedex_stealth(context: BrowserContext) -> None:
    try:
        context.add_init_script(_FEDEX_STEALTH_INIT)
    except Exception:
        pass


def _open_fedex_browser(
    p: Playwright,
    cfg: dict[str, Any],
    *,
    headless: bool,
    slow_mo: int,
) -> tuple[Browser | None, BrowserContext, Page, bool]:
    """
    Launch FedEx automation browser.

    Uses installed Edge/Chrome + persistent profile by default because FedEx
    often serves Retry/failed-to-load to Playwright's bundled Chromium.
    """
    browser_cfg = cfg.get("browser", {})
    user_data_dir = _resolve_fedex_user_data_dir(browser_cfg)
    args = _fedex_launch_args(browser_cfg)
    ignore_automation = browser_cfg.get("ignore_automation_args", True)
    ignore_default_args = ["--enable-automation"] if ignore_automation else None
    channels: list[str | None] = []
    primary = _resolve_fedex_channel(browser_cfg)
    if primary:
        channels.append(primary)
    for alt in ("msedge", "chrome"):
        if alt not in channels:
            channels.append(alt)
    channels.append(None)

    seen: set[str | None] = set()
    last_err: Exception | None = None

    for channel in channels:
        if channel in seen:
            continue
        seen.add(channel)
        label = channel or "playwright chromium"
        try:
            launch_kwargs: dict[str, Any] = {
                "headless": headless,
                "slow_mo": slow_mo,
                "args": args,
            }
            if ignore_default_args:
                launch_kwargs["ignore_default_args"] = ignore_default_args
            if channel:
                launch_kwargs["channel"] = channel

            if user_data_dir is not None:
                user_data_dir.mkdir(parents=True, exist_ok=True)
                context = p.chromium.launch_persistent_context(
                    str(user_data_dir),
                    accept_downloads=True,
                    **launch_kwargs,
                )
                _apply_fedex_stealth(context)
                page = context.pages[0] if context.pages else context.new_page()
                _log(
                    f"FedEx browser: {label} with persistent profile "
                    f"({user_data_dir})"
                )
                return None, context, page, True

            browser = p.chromium.launch(**launch_kwargs)
            storage = STORAGE_STATE if STORAGE_STATE.is_file() else None
            context = browser.new_context(
                accept_downloads=True,
                storage_state=str(storage) if storage else None,
                locale="en-US",
                viewport={"width": 1440, "height": 900},
            )
            _apply_fedex_stealth(context)
            page = context.new_page()
            _log(f"FedEx browser: {label} (ephemeral context)")
            return browser, context, page, False
        except Exception as exc:
            last_err = exc
            _log(f"WARN: could not launch FedEx browser ({label}): {exc}")

    raise FedexBatchError(
        "Could not launch a FedEx browser. Install Microsoft Edge or Google Chrome, "
        "or set FEDEX_BROWSER_CHANNEL=msedge (or chrome) in .env. "
        f"Last error: {last_err}"
    )


def _save_fedex_session(context: BrowserContext, *, uses_persistent_profile: bool) -> None:
    if uses_persistent_profile:
        _log(
            f"Session cookies kept in browser profile ({DEFAULT_BROWSER_PROFILE_DIR})."
        )
        return
    context.storage_state(path=str(STORAGE_STATE))
    _log(f"Session saved to {STORAGE_STATE}")


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


def _fedex_short_pause(page: Page, cfg: dict[str, Any], *, ms: int | None = None) -> None:
    delay = ms if ms is not None else _timing_ms(cfg, "micro_pause_ms", "FEDEX_MICRO_PAUSE_MS", 350)
    if delay > 0:
        page.wait_for_timeout(delay)


def _fedex_initial_wait(page: Page, cfg: dict[str, Any]) -> None:
    """One short wait after the first navigation (avoid networkidle on every step)."""
    extra_ms = _timing_ms(cfg, "initial_wait_ms", "FEDEX_INITIAL_WAIT_MS", 1200)
    _log(f"Waiting for FedEx page ({extra_ms}ms)…")
    page.wait_for_timeout(extra_ms)
    try:
        page.wait_for_load_state("domcontentloaded", timeout=60_000)
    except PlaywrightTimeout:
        _log("WARN: initial domcontentloaded timeout — continuing.")


def _retry_selectors(cfg: dict[str, Any]) -> list[str]:
    custom = _sel(cfg, "load_retry_button")
    out: list[str] = []
    if custom:
        out.extend(s.strip() for s in custom.split(",") if s.strip())
    out.extend(
        [
            "button:has-text('Retry')",
            "button:has-text('RETRY')",
            "a:has-text('Retry')",
            ".fdx-c-button:has-text('Retry')",
            "button.fdx-c-button:has-text('Retry')",
            "[role='button']:has-text('Retry')",
        ]
    )
    return out


def _is_load_failure_page(page: Page) -> bool:
    try:
        body = (page.locator("body").inner_text(timeout=2000) or "").lower()
    except Exception:
        return False
    return (
        "failed to load" in body
        or "page didn't load" in body
        or "page did not load" in body
        or "something went wrong" in body
        or "try again" in body
    )


def _retry_button_visible(page: Page, cfg: dict[str, Any]) -> bool:
    if _is_load_failure_page(page):
        return True
    try:
        btn = page.get_by_role("button", name=re.compile(r"retry", re.I)).first
        if btn.count() > 0 and btn.is_visible():
            return True
    except Exception:
        pass
    for sel in _retry_selectors(cfg):
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                return True
        except Exception:
            continue
    return False


_fedex_recover_count = 0
_fedex_last_reload_at = 0.0


def _reset_fedex_recover_state() -> None:
    global _fedex_recover_count, _fedex_last_reload_at
    _fedex_recover_count = 0
    _fedex_last_reload_at = 0.0


def _batch_url(cfg: dict[str, Any]) -> str:
    return (cfg.get("fedex", {}).get("batch_url") or DEFAULT_BATCH_URL).strip()


def _recover_from_load_failure(page: Page, cfg: dict[str, Any], creds: FedexCredentials) -> bool:
    """
    Retry/load-failure page: do NOT click Retry — reload batch shipping URL and log in if needed.
    """
    global _fedex_recover_count, _fedex_last_reload_at

    if not _retry_button_visible(page, cfg):
        return False

    now = time.monotonic()
    if now - _fedex_last_reload_at < 6.0:
        return False
    if _fedex_recover_count >= 3:
        raise FedexBatchError(
            "FedEx stayed on a Retry/load-failure page after reloading the batch "
            "shipping URL 3 times. Check network or VPN, then run again."
        )

    _fedex_recover_count += 1
    _fedex_last_reload_at = now
    batch_url = _batch_url(cfg)
    _log(
        "Retry/load-failure page detected — reloading batch shipping "
        f"(not clicking Retry): {batch_url}"
    )
    page.goto(batch_url, wait_until="domcontentloaded", timeout=120_000)
    _fedex_short_pause(page, cfg, ms=800)
    _maybe_accept_fedex_cookies(page, cfg)

    if _is_batch_page(page):
        _log("Batch uploads page is ready after reload.")
        return True

    if creds is None:
        _log(
            "Load-failure page after reload — manual/session mode; "
            "not auto-filling login (complete sign-in in the browser)."
        )
        return False

    if _find_login_form(page, cfg, quick=True) or _is_load_failure_page(page):
        _log("Login required after reload — using FedEx secure-login page (not empty redirect).")
        _goto_fedex_login_page(page, cfg, creds)
        _submit_fedex_login(page, cfg, creds)
        if not _is_batch_page(page):
            _log(f"Opening batch uploads after login: {batch_url}")
            page.goto(batch_url, wait_until="domcontentloaded", timeout=120_000)
            _fedex_short_pause(page, cfg, ms=600)
            _maybe_accept_fedex_cookies(page, cfg, peel_overlays=False)
        return _is_batch_page(page)

    return False


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
        post_ms = _timing_ms(cfg, "after_cookie_accept_ms", "FEDEX_AFTER_COOKIE_MS", 600)
        page.wait_for_timeout(post_ms)
    return accepted


def _is_batch_page(page: Page) -> bool:
    return page.locator('[data-test-id="files-upload-btn"]').count() > 0


def _is_batch_uploads_list(page: Page) -> bool:
    """True on the batch uploads table (not an empty in-progress shipment detail page)."""
    if not _is_batch_page(page):
        return False
    try:
        body = (page.locator("body").inner_text(timeout=3000) or "").upper()
    except Exception:
        return False
    if "VIEWING 0/" in body or "VIEWING 0 " in body:
        return False
    return any(
        marker in body
        for marker in (
            "READY TO FINALIZE",
            "READY TO FINALIZE OR SHARE",
            "CREATION DATE",
            "BATCH UPLOADS",
        )
    )


def _ensure_batch_uploads_list(page: Page, cfg: dict[str, Any]) -> None:
    """Stay on or return to the batch uploads table after upload/navigation."""
    if _is_batch_uploads_list(page):
        return
    batch_url = _batch_url(cfg)
    _log(
        "Returning to batch uploads table "
        f"(current URL: {page.url})."
    )
    page.goto(batch_url, wait_until="domcontentloaded", timeout=120_000)
    _fedex_short_pause(page, cfg, ms=1200)


def _find_batch_upload_row(page: Page, csv_basename: str):
    return page.locator("tr").filter(has_text=csv_basename).first


def _upload_poll_per_shipment_s() -> float:
    raw = (os.environ.get("FEDEX_UPLOAD_POLL_PER_SHIPMENT_S") or "6").strip()
    try:
        return max(2.0, float(raw))
    except ValueError:
        return 6.0


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
    page: Page,
    cfg: dict[str, Any],
    creds: FedexCredentials | None = None,
    *,
    timeout_ms: int = 90_000,
) -> tuple[Any, Any, Page | Frame] | None:
    """
    Wait until username/password are visible and stable (not a loading flash).
    Returns None if the batch page loaded instead (already signed in).
    """
    deadline = time.monotonic() + timeout_ms / 1000.0
    stable_hits = 0

    while time.monotonic() < deadline:
        if creds is not None and _retry_button_visible(page, cfg):
            _recover_from_load_failure(page, cfg, creds)

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
                if stable_hits >= 2:
                    _log(f"Login form ready ({page.url}).")
                    return user, pw, scope
            else:
                stable_hits = 0
        else:
            stable_hits = 0

        page.wait_for_timeout(350)

    raise FedexBatchError(
        f"FedEx login form did not become ready within {timeout_ms / 1000:.0f}s "
        f"(last URL: {page.url}). "
        "Use login_url https://www.fedex.com/secure-login/en-us/ in fedex_batch.json."
    )


def _wait_for_login_or_batch(
    page: Page,
    cfg: dict[str, Any],
    creds: FedexCredentials | None = None,
    *,
    timeout_ms: int = 60_000,
) -> str:
    """Return 'batch' if uploads page is ready, 'login' if login form is ready."""
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        if creds is not None and _retry_button_visible(page, cfg):
            _recover_from_load_failure(page, cfg, creds)
        if _is_batch_page(page):
            return "batch"
        if _find_login_form(page, cfg, quick=True):
            return "login"
        page.wait_for_timeout(350)
    return ""


def _login_next_selectors(cfg: dict[str, Any]) -> list[str]:
    custom = _sel(cfg, "login_next")
    out: list[str] = []
    if custom:
        out.extend(s.strip() for s in custom.split(",") if s.strip())
    out.extend(
        [
            "button:has-text('Next')",
            "button:has-text('Continue')",
            "button:has-text('CONTINUE')",
            "button.fdx-c-button:has-text('Next')",
        ]
    )
    return out


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


def _field_input_value(field: Any) -> str:
    try:
        return (field.input_value() or "").strip()
    except Exception:
        return ""


def _type_into_login_field(field: Any, value: str, *, label: str) -> None:
    """Type like a user so FedEx React fields keep the value."""
    pg = getattr(field, "page", None)
    field.wait_for(state="visible", timeout=45_000)
    field.click(timeout=10_000)
    if pg is not None:
        pg.wait_for_timeout(150)
    try:
        field.press("Control+a", timeout=3000)
        field.press("Backspace", timeout=3000)
    except Exception:
        pass
    try:
        field.press_sequentially(value, delay=45, timeout=60_000)
    except Exception:
        field.fill(value, timeout=15_000)
    if pg is not None:
        pg.wait_for_timeout(250)


def _maybe_advance_username_step(
    page: Page, scope: Page | Frame, pw: Any, cfg: dict[str, Any]
) -> None:
    """FedEx secure login: username step may require Next before password appears."""
    try:
        if pw.is_visible():
            return
    except Exception:
        pass
    for sel in _login_next_selectors(cfg):
        try:
            btn = scope.locator(sel).first
            if btn.count() == 0 or not btn.is_visible():
                continue
            _log(f"Clicking {sel!r} after username (password step).")
            btn.click(timeout=10_000)
            page.wait_for_timeout(600)
            return
        except Exception:
            continue


def _fill_fedex_login_credentials(
    page: Page,
    scope: Page | Frame,
    user: Any,
    pw: Any,
    cfg: dict[str, Any],
    creds: FedexCredentials,
) -> None:
    """
    Username → Tab → password → Tab so values stick in FedEx's login form.
    """
    _log(f"Entering credentials for {creds.username!r}")
    _type_into_login_field(user, creds.username, label="username")
    got_user = _field_input_value(user)
    if got_user != creds.username.strip():
        _log(f"WARN: username field={got_user!r}; retyping.")
        _type_into_login_field(user, creds.username, label="username")
        got_user = _field_input_value(user)
    _log(f"Username field: {len(got_user)} character(s).")

    _maybe_advance_username_step(page, scope, pw, cfg)

    try:
        user.press("Tab", timeout=5000)
        page.wait_for_timeout(400)
    except Exception:
        pass

    try:
        pw.wait_for(state="visible", timeout=20_000)
    except PlaywrightTimeout:
        _maybe_advance_username_step(page, scope, pw, cfg)
        pw.wait_for(state="visible", timeout=20_000)

    _type_into_login_field(pw, creds.password, label="password")
    got_pw = _field_input_value(pw)
    if len(got_pw) < 2:
        _log("WARN: password field empty after fill; retyping.")
        _type_into_login_field(pw, creds.password, label="password")
        got_pw = _field_input_value(pw)
    _log(f"Password field: {len(got_pw)} character(s).")
    if len(got_pw) < 2:
        raise FedexBatchError(
            "FedEx password field stayed empty after fill. "
            "Check FEDEX_PASSWORD in Inventory Submissions\\.env"
        )

    try:
        pw.press("Tab", timeout=5000)
        page.wait_for_timeout(300)
    except Exception:
        pass


def _submit_fedex_login(page: Page, cfg: dict[str, Any], creds: FedexCredentials) -> bool:
    """Wait for login form, fill credentials, submit, wait for batch or Shipping Plus."""
    hit = _wait_for_login_ready(page, cfg, creds, timeout_ms=90_000)
    if hit is None:
        return _is_batch_page(page)
    user, pw, scope = hit
    _fill_fedex_login_credentials(page, scope, user, pw, cfg, creds)
    page.wait_for_timeout(350)
    _click_login_submit(page, scope, cfg)
    _wait_for_logged_in(page, cfg, creds)
    return _is_batch_page(page) or "shippingplus" in page.url.lower()


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


def _wait_for_logged_in(
    page: Page,
    cfg: dict[str, Any],
    creds: FedexCredentials | None = None,
    *,
    timeout_ms: int = 90_000,
) -> None:
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        if creds is not None and _retry_button_visible(page, cfg):
            _recover_from_load_failure(page, cfg, creds)
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
        page.wait_for_timeout(350)
    raise FedexBatchError(
        f"FedEx login did not complete within {timeout_ms / 1000:.0f}s (still at {page.url})."
    )


def _perform_fedex_login(page: Page, cfg: dict[str, Any], creds: FedexCredentials) -> None:
    if not _submit_fedex_login(page, cfg, creds):
        raise FedexBatchError(f"FedEx login did not reach batch/Shipping Plus ({page.url}).")


def _goto_fedex_login_page(page: Page, cfg: dict[str, Any], creds: FedexCredentials) -> None:
    """Open secure-login URL (more reliable than redirect stub with empty fields)."""
    login_url = (cfg.get("fedex", {}).get("login_url") or DEFAULT_LOGIN_URL).strip()
    _log(f"Opening FedEx login page: {login_url}")
    page.goto(login_url, wait_until="domcontentloaded", timeout=120_000)
    _fedex_short_pause(page, cfg, ms=900)
    _maybe_accept_fedex_cookies(page, cfg, peel_overlays=False)


def _env_truthy(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


def _print_manual_login_instructions() -> None:
    print(
        "\n=== FedEx manual login ===\n"
        "The browser opened the FedEx SIGN-IN page (secure-login), not the batch page.\n"
        "Type your username and password yourself — the script will NOT auto-fill them.\n"
        "Complete MFA / email code if FedEx asks.\n"
        "When signed in, open Shipping Plus batch uploads if you are not redirected there.\n"
        "Press Enter here when the Upload button is visible on the batch page.\n"
        "Type q + Enter to cancel.\n",
        flush=True,
    )


def _manual_login_status_line(page: Page, cfg: dict[str, Any]) -> str:
    if _is_batch_page(page):
        return "Batch uploads page detected."
    if _retry_button_visible(page, cfg):
        return (
            "Browser shows FedEx Retry / failed-to-load — sign in or click Retry yourself "
            "(script will not auto-fill credentials)."
        )
    url = (page.url or "").strip()
    if len(url) > 90:
        url = url[:87] + "..."
    return f"Waiting for you to finish sign-in… current URL: {url or '(unknown)'}"


def _wait_for_manual_batch_ready(page: Page, cfg: dict[str, Any]) -> None:
    """Pause until the user finishes signing in and the batch uploads page is ready."""
    batch_url = _batch_url(cfg)
    deadline = time.monotonic() + 600.0
    last_status_at = 0.0

    while time.monotonic() < deadline:
        if _is_batch_page(page):
            _log("Batch uploads page ready.")
            return

        now = time.monotonic()
        if now - last_status_at >= 10.0:
            print(f"  [status] {_manual_login_status_line(page, cfg)}", flush=True)
            last_status_at = now

        try:
            line = input("Press Enter when batch Upload page is ready (q to quit): ")
        except EOFError as exc:
            raise FedexBatchError(
                "Manual login needs an interactive console "
                "(run from Run FedEx Manual Login.bat or Run FedEx Batch.bat, not a detached job)."
            ) from exc
        if line.strip().lower() == "q":
            raise FedexBatchError("Manual login cancelled.")

        if _is_batch_page(page):
            _log("Batch uploads page ready.")
            return

        _log(f"Checking batch uploads page: {batch_url}")
        page.goto(batch_url, wait_until="domcontentloaded", timeout=120_000)
        _fedex_short_pause(page, cfg, ms=600)
        _maybe_accept_fedex_cookies(page, cfg, peel_overlays=False)

        if _is_batch_page(page):
            _log("Batch uploads page ready.")
            return

        if _retry_button_visible(page, cfg):
            print(
                "\nFedEx still shows Retry / failed-to-load.\n"
                "In manual mode the script does NOT auto-fill username/password or click Retry.\n"
                "Fix the page in the browser, then press Enter when Upload is visible.\n",
                flush=True,
            )
        else:
            print(
                "Batch page not detected yet — finish login/navigation in the browser, "
                "then press Enter again.",
                flush=True,
            )

    raise FedexBatchError(
        "Manual login timed out after 10 minutes. Confirm you can open FedEx Shipping Plus "
        "batch import in a normal browser."
    )


def _pause_for_manual_login(page: Page, cfg: dict[str, Any]) -> None:
    """Open secure-login; user types credentials; continue when batch uploads is ready."""
    if _is_batch_page(page):
        _log("Batch uploads page already open (manual login not required).")
        return

    login_url = (cfg.get("fedex", {}).get("login_url") or DEFAULT_LOGIN_URL).strip()
    _log(f"Manual login: opening FedEx sign-in page: {login_url}")
    page.goto(login_url, wait_until="domcontentloaded", timeout=120_000)
    _fedex_short_pause(page, cfg, ms=900)
    _maybe_accept_fedex_cookies(page, cfg, peel_overlays=False)

    _print_manual_login_instructions()
    _wait_for_manual_batch_ready(page, cfg)


def _open_batch_after_login(page: Page, cfg: dict[str, Any], creds: FedexCredentials) -> None:
    batch_url = (cfg.get("fedex", {}).get("batch_url") or DEFAULT_BATCH_URL).strip()
    if _is_batch_page(page):
        return
    _log(f"Opening batch uploads: {batch_url}")
    page.goto(batch_url, wait_until="domcontentloaded", timeout=120_000)
    _fedex_short_pause(page, cfg, ms=500)
    if _retry_button_visible(page, cfg):
        _recover_from_load_failure(page, cfg, creds)
    _maybe_accept_fedex_cookies(page, cfg, peel_overlays=False)
    if not _is_batch_page(page):
        state = _wait_for_login_or_batch(page, cfg, creds, timeout_ms=45_000)
        if state != "batch" and not _is_batch_page(page):
            raise FedexBatchError(
                f"Batch uploads page did not load after login (URL: {page.url}). "
                "Confirm the account can access FedEx Shipping Plus batch import."
            )


def _login_if_needed(
    page: Page,
    cfg: dict[str, Any],
    creds: FedexCredentials,
    *,
    manual_login: bool = False,
    skip_auto_login: bool = False,
) -> None:
    if manual_login:
        _reset_fedex_recover_state()
        _pause_for_manual_login(page, cfg)
        return

    batch_url = _batch_url(cfg)
    login_url = (cfg.get("fedex", {}).get("login_url") or DEFAULT_LOGIN_URL).strip()

    _reset_fedex_recover_state()
    _log(f"Opening batch page (will log in if needed): {batch_url}")
    page.goto(batch_url, wait_until="domcontentloaded", timeout=120_000)
    _fedex_initial_wait(page, cfg)
    if _retry_button_visible(page, cfg):
        _recover_from_load_failure(page, cfg, creds)
    _maybe_accept_fedex_cookies(page, cfg)

    if _is_batch_page(page):
        _log("Already on batch uploads page (session active).")
        return

    if skip_auto_login:
        _log(
            "FEDEX_SKIP_AUTO_LOGIN: saved session did not reach batch page — "
            "complete login in the browser."
        )
        _pause_for_manual_login(page, cfg)
        return

    state = _wait_for_login_or_batch(page, cfg, creds, timeout_ms=60_000)
    if state == "batch":
        _log("Batch uploads page loaded after cookies/redirect.")
        return

    if state == "login":
        _log("Login form on batch redirect — opening secure-login page.")
        _goto_fedex_login_page(page, cfg, creds)
        _submit_fedex_login(page, cfg, creds)
        _open_batch_after_login(page, cfg, creds)
        return

    if _retry_button_visible(page, cfg):
        if _recover_from_load_failure(page, cfg, creds) and _is_batch_page(page):
            return

    _goto_fedex_login_page(page, cfg, creds)
    if _is_batch_page(page):
        _log("Batch uploads page ready.")
        return

    _submit_fedex_login(page, cfg, creds)
    _open_batch_after_login(page, cfg, creds)


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
    _log("Start upload clicked; waiting for upload to finish on batch uploads table…")
    page.wait_for_timeout(2000)
    _ensure_batch_uploads_list(page, cfg)


def _parse_ready_count(row) -> int:
    try:
        link = row.locator("a[href*='ready-to-finalize']").first
        if link.count() == 0:
            for sel in (
                "a[href*='ready-to-finalize']",
                "a[href*='readyToFinalize']",
            ):
                link = row.locator(sel).first
                if link.count() > 0:
                    break
        if link.count() == 0:
            return 0
        text = (link.inner_text() or "").strip()
        if text.isdigit():
            return int(text)
    except Exception:
        pass
    return 0


def _parse_batch_row_progress(row) -> tuple[int, int | None, int | None]:
    """Return (ready_to_finalize, finalized_so_far, total_in_batch)."""
    ready = _parse_ready_count(row)
    try:
        text = row.inner_text() or ""
    except Exception:
        text = ""
    fin_m = re.search(r"(\d+)\s*/\s*(\d+)", text)
    finalized = int(fin_m.group(1)) if fin_m else None
    total = int(fin_m.group(2)) if fin_m else None
    return ready, finalized, total


def _wait_for_batch_ready(page: Page, cfg: dict[str, Any], csv_basename: str) -> int:
    """
    Poll the batch uploads table until the file row shows a ready-to-finalize count > 0.
    Large batches need longer — timeout extends when total shipment count is known.
    """
    _ensure_batch_uploads_list(page, cfg)
    interval = upload_poll_interval_s()
    per_shipment_s = _upload_poll_per_shipment_s()
    started_at = time.monotonic()
    deadline = started_at + upload_poll_timeout_s()
    last_status = ""
    extended_for_total = False

    while time.monotonic() < deadline:
        _ensure_batch_uploads_list(page, cfg)
        row = _find_batch_upload_row(page, csv_basename)
        if row.count() == 0:
            if last_status != "missing":
                _log(f"Waiting for batch row {csv_basename!r} on uploads table…")
                last_status = "missing"
        else:
            ready, finalized, total = _parse_batch_row_progress(row)
            if total and total > 0 and not extended_for_total:
                needed_s = max(upload_poll_timeout_s(), 120.0 + total * per_shipment_s)
                deadline = started_at + needed_s
                extended_for_total = True
                _log(
                    f"Batch has {total} shipment(s) — allowing up to {needed_s:.0f}s "
                    "for FedEx to finish processing."
                )
            if ready > 0:
                _log(
                    f"Batch {csv_basename!r} ready to finalize: {ready} shipment(s) "
                    f"(finalized {finalized or 0}/{total or '?'})"
                )
                return ready
            try:
                body = (row.inner_text() or "").lower()
            except Exception:
                body = ""
            if "in queue" in body:
                status = "in queue"
            elif finalized is not None and total:
                status = f"processing {finalized}/{total}"
            else:
                status = "waiting for ready link"
            if status != last_status:
                _log(f"Batch {csv_basename!r}: {status}…")
                last_status = status
        page.wait_for_timeout(int(interval * 1000))

    raise FedexBatchError(
        f"Timed out waiting for batch {csv_basename!r} to show ready-to-finalize on the "
        "uploads table. Open FedEx batch uploads manually and confirm the blue ready count "
        "appears in the row."
    )


def _shipment_detail_viewing_count(page: Page) -> int | None:
    try:
        body = page.locator("body").inner_text(timeout=3000) or ""
    except Exception:
        return None
    m = re.search(r"viewing\s+(\d+)\s*/\s*(\d+)", body, re.I)
    if m:
        return int(m.group(1))
    return None


def _open_batch_shipments(
    page: Page,
    cfg: dict[str, Any],
    csv_basename: str,
    *,
    expected_ready: int = 0,
) -> None:
    """From batch uploads table, click the blue ready-to-finalize link (not the whole row)."""
    _ensure_batch_uploads_list(page, cfg)
    row = _find_batch_upload_row(page, csv_basename)
    if row.count() == 0:
        raise FedexBatchError(
            f"Batch row not found for {csv_basename!r} on uploads table."
        )

    ready, finalized, total = _parse_batch_row_progress(row)
    if expected_ready > 0 and ready != expected_ready:
        _log(
            f"WARN: ready count is {ready} (expected {expected_ready}); "
            "waiting briefly for FedEx to update the batch row…"
        )
        page.wait_for_timeout(3000)
        _ensure_batch_uploads_list(page, cfg)
        row = _find_batch_upload_row(page, csv_basename)
        ready, finalized, total = _parse_batch_row_progress(row)

    if ready <= 0:
        raise FedexBatchError(
            f"Batch {csv_basename!r} shows 0 ready to finalize "
            f"(finalized {finalized or 0}/{total or '?'}). "
            "Wait until the blue ready count appears before opening shipments."
        )

    ready_link = row.locator("a[href*='ready-to-finalize']").first
    if ready_link.count() == 0:
        ready_link = row.get_by_role("link", name=str(ready)).first
    if ready_link.count() == 0:
        raise FedexBatchError(
            f"Could not find ready-to-finalize link for {csv_basename!r} "
            f"({ready} shipment(s))."
        )

    _log(f"Clicking ready-to-finalize link ({ready} shipment(s)) for {csv_basename!r}…")
    ready_link.click(timeout=30_000)
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(1500)

    viewing = _shipment_detail_viewing_count(page)
    if viewing == 0:
        _log(
            "WARN: shipment detail shows VIEWING 0 — batch may still be processing; "
            "returning to uploads table and retrying once…"
        )
        _ensure_batch_uploads_list(page, cfg)
        row = _find_batch_upload_row(page, csv_basename)
        ready, _, _ = _parse_batch_row_progress(row)
        if ready <= 0:
            raise FedexBatchError(
                f"Opened {csv_basename!r} but shipment list was empty (0 orders). "
                "FedEx may still be processing the upload — try again in a few minutes."
            )
        ready_link = row.locator("a[href*='ready-to-finalize']").first
        ready_link.click(timeout=30_000)
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(2000)
        viewing = _shipment_detail_viewing_count(page)
        if viewing == 0:
            raise FedexBatchError(
                f"Shipment list for {csv_basename!r} still shows 0 orders after retry."
            )

    if viewing is not None:
        _log(f"Opened shipment list: viewing {viewing} shipment(s).")
    else:
        _log(f"Opened shipment list for {csv_basename!r}")


def _wait_for_shipment_list_loaded(
    page: Page,
    cfg: dict[str, Any],
    *,
    min_rows: int = 1,
    timeout_ms: int = 60_000,
) -> list[ShipmentRowState]:
    """Wait until the shipment table has parsed reference rows (Angular paint)."""
    deadline = time.monotonic() + timeout_ms / 1000.0
    last_count = 0
    while time.monotonic() < deadline:
        states = _scan_shipment_rows(page, cfg)
        if len(states) >= min_rows:
            _log(f"Shipment list loaded: {len(states)} row(s) with PO reference.")
            return states
        row_sel = _sel(cfg, "shipment_table_row", "table tbody tr.mat-mdc-row, tr.mat-mdc-row")
        try:
            last_count = page.locator(row_sel).count()
        except Exception:
            last_count = 0
        page.wait_for_timeout(500)
    states = _scan_shipment_rows(page, cfg)
    if states:
        _log(f"Shipment list loaded: {len(states)} row(s) with PO reference.")
        return states
    raise FedexBatchError(
        f"Shipment list did not load within {timeout_ms / 1000:.0f}s "
        f"({last_count} table row(s) visible, 0 with readable PO reference). "
        "Check fedex_batch.json selectors for shipment_table_row / reference column."
    )


def _row_reference_text(row) -> str:
    ref_cell = row.locator(
        "td.mat-column-reference, [data-label='Reference'], .cdk-column-reference"
    ).first
    if ref_cell.count() > 0:
        inner = ref_cell.locator('[data-test-id="rowText"]').first
        if inner.count() > 0:
            text = _normalize_reference(inner.inner_text() or "")
            if text:
                return text
        text = _normalize_reference(ref_cell.inner_text() or "")
        if text:
            m = _REFERENCE_RE.search(text)
            if m:
                return m.group(1)
    texts = row.locator('[data-test-id="rowText"]')
    for i in range(texts.count()):
        t = _normalize_reference(texts.nth(i).inner_text() or "")
        m = _REFERENCE_RE.search(t)
        if m:
            return m.group(1)
    try:
        row_text = _normalize_reference(row.inner_text() or "")
        m = _REFERENCE_RE.search(row_text)
        if m:
            return m.group(1)
    except Exception:
        pass
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


def _is_row_pending_finalize(status: str) -> bool:
    st = (status or "").lower()
    return "ready to be finalized" in st or "ready to finalize" in st


def _is_row_done(status: str, tracking: str) -> bool:
    if _is_row_pending_finalize(status):
        return False
    st = (status or "").lower()
    tr = (tracking or "").strip()
    if tr and re.search(r"\d{10,}", tr):
        return True
    if "shipment created" in st and "printed" in st:
        return True
    if "created & printed" in st or "created and printed" in st:
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


def _reference_matches_row(expected_ref: str, row_ref: str) -> bool:
    expected = _normalize_reference(expected_ref)
    actual = _normalize_reference(row_ref)
    if not expected or not actual:
        return False
    if expected == actual:
        return True
    expected_po, _ = parse_reference(expected)
    actual_po, _ = parse_reference(actual)
    if expected_po and expected_po == actual_po:
        return expected in actual or actual in expected
    return False


def _row_checkbox_locator(row, cfg: dict[str, Any]):
    cb_sel = _sel(cfg, "row_checkbox", "input[type='checkbox']")
    return row.locator(cb_sel).first


def _is_row_checked(row, cfg: dict[str, Any]) -> bool:
    cb = _row_checkbox_locator(row, cfg)
    try:
        if cb.count() > 0:
            return cb.is_checked()
    except Exception:
        pass
    try:
        return row.locator("mat-checkbox.mat-mdc-checkbox-checked").count() > 0
    except Exception:
        return False


def _click_row_checkbox(
    row,
    cfg: dict[str, Any],
    ref: str,
    *,
    ctrl_click: bool = False,
) -> bool:
    try:
        row.scroll_into_view_if_needed(timeout=5000)
    except Exception:
        pass
    if _is_row_checked(row, cfg):
        return True

    page = row.page
    cb_sel = _sel(cfg, "row_checkbox", "input[type='checkbox']")

    def _do_click(click_fn) -> bool:
        if ctrl_click:
            page.keyboard.down("Control")
        try:
            click_fn()
            page.wait_for_timeout(300)
            return _is_row_checked(row, cfg)
        finally:
            if ctrl_click:
                page.keyboard.up("Control")

    for label_sel in (
        "label.fdx-c-form-group__label[data-test-id='label']",
        "label.fdx-c-form-group__label",
        "mat-checkbox label",
    ):
        label = row.locator(label_sel).first
        try:
            if label.count() > 0 and label.is_visible():
                if _do_click(lambda: label.click(timeout=8000)):
                    return True
        except Exception:
            continue

    cb = row.locator(cb_sel).first
    try:
        if cb.count() > 0:
            if not cb.is_checked():
                if _do_click(lambda: cb.click(timeout=8000)):
                    return True
            return _is_row_checked(row, cfg)
    except Exception:
        pass
    try:
        if ctrl_click:
            page.keyboard.down("Control")
        try:
            cb.check(force=True, timeout=8000)
        finally:
            if ctrl_click:
                page.keyboard.up("Control")
        page.wait_for_timeout(250)
        return _is_row_checked(row, cfg)
    except Exception as exc:
        _log(f"WARN: could not check row {ref!r}: {exc}")
        return False


def _find_shipment_row_for_reference(page: Page, cfg: dict[str, Any], ref: str):
    row_sel = _sel(cfg, "shipment_table_row", "table tbody tr.mat-mdc-row, tr.mat-mdc-row")
    rows = page.locator(row_sel)
    for i in range(rows.count()):
        row = rows.nth(i)
        row_ref = _row_reference_text(row)
        if _reference_matches_row(ref, row_ref):
            return row
    return None


def _read_shipments_selected_count(page: Page) -> int | None:
    try:
        body = page.locator("body").inner_text(timeout=4000) or ""
    except Exception:
        return None
    patterns = (
        re.compile(r"(\d+)\s+shipments?\s+selected", re.I),
        re.compile(r"shipment\s+selected[:\s]+(\d+)", re.I),
        re.compile(r"(\d+)\s+selected\s+shipments?", re.I),
    )
    for pat in patterns:
        m = pat.search(body)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                continue
    return None


def _count_checked_refs(page: Page, cfg: dict[str, Any], group: list[ReferenceOrder]) -> list[str]:
    checked_refs: list[str] = []
    for order in group:
        row = _find_shipment_row_for_reference(page, cfg, order.reference)
        if row is not None and _is_row_checked(row, cfg):
            checked_refs.append(order.reference)
    return checked_refs


def _select_rows_for_group(page: Page, cfg: dict[str, Any], group: list[ReferenceOrder]) -> int:
    _clear_row_selection(page, cfg)
    page.wait_for_timeout(500)

    expected = len(group)
    for attempt in range(1, 4):
        missing = []
        for idx, order in enumerate(group):
            ref = order.reference
            row = _find_shipment_row_for_reference(page, cfg, ref)
            if row is None:
                missing.append(ref)
                continue
            if not _click_row_checkbox(row, cfg, ref, ctrl_click=idx > 0):
                missing.append(ref)
            else:
                _log(f"  Checked row {ref!r}")

        checked_refs = _count_checked_refs(page, cfg, group)
        ui_selected = _read_shipments_selected_count(page)
        checked_count = len(checked_refs)
        _log(
            f"Row selection attempt {attempt}: {checked_count}/{expected} checked "
            f"(UI selected={ui_selected if ui_selected is not None else '?'})"
        )
        if checked_count == expected and (ui_selected is None or ui_selected >= expected):
            settle_ms = _after_row_select_wait_ms(cfg)
            _log(f"Waiting {settle_ms}ms for selection toolbar before Finalize…")
            page.wait_for_timeout(settle_ms)
            return expected

        if missing:
            _log(f"WARN: could not find/check row(s): {', '.join(missing)}")
        if attempt < 3:
            page.wait_for_timeout(600)
            for order in group:
                row = _find_shipment_row_for_reference(page, cfg, order.reference)
                if row is not None and not _is_row_checked(row, cfg):
                    _click_row_checkbox(row, cfg, order.reference)

    checked_refs = _count_checked_refs(page, cfg, group)
    checked_count = len(checked_refs)
    ui_selected = _read_shipments_selected_count(page)
    _log(
        f"Row selection final: {checked_count}/{expected} checked "
        f"(UI selected={ui_selected if ui_selected is not None else '?'})"
    )
    return checked_count


def _label_tab_timeout_ms(cfg: dict[str, Any]) -> int:
    timing = cfg.get("timing") or {}
    raw = (
        str(timing.get("label_tab_timeout_ms") or "")
        or (os.environ.get("FEDEX_LABEL_TAB_TIMEOUT_MS") or "90000")
    ).strip()
    try:
        return max(15_000, int(raw))
    except ValueError:
        return 90_000


def _is_batch_list_page_content(p: Page) -> bool:
    """True when a page/tab is the FedEx batch shipment list (not a label PDF)."""
    try:
        body = (p.locator("body").inner_text(timeout=3000) or "").lower()
    except Exception:
        return False
    markers = (
        "batch shipping",
        "shipment selected",
        "clear selection",
        "ready to be finalized",
        "documents not printed",
        "shipment created & printed",
        "batch uploads",
    )
    return sum(1 for m in markers if m in body) >= 2


def _looks_like_label_tab(p: Page, list_page: Page) -> bool:
    if p == list_page:
        url = (p.url or "").lower()
        if "blob:" in url or url.endswith(".pdf"):
            return not _is_batch_list_page_content(p)
        return False
    if _is_batch_list_page_content(p):
        return False
    url = (p.url or "").lower()
    if "blob:" in url or ".pdf" in url or "print" in url:
        return True
    try:
        if p.locator("embed[type='application/pdf']").count() > 0:
            return True
        if p.locator(".pdfViewer, #viewer").count() > 0:
            return True
    except Exception:
        pass
    return True


def _wait_for_label_tab(
    list_page: Page,
    context: BrowserContext,
    pages_before: set[Page],
    *,
    timeout_ms: int,
) -> Page | None:
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        for candidate in context.pages:
            if candidate in pages_before:
                continue
            if _looks_like_label_tab(candidate, list_page):
                return candidate
        url = (list_page.url or "").lower()
        if ("blob:" in url or ".pdf" in url) and not _is_batch_list_page_content(list_page):
            return list_page
        list_page.wait_for_timeout(350)
    return None


def _click_finalize_and_print_manual(page: Page, cfg: dict[str, Any]) -> None:
    """Open Finalize dropdown → Finalize and print manually (label tab opens separately)."""
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


def _finalize_and_open_label_tab(
    list_page: Page,
    context: BrowserContext,
    cfg: dict[str, Any],
) -> Page:
    """Click Finalize and print manually; return the new tab that contains label PDF(s)."""
    pages_before = set(context.pages)
    tab_timeout = _label_tab_timeout_ms(cfg)
    label_tab: Page | None = None

    try:
        with context.expect_page(timeout=tab_timeout) as page_info:
            _click_finalize_and_print_manual(list_page, cfg)
        label_tab = page_info.value
        _log("Label tab opened (expect_page).")
    except PlaywrightTimeout:
        _log("Waiting for label tab after Finalize and print manually…")
        label_tab = _wait_for_label_tab(
            list_page, context, pages_before, timeout_ms=tab_timeout
        )

    if label_tab is None:
        raise FedexBatchError(
            "FedEx did not open a label browser tab after Finalize and print manually. "
            "Confirm labels open in a new tab when you finalize manually."
        )

    if _is_batch_list_page_content(label_tab):
        raise FedexBatchError(
            "Opened tab is still the batch shipment list, not shipping labels. "
            "Refusing to save the wrong page as a label PDF."
        )

    try:
        label_tab.wait_for_load_state("domcontentloaded", timeout=30_000)
    except PlaywrightTimeout:
        _log("WARN: label tab domcontentloaded timeout — continuing.")
    label_tab.wait_for_timeout(min(pdf_page_wait_ms(), 6000))
    _log(f"Label tab ready: {label_tab.url[:140]}")
    return label_tab


def _close_label_tabs(
    list_page: Page,
    context: BrowserContext,
    pages_before: set[Page],
) -> None:
    """Close label tab(s) opened for printing; keep the batch shipment list tab."""
    for candidate in list(context.pages):
        if candidate == list_page:
            continue
        if candidate in pages_before:
            continue
        try:
            candidate.close()
            _log("Closed label tab.")
        except Exception:
            pass
    try:
        list_page.bring_to_front()
    except Exception:
        pass


def _pdf_looks_like_batch_list_ui(path: Path) -> bool:
    try:
        head = path.read_bytes()[:24_000]
    except OSError:
        return True
    if not head.startswith(b"%PDF"):
        return True
    text = head.decode("latin-1", errors="ignore").upper()
    markers = (
        "BATCH SHIPPING",
        "SHIPMENT SELECTED",
        "CLEAR SELECTION",
        "READY TO BE FINALIZED",
        "DOCUMENTS NOT PRINTED",
    )
    return sum(1 for m in markers if m in text) >= 2


_BLOB_TO_B64_JS = """async (href) => {
    const url = href || location.href;
    if (!url || !url.startsWith('blob:')) return null;
    const resp = await fetch(url);
    const buf = await resp.arrayBuffer();
    const bytes = new Uint8Array(buf);
    let binary = '';
    const chunk = 0x8000;
    for (let i = 0; i < bytes.length; i += chunk) {
        binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
    }
    return btoa(binary);
}"""


def _label_tab_pdf_url(page: Page) -> str | None:
    url = (page.url or "").strip()
    if url.startswith("blob:"):
        return url
    if url.startswith(("http://", "https://")):
        low = url.lower()
        if low.endswith(".pdf") or ".pdf?" in low or "label" in low or "print" in low:
            return url
    try:
        src = page.evaluate(
            """() => {
                const el = document.querySelector(
                    'embed[type="application/pdf"], object[type="application/pdf"]'
                );
                if (!el) return null;
                return el.src || el.data || null;
            }"""
        )
        if isinstance(src, str) and src.strip():
            return src.strip()
    except Exception:
        pass
    return url if url.startswith(("blob:", "http://", "https://")) else None


def _fetch_pdf_bytes_via_blob(page: Page, href: str | None = None) -> bytes | None:
    target = (href or page.url or "").strip()
    if not target.startswith("blob:"):
        return None
    try:
        b64 = page.evaluate(_BLOB_TO_B64_JS, target)
        if not b64:
            return None
        data = base64.b64decode(b64)
        if len(data) > 800 and data.startswith(b"%PDF"):
            return data
    except Exception as exc:
        _log(f"WARN: blob PDF fetch failed: {exc}")
    return None


def _fetch_pdf_bytes_via_http(context: BrowserContext, url: str) -> bytes | None:
    if not url.startswith(("http://", "https://")):
        return None
    try:
        resp = context.request.get(url, timeout=60_000)
        body = resp.body()
        if resp.ok and body.startswith(b"%PDF") and len(body) > 800:
            return body
    except Exception as exc:
        _log(f"WARN: HTTP PDF fetch failed: {exc}")
    return None


def _fetch_label_pdf_bytes(page: Page, context: BrowserContext) -> bytes | None:
    """Read actual label PDF bytes from Edge's PDF viewer tab (not printToPDF)."""
    pdf_url = _label_tab_pdf_url(page)
    if pdf_url:
        if pdf_url.startswith("blob:"):
            data = _fetch_pdf_bytes_via_blob(page, pdf_url)
            if data:
                _log(f"Fetched label PDF from blob URL ({len(data):,} bytes).")
                return data
        if pdf_url.startswith(("http://", "https://")):
            data = _fetch_pdf_bytes_via_http(context, pdf_url)
            if data:
                _log(f"Fetched label PDF from HTTP URL ({len(data):,} bytes).")
                return data
    data = _fetch_pdf_bytes_via_blob(page)
    if data:
        _log(f"Fetched label PDF from tab blob ({len(data):,} bytes).")
        return data
    return None


def _wait_for_label_pdf_ready(page: Page, *, timeout_ms: int) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        pdf_url = _label_tab_pdf_url(page)
        if pdf_url and pdf_url.startswith("blob:"):
            try:
                size = page.evaluate(
                    """async (href) => {
                        try {
                            const r = await fetch(href || location.href);
                            return (await r.arrayBuffer()).byteLength;
                        } catch (e) {
                            return 0;
                        }
                    }""",
                    pdf_url,
                )
                if isinstance(size, (int, float)) and size > 2000:
                    return True
            except Exception:
                pass
        elif pdf_url and pdf_url.startswith("http"):
            return True
        try:
            if page.locator("embed[type='application/pdf']").count() > 0:
                return True
        except Exception:
            pass
        page.wait_for_timeout(400)
    return False


def _pdf_looks_like_blank_label(path: Path) -> bool:
    try:
        data = path.read_bytes()
    except OSError:
        return True
    if len(data) < 800 or not data.startswith(b"%PDF"):
        return True
    text = data[:160_000].decode("latin-1", errors="ignore").lower()
    label_signals = (
        "fedex",
        "tracking",
        "ship date",
        "weight",
        "barcode",
        "/image",
        "express",
        "ground",
    )
    if any(sig in text for sig in label_signals):
        return False
    if text.count(" re ") < 2 and "/font" not in text and "/image" not in text:
        return True
    return len(data) < 4000


def _pdf_is_valid_label(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 800:
        return False
    if _pdf_looks_like_batch_list_ui(path):
        return False
    if _pdf_looks_like_blank_label(path):
        return False
    return True


def _label_save_verify_timeout_s() -> float:
    raw = (os.environ.get("FEDEX_LABEL_SAVE_VERIFY_TIMEOUT_S") or "25").strip()
    try:
        return max(5.0, float(raw))
    except ValueError:
        return 25.0


def _wait_label_pdf_on_disk(dest: Path, *, timeout_s: float | None = None) -> bool:
    """UNC shares can lag — poll until the PDF exists and looks like a real label."""
    deadline = time.monotonic() + (timeout_s or _label_save_verify_timeout_s())
    while time.monotonic() < deadline:
        if _pdf_is_valid_label(dest):
            return True
        time.sleep(0.5)
    return _pdf_is_valid_label(dest)


def _persist_label_pdf_to_disk(dest: Path, data: bytes) -> bool:
    """Write PDF bytes and confirm the file exists at the exact target path."""
    if len(data) < 800 or not data.startswith(b"%PDF"):
        return False
    dest = dest.resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(f"{dest.stem}.partial{dest.suffix}")
    try:
        partial.write_bytes(data)
        if not _pdf_is_valid_label(partial):
            return False
        if dest.is_file():
            dest.unlink()
        partial.replace(dest)
    except OSError as exc:
        _log(f"ERROR: could not write label PDF to {dest}: {exc}")
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    finally:
        try:
            if partial.is_file():
                partial.unlink(missing_ok=True)
        except OSError:
            pass

    if _wait_label_pdf_on_disk(dest):
        return True
    _log(f"ERROR: label PDF not found on disk after write: {dest}")
    try:
        dest.unlink(missing_ok=True)
    except OSError:
        pass
    return False


def _save_via_edge_pdf_viewer(page: Page, dest: Path, *, timeout_s: float) -> bool:
    """Edge built-in PDF viewer: Ctrl+S / Save → download or Windows Save As."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        page.bring_to_front()
        page.wait_for_timeout(600)
    except Exception:
        pass

    download_ms = int(min(timeout_s, 45) * 1000)
    try:
        with page.expect_download(timeout=download_ms) as dl_info:
            page.keyboard.press("Control+s")
        dl_info.value.save_as(dest)
        if _pdf_is_valid_label(dest):
            _log(f"Saved label PDF via browser download ({dest.stat().st_size:,} bytes).")
            return True
        dest.unlink(missing_ok=True)
    except Exception:
        _log("No direct download from Ctrl+S; trying Save As dialog…")

    try:
        page.keyboard.press("Control+s")
    except Exception:
        return False
    time.sleep(0.9)
    hwnd = wait_for_save_as_dialog(timeout_s=min(timeout_s, 20))
    if not hwnd:
        return False
    if not fill_save_as_dialog(dest, timeout_s=timeout_s, dialog_hwnd=hwnd):
        return False
    return _wait_label_pdf_on_disk(dest, timeout_s=_label_save_verify_timeout_s())


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


def _save_pdf_from_label_tab(
    label_tab: Page,
    context: BrowserContext,
    dest: Path,
    cfg: dict[str, Any],
) -> bool:
    """Save shipping label PDF from the tab FedEx opens after Finalize and print manually."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    _log(f"Saving label PDF from print tab → {dest}")

    if _is_batch_list_page_content(label_tab):
        _log("ERROR: refusing to save — target tab is the batch list, not labels.")
        return False

    ready_ms = max(pdf_page_wait_ms(), 12_000)
    if not _wait_for_label_pdf_ready(label_tab, timeout_ms=ready_ms):
        _log("WARN: label PDF may not be fully loaded yet — trying capture anyway.")

    saved = False
    verify_s = _label_save_verify_timeout_s()

    pdf_bytes = _fetch_label_pdf_bytes(label_tab, context)
    if pdf_bytes and _persist_label_pdf_to_disk(dest, pdf_bytes):
        saved = True

    if not saved and bool(cfg.get("label_save", {}).get("use_native_save_dialog", True)):
        if _save_via_edge_pdf_viewer(
            label_tab, dest, timeout_s=label_save_timeout_s()
        ) and _wait_label_pdf_on_disk(dest, timeout_s=verify_s):
            saved = True

    if not saved:
        _log("WARN: blob/download capture failed; trying printToPDF as last resort.")
        fd, tmp_name = tempfile.mkstemp(suffix=".pdf", prefix="fedex_label_cdp_")
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            if _cdp_save_pdf(label_tab, context, tmp) and _pdf_is_valid_label(tmp):
                data = tmp.read_bytes()
                saved = _persist_label_pdf_to_disk(dest, data)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    if not saved or not _wait_label_pdf_on_disk(dest, timeout_s=verify_s):
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
        _log(
            f"ERROR: label PDF was not saved to disk at:\n  {dest}\n"
            "Confirm the label tab shows FedEx labels and the share folder is writable."
        )
        return False

    size = dest.stat().st_size
    pages = _count_pdf_pages(dest)
    _log(
        f"Confirmed label PDF on disk ({size:,} bytes, {pages} page(s)) → {dest}"
    )
    return True


def _restore_windows_default_printer(old_name: str | None) -> None:
    if not old_name:
        return
    try:
        import win32print

        win32print.SetDefaultPrinter(old_name)
    except Exception:
        pass


def _set_windows_default_printer(printer: str) -> str | None:
    try:
        import win32print

        old = win32print.GetDefaultPrinter()
        win32print.SetDefaultPrinter(printer)
        return old
    except Exception as exc:
        _log(f"WARN: could not set Windows default printer to {printer!r}: {exc}")
        return None


def _wait_edge_print_preview(page: Page, *, timeout_ms: int) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        try:
            if page.locator("print-preview-app").count() > 0:
                return True
            if page.get_by_text("Printer", exact=True).count() > 0:
                return True
            if page.get_by_text(re.compile(r"Total\s+\d+\s+sheet", re.I)).count() > 0:
                return True
        except Exception:
            pass
        page.wait_for_timeout(300)
    return False


def _edge_click_pdf_toolbar_print(page: Page) -> bool:
    for sel in (
        "#print",
        "cr-icon-button#print",
        "button#print",
        "[aria-label='Print']",
        "[title='Print']",
    ):
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                loc.click(timeout=8000)
                return True
        except Exception:
            continue
    try:
        return bool(
            page.evaluate(
                """() => {
                    const tryRoot = (root) => {
                        if (!root) return false;
                        for (const el of root.querySelectorAll(
                            '#print, cr-icon-button#print, [aria-label="Print"], [title="Print"]'
                        )) {
                            el.click();
                            return true;
                        }
                        for (const el of root.querySelectorAll('*')) {
                            if (el.shadowRoot && tryRoot(el.shadowRoot)) return true;
                        }
                        return false;
                    };
                    return tryRoot(document);
                }"""
            )
        )
    except Exception:
        return False


def _edge_printer_already_selected(page: Page, printer: str) -> bool:
    try:
        body = page.locator("body").inner_text(timeout=3000) or ""
    except Exception:
        return False
    for line in body.splitlines():
        if _printer_name_matches(line.strip(), printer):
            return True
    return False


def _edge_select_printer(page: Page, printer: str) -> bool:
    if _edge_printer_already_selected(page, printer):
        _log(f"Printer already set to {printer!r} in Edge print preview.")
        return True

    for root_sel in ("print-preview-app >> select", "select"):
        try:
            loc = page.locator(root_sel).first
            if loc.count() == 0:
                continue
            for i in range(loc.locator("option").count()):
                text = (loc.locator("option").nth(i).inner_text() or "").strip()
                if _printer_name_matches(text, printer):
                    loc.select_option(label=text)
                    page.wait_for_timeout(500)
                    return True
        except Exception:
            continue

    for sel in (
        "print-preview-app >> select",
        "print-preview-app >> cr-select",
        "print-preview-app >> [role='combobox']",
    ):
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                loc.click(timeout=3000)
                page.wait_for_timeout(500)
                break
        except Exception:
            continue
    else:
        try:
            page.get_by_text("Printer", exact=True).first.click(timeout=3000)
            page.wait_for_timeout(500)
        except Exception:
            pass

    try:
        zebra = page.get_by_text(re.compile(r"Zebra\s+ZP\s+450", re.I))
        if zebra.count() > 0:
            zebra.first.click(timeout=8000)
            page.wait_for_timeout(500)
            return True
    except Exception:
        pass

    try:
        return bool(
            page.evaluate(
                """(printer) => {
                    const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                    const match = (text) => {
                        const n = norm(text);
                        return n.includes('zebra') && (n.includes('450') || n.includes('zp'));
                    };
                    const walk = (root) => {
                        if (!root) return false;
                        const sel = root.querySelector('select');
                        if (sel) {
                            for (const opt of sel.options) {
                                if (match(opt.text)) {
                                    sel.value = opt.value;
                                    sel.dispatchEvent(new Event('change', { bubbles: true }));
                                    return true;
                                }
                            }
                        }
                        for (const el of root.querySelectorAll('div, span')) {
                            if (match(el.textContent || '')) {
                                el.click();
                                return true;
                            }
                        }
                        for (const el of root.querySelectorAll('*')) {
                            if (el.shadowRoot && walk(el.shadowRoot)) return true;
                        }
                        return false;
                    };
                    const app = document.querySelector('print-preview-app');
                    if (app && app.shadowRoot && walk(app.shadowRoot)) return true;
                    return walk(document);
                }""",
                printer,
            )
        )
    except Exception:
        return False


def _edge_click_print_submit(page: Page) -> bool:
    for sel in (
        "print-preview-app >> #action-button",
        "print-preview-app >> cr-button#action-button",
        "print-preview-app >> button:has-text('Print')",
        "button:has-text('Print')",
        "[role='button']:has-text('Print')",
    ):
        try:
            btn = page.locator(sel).first
            if btn.count() > 0 and btn.is_visible():
                btn.click(timeout=10_000)
                return True
        except Exception:
            continue
    try:
        return bool(
            page.evaluate(
                """() => {
                    const clickPrint = (root) => {
                        if (!root) return false;
                        for (const el of root.querySelectorAll(
                            '#action-button, button, cr-button, [role="button"]'
                        )) {
                            const t = (el.textContent || '').trim().toLowerCase();
                            if (t === 'print') {
                                el.click();
                                return true;
                            }
                        }
                        for (const el of root.querySelectorAll('*')) {
                            if (el.shadowRoot && clickPrint(el.shadowRoot)) return true;
                        }
                        return false;
                    };
                    const app = document.querySelector('print-preview-app');
                    if (app && app.shadowRoot && clickPrint(app.shadowRoot)) return true;
                    return clickPrint(document);
                }"""
            )
        )
    except Exception:
        return False


def _print_warehouse_label_from_tab(
    label_tab: Page,
    printer: str,
    cfg: dict[str, Any],
) -> bool:
    """Warehouse vendors: Edge label tab → Print → Zebra → submit (not saved to share)."""
    pause_ms = warehouse_print_pause_ms()
    after_ms = warehouse_after_print_ms()
    preview_timeout_ms = max(20_000, pause_ms * 3)

    try:
        label_tab.bring_to_front()
    except Exception:
        pass

    if not _wait_for_label_pdf_ready(label_tab, timeout_ms=max(pdf_page_wait_ms(), 12_000)):
        _log("WARN: label PDF may not be fully loaded before warehouse print.")

    _log(f"Warehouse print: waiting {pause_ms}ms on label tab, then opening Edge print UI…")
    label_tab.wait_for_timeout(pause_ms)

    old_default = _set_windows_default_printer(printer)

    opened = False
    if _edge_click_pdf_toolbar_print(label_tab):
        _log("Clicked PDF viewer Print button.")
        opened = True
    if not opened:
        label_tab.keyboard.press("Control+p")
        _log("Opened print preview via Ctrl+P.")
        opened = True

    label_tab.wait_for_timeout(1200)
    if not _wait_edge_print_preview(label_tab, timeout_ms=preview_timeout_ms):
        _log("ERROR: Edge print preview did not appear for warehouse label.")
        _restore_windows_default_printer(old_default)
        return False

    if not _edge_select_printer(label_tab, printer):
        _log(f"WARN: could not select {printer!r}; continuing with current printer.")
    else:
        _log(f"Printer set to {printer!r} in Edge print preview.")

    label_tab.wait_for_timeout(800)
    if not _edge_click_print_submit(label_tab):
        _log("WARN: Print button not found; sending Enter to submit.")
        label_tab.keyboard.press("Enter")

    _log(f"Waiting {after_ms}ms for Zebra to receive label…")
    label_tab.wait_for_timeout(after_ms)
    try:
        label_tab.keyboard.press("Escape")
        label_tab.wait_for_timeout(400)
    except Exception:
        pass

    _restore_windows_default_printer(old_default)
    _log(f"Warehouse label sent to {printer!r} via Edge print UI.")
    return True


def _print_warehouse_label_with_fallback(
    label_tab: Page,
    context: BrowserContext,
    cfg: dict[str, Any],
    *,
    printer: str,
    order_ref: str,
) -> bool:
    if _print_warehouse_label_from_tab(label_tab, printer, cfg):
        return True

    _log(f"Edge print UI failed for {order_ref!r}; trying saved-PDF fallback…")
    fd, tmp_name = tempfile.mkstemp(suffix=".pdf", prefix="fedex_warehouse_label_")
    os.close(fd)
    dest = Path(tmp_name)
    try:
        if not _save_pdf_from_label_tab(label_tab, context, dest, cfg):
            return False
        print_pdf_windows(dest, printer)
        label_tab.wait_for_timeout(warehouse_after_print_ms())
        _log(f"Submitted Zebra print via Windows fallback for {order_ref!r}.")
        return True
    except Exception as exc:
        _log(f"ERROR: warehouse print fallback failed for {order_ref!r}: {exc}")
        return False
    finally:
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass


def _count_pdf_pages(path: Path) -> int:
    try:
        if not path.is_file() or path.stat().st_size < 200:
            return 0
        from pypdf import PdfReader

        return len(PdfReader(str(path)).pages)
    except Exception:
        try:
            data = path.read_bytes()[:500_000]
        except OSError:
            return 0
        if not data.startswith(b"%PDF"):
            return 0
        count = len(re.findall(rb"/Type\s*/Page\b", data))
        return max(1, count) if count else 0


def _merge_pdf_files(sources: list[Path], dest: Path) -> None:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for src in sources:
        reader = PdfReader(str(src))
        for page in reader.pages:
            writer.add_page(page)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as f:
        writer.write(f)


def _zebra_label_printer() -> str:
    name, _source = resolve_fedex_warehouse_label_printer()
    return name


def _zebra_label_printer_source() -> str:
    _name, source = resolve_fedex_warehouse_label_printer()
    return source


def _warehouse_labels_use_queue() -> bool:
    return warehouse_label_print_mode() == "queue"


def _save_warehouse_label_to_queue(
    label_tab: Page,
    context: BrowserContext,
    cfg: dict[str, Any],
    *,
    vendor: str,
    order_date: date | None,
) -> bool:
    """Save label PDF to the print queue; background watcher sends it to the Zebra."""
    dest = warehouse_label_queue_path(vendor, order_date)
    if not _save_pdf_from_label_tab(label_tab, context, dest, cfg):
        return False
    _log(f"Warehouse label saved to print queue: {dest}")
    return True


def _capture_label_from_finalize(
    list_page: Page,
    context: BrowserContext,
    cfg: dict[str, Any],
    *,
    orders: list[ReferenceOrder],
) -> Page:
    selected = _select_rows_for_group(list_page, cfg, orders)
    if selected != len(orders):
        refs = [o.reference for o in orders]
        raise FedexBatchError(
            f"Could not check all {len(orders)} row(s) before Finalize "
            f"({selected} checked). References: {', '.join(refs)}"
        )
    return _finalize_and_open_label_tab(list_page, context, cfg)


def _finalize_and_save_vendor_group(
    page: Page,
    context: BrowserContext,
    cfg: dict[str, Any],
    group: list[ReferenceOrder],
    *,
    order_date: date | None,
) -> tuple[bool, int]:
    """Select all rows in the group, Finalize once, save/print one label PDF."""
    vendor = group[0].vendor_folder
    warehouse = is_warehouse_print_vendor(vendor)
    refs = [o.reference for o in group]

    if len(group) > 1:
        _log(
            f"  → Checking all {len(group)} row(s) together, then one Finalize: "
            f"{', '.join(refs)}"
        )

    pages_before = set(context.pages)
    try:
        label_tab = _capture_label_from_finalize(page, context, cfg, orders=group)
        try:
            if warehouse:
                if _warehouse_labels_use_queue():
                    ok = _save_warehouse_label_to_queue(
                        label_tab,
                        context,
                        cfg,
                        vendor=vendor,
                        order_date=order_date,
                    )
                    if not ok:
                        return False, 0
                    dest = warehouse_label_queue_path(vendor, order_date)
                    pages = _count_pdf_pages(dest)
                    return True, pages or len(group)
                printer = _zebra_label_printer()
                ok = _print_warehouse_label_with_fallback(
                    label_tab,
                    context,
                    cfg,
                    printer=printer,
                    order_ref=refs[0] if len(refs) == 1 else f"{vendor} ({len(group)} labels)",
                )
                return ok, len(group) if ok else 0

            dest = vendor_label_pdf_path(vendor, order_date)
            if not _save_pdf_from_label_tab(label_tab, context, dest, cfg):
                return False, 0
            pages = _count_pdf_pages(dest)
            if pages < len(group):
                _log(
                    f"WARN: saved PDF has {pages} page(s) but {len(group)} "
                    f"shipment(s) were selected for {vendor!r}."
                )
            return True, pages
        finally:
            _close_label_tabs(page, context, pages_before)
            if warehouse and not _warehouse_labels_use_queue():
                settle_ms = warehouse_after_print_ms()
            else:
                settle_ms = 1200
            page.wait_for_timeout(settle_ms)
            try:
                page.wait_for_load_state("domcontentloaded")
            except PlaywrightTimeout:
                pass
    except FedexBatchError:
        raise
    except Exception as exc:
        _log(f"ERROR: batch finalize/save failed for {vendor!r}: {exc}")
        return False, 0


def _process_vendor_group_labels_sequential(
    page: Page,
    context: BrowserContext,
    cfg: dict[str, Any],
    group: list[ReferenceOrder],
    *,
    order_date: date | None,
) -> tuple[bool, int]:
    """Fallback: one row at a time when batch select/finalize fails."""
    vendor = group[0].vendor_folder
    warehouse = is_warehouse_print_vendor(vendor)
    label_parts: list[Path] = []
    printed_jobs = 0

    _log(f"  → Fallback: finalize {len(group)} shipment(s) one at a time for {vendor!r}")
    for idx, order in enumerate(group, start=1):
        pages_before = set(context.pages)
        try:
            label_tab = _capture_label_from_finalize(
                page, context, cfg, orders=[order]
            )
            try:
                if warehouse:
                    if _warehouse_labels_use_queue():
                        fd, tmp_name = tempfile.mkstemp(
                            suffix=".pdf", prefix=f"fedex_wh_label_{idx}_"
                        )
                        os.close(fd)
                        part = Path(tmp_name)
                        try:
                            if _save_pdf_from_label_tab(label_tab, context, part, cfg):
                                label_parts.append(part)
                                part = None
                        finally:
                            if part is not None:
                                part.unlink(missing_ok=True)
                    elif _print_warehouse_label_with_fallback(
                        label_tab,
                        context,
                        cfg,
                        printer=_zebra_label_printer(),
                        order_ref=order.reference,
                    ):
                        printed_jobs += 1
                else:
                    fd, tmp_name = tempfile.mkstemp(
                        suffix=".pdf", prefix=f"fedex_label_{idx}_"
                    )
                    os.close(fd)
                    part = Path(tmp_name)
                    try:
                        if _save_pdf_from_label_tab(label_tab, context, part, cfg):
                            label_parts.append(part)
                            part = None
                    finally:
                        if part is not None:
                            part.unlink(missing_ok=True)
            finally:
                _close_label_tabs(page, context, pages_before)
                page.wait_for_timeout(1200)
        except FedexBatchError as exc:
            _log(f"WARN: sequential save failed for {order.reference!r}: {exc}")

    if warehouse:
        if _warehouse_labels_use_queue():
            if not label_parts:
                return False, 0
            dest = warehouse_label_queue_path(vendor, order_date)
            if len(label_parts) == 1:
                if not _persist_label_pdf_to_disk(dest, label_parts[0].read_bytes()):
                    return False, 0
            else:
                _merge_pdf_files(label_parts, dest)
                if not _wait_label_pdf_on_disk(dest):
                    return False, 0
            for part in label_parts:
                part.unlink(missing_ok=True)
            pages = _count_pdf_pages(dest)
            _log(f"Warehouse label saved to print queue: {dest}")
            return True, pages or len(label_parts)
        return printed_jobs > 0, printed_jobs

    if not label_parts:
        return False, 0
    dest = vendor_label_pdf_path(vendor, order_date)
    if len(label_parts) == 1:
        if not _persist_label_pdf_to_disk(dest, label_parts[0].read_bytes()):
            return False, 0
    else:
        _merge_pdf_files(label_parts, dest)
        if not _wait_label_pdf_on_disk(dest):
            return False, 0
    for part in label_parts:
        part.unlink(missing_ok=True)
    return True, _count_pdf_pages(dest)


def _process_vendor_group_labels(
    page: Page,
    context: BrowserContext,
    cfg: dict[str, Any],
    group: list[ReferenceOrder],
    *,
    order_date: date | None,
) -> tuple[bool, int]:
    """Finalize all vendor rows together; fall back to one-at-a-time if needed."""
    vendor = group[0].vendor_folder
    try:
        ok, count = _finalize_and_save_vendor_group(
            page, context, cfg, group, order_date=order_date
        )
        if ok:
            return ok, count
    except FedexBatchError as exc:
        _log(f"WARN: batch finalize for {vendor!r} failed: {exc}")

    if len(group) <= 1:
        return False, 0
    return _process_vendor_group_labels_sequential(
        page, context, cfg, group, order_date=order_date
    )


def _print_label_pdf(
    label_tab: Page,
    context: BrowserContext,
    cfg: dict[str, Any],
    *,
    vendor: str,
) -> bool:
    """Capture label PDF from the print tab and send to the warehouse Zebra."""
    fd, tmp_name = tempfile.mkstemp(suffix=".pdf", prefix="fedex_warehouse_label_")
    os.close(fd)
    dest = Path(tmp_name)
    printer = _zebra_label_printer()
    _log(f"Warehouse vendor {vendor!r}: printing labels on {printer!r} (not saving to share)")

    try:
        if not _save_pdf_from_label_tab(label_tab, context, dest, cfg):
            _log(f"WARN: could not capture label PDF for {vendor!r}")
            return False
        print_pdf_windows(dest, printer)
        time.sleep(2.0)
        _log(f"Submitted Zebra print job for {vendor!r} on {printer!r}")
        return True
    except Exception as exc:
        _log(f"ERROR: Zebra print failed for {vendor!r}: {exc}")
        return False
    finally:
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
    expected_row_count: int = 0,
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
    loaded = _wait_for_shipment_list_loaded(page, cfg, min_rows=1)
    if expected_row_count > 0 and len(loaded) < expected_row_count:
        _log(
            f"WARN: batch reported {expected_row_count} ready shipment(s) but parsed "
            f"{len(loaded)} row reference(s) on the list page."
        )

    pass_num = 0
    while pass_num < 50:
        pass_num += 1
        states = _scan_shipment_rows(page, cfg)
        pending = [s for s in states if not s.done]
        if not pending:
            if not states:
                raise FedexBatchError(
                    "No shipment rows found on the batch list — cannot finalize or print labels."
                )
            ready_count = sum(1 for s in states if _is_row_pending_finalize(s.status))
            if ready_count > 0:
                raise FedexBatchError(
                    f"{ready_count} shipment(s) still show 'ready to be finalized' but were not "
                    "selected/processed. Check row checkbox selectors in fedex_batch.json."
                )
            _log(
                f"All {len(states)} shipment row(s) finalized "
                "(tracking present or status printed)."
            )
            break

        _log(f"Pass {pass_num}: {len(pending)} shipment(s) pending finalize/print.")

        orders = [s.order for s in pending]
        groups = group_by_vendor(orders)
        if not groups:
            break

        group = groups[0]
        vendor = group[0].vendor_folder
        skus = ", ".join(o.sku for o in group)
        _log(f"Vendor group {vendor!r}: {len(group)} shipment(s) — SKU(s): {skus}")
        if is_warehouse_print_vendor(vendor):
            if _warehouse_labels_use_queue():
                dest = warehouse_label_queue_path(vendor, order_date)
                _log(
                    f"  → Warehouse print queue → Zebra {_zebra_label_printer()!r} "
                    f"({_zebra_label_printer_source()}): save PDF, watcher prints — {dest}"
                )
            else:
                _log(
                    f"  → Zebra ({_zebra_label_printer()!r}): warehouse-print vendor — "
                    "Edge print UI (legacy mode)"
                )
        else:
            dest = vendor_label_pdf_path(vendor, order_date)
            _log(f"  → Save PDF: {dest}")
            if len(group) > 1:
                _log(
                    f"  → {len(group)} orders: check all rows, one Finalize, "
                    f"one PDF with all labels"
                )

        ok, label_count = _process_vendor_group_labels(
            page, context, cfg, group, order_date=order_date
        )
        if is_warehouse_print_vendor(vendor):
            if ok:
                printed_groups += 1
            elif _warehouse_labels_use_queue():
                _log(f"WARN: could not save warehouse label PDF to print queue for {vendor!r}")
            else:
                _log(f"WARN: could not print all labels on Zebra for {vendor!r}")
        elif ok:
            saved_pdfs += 1
            if len(group) > 1:
                dest = vendor_label_pdf_path(vendor, order_date)
                _log(
                    f"Saved {label_count} label(s) for {vendor!r} "
                    f"({_count_pdf_pages(dest)} page(s) in {dest.name})"
                )
        else:
            _log(f"WARN: could not save label PDF for {vendor!r}")

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


def run_fedex_login_test(*, config_path: Path) -> int:
    """
    Open FedEx sign-in for manual entry only — no auto-fill, no CSV upload.
    Verifies the batch page loads afterward and saves fedex_storage_state.json.
    """
    cfg = _load_config(config_path)
    _log("FedEx login test: you type username/password in the browser (no auto-fill).")

    browser_cfg = cfg.get("browser", {})
    slow_mo = int(browser_cfg.get("slow_mo_ms", 0))
    default_timeout = int(browser_cfg.get("default_timeout_ms", 120_000))

    with sync_playwright() as p:
        browser, context, page, persistent = _open_fedex_browser(
            p, cfg, headless=False, slow_mo=slow_mo
        )
        page.set_default_timeout(default_timeout)
        try:
            _login_if_needed(page, cfg, creds=None, manual_login=True)

            batch_url = _batch_url(cfg)
            if not _is_batch_page(page):
                _log(f"Verifying batch uploads page: {batch_url}")
                page.goto(batch_url, wait_until="domcontentloaded", timeout=120_000)
                _fedex_short_pause(page, cfg, ms=600)
                _maybe_accept_fedex_cookies(page, cfg, peel_overlays=False)

            if _retry_button_visible(page, cfg):
                raise FedexBatchError(
                    "Still on FedEx Retry / failed-to-load after manual login. "
                    "FedEx may be blocking this browser profile — try signing in once in "
                    "your normal Edge/Chrome, or set FEDEX_BROWSER_CHANNEL=chrome in .env."
                )
            if not _is_batch_page(page):
                raise FedexBatchError(
                    f"Batch uploads page not detected after manual login (URL: {page.url}). "
                    "Confirm the account can open FedEx Shipping Plus batch import."
                )

            _log("SUCCESS: Batch uploads page is ready after manual login.")
            _save_fedex_session(context, uses_persistent_profile=persistent)
        finally:
            context.close()
            if browser is not None:
                browser.close()

    return 0


def run_fedex_batch(
    *,
    config_path: Path,
    order_date: date | None = None,
    csv_path: Path | None = None,
    plan_only: bool = False,
    skip_upload: bool = False,
    dry_run: bool = False,
    manual_login: bool = False,
    skip_auto_login: bool = False,
) -> int:
    cfg = _load_config(config_path)
    d = order_date or date.today()

    manual_login = manual_login or _env_truthy("FEDEX_MANUAL_LOGIN")
    skip_auto_login = skip_auto_login or _env_truthy("FEDEX_SKIP_AUTO_LOGIN")

    creds: FedexCredentials | None = None
    if not plan_only and not dry_run and not manual_login:
        try:
            creds = load_fedex_credentials(cfg)
            _log(f"FedEx credentials loaded for {creds.username!r} (from {env_file_path()})")
        except ValueError as exc:
            raise FedexBatchError(str(exc)) from exc
    elif not plan_only and not dry_run and manual_login:
        _log("Manual login: credentials in .env are not used (you type them in the browser).")

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

    warehouse_watcher: FedexWarehouseLabelWatcher | None = None
    if warehouse_vendors:
        try:
            zebra, zebra_source = resolve_fedex_warehouse_label_printer()
            _log(f"Warehouse Zebra printer: {zebra!r} (from {zebra_source})")
        except Exception as exc:
            raise FedexBatchError(
                f"Cannot resolve Zebra printer for warehouse labels: {exc}"
            ) from exc
        if _warehouse_labels_use_queue():
            _log(f"Warehouse labels: save to queue at {WAREHOUSE_LABEL_QUEUE_DIR}")
            warehouse_watcher = FedexWarehouseLabelWatcher.for_queue_dir(WAREHOUSE_LABEL_QUEUE_DIR)
            warehouse_watcher.start()
        else:
            _log("Warehouse labels: legacy Edge print UI (FEDEX_WAREHOUSE_LABEL_PRINT_MODE=edge)")

    if manual_login:
        _log("Login mode: manual (browser sign-in, no auto-fill).")
    elif skip_auto_login:
        _log("Login mode: saved session only; manual prompt if session expired.")

    browser_cfg = cfg.get("browser", {})
    headless = bool(browser_cfg.get("headless", False))
    if manual_login and headless:
        _log("WARN: manual login requires a visible browser; forcing headless=false.")
        headless = False
    slow_mo = int(browser_cfg.get("slow_mo_ms", 0))
    default_timeout = int(browser_cfg.get("default_timeout_ms", 120_000))

    saved_pdfs = 0
    queued_groups = 0
    try:
        with sync_playwright() as p:
            browser, context, page, persistent = _open_fedex_browser(
                p, cfg, headless=headless, slow_mo=slow_mo
            )
            page.set_default_timeout(default_timeout)
            try:
                _login_if_needed(
                    page,
                    cfg,
                    creds,
                    manual_login=manual_login,
                    skip_auto_login=skip_auto_login,
                )

                ready_count = 0
                if not skip_upload:
                    _upload_lowes_csv(page, cfg, upload_csv)
                    ready_count = _wait_for_batch_ready(page, cfg, csv_basename)
                    mark_file_used(csv_basename, note="uploaded to FedEx batch")
                else:
                    _log("skip_upload: opening existing batch from uploads table…")
                    _ensure_batch_uploads_list(page, cfg)
                    ready_count = _wait_for_batch_ready(page, cfg, csv_basename)

                _open_batch_shipments(
                    page, cfg, csv_basename, expected_ready=ready_count
                )
                saved_pdfs, queued_groups = _process_vendor_groups(
                    page,
                    context,
                    cfg,
                    order_date=d,
                    expected_row_count=ready_count,
                )
                _log(f"Saved {saved_pdfs} vendor label PDF(s) to share.")
                if warehouse_watcher is None and queued_groups:
                    _log(f"Printed {queued_groups} warehouse vendor group(s) on Zebra.")

                if ready_count > 0 and saved_pdfs == 0 and queued_groups == 0:
                    raise FedexBatchError(
                        f"Batch had {ready_count} shipment(s) ready to finalize but no labels were "
                        "saved or printed. Skipping shipment report export."
                    )

                _export_shipment_report_for_tracking(page, cfg)

                _save_fedex_session(context, uses_persistent_profile=persistent)
            finally:
                context.close()
                if browser is not None:
                    browser.close()
    finally:
        if warehouse_watcher is not None:
            watcher_printed = warehouse_watcher.stop_and_drain(timeout_s=drain_timeout_s())
            if queued_groups:
                _log(
                    f"Queued {queued_groups} warehouse vendor group(s); "
                    f"watcher printed {watcher_printed} label PDF(s) on Zebra."
                )

    return 0
