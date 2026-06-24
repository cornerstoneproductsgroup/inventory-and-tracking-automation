"""Amazon Seller Central: Payments → Reports Repository → Deferred Transaction CSV download."""

from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path
from typing import Any, Callable

from playwright.sync_api import Frame, Page, TimeoutError as PlaywrightTimeout, sync_playwright

from amazon_seller_config import (
    STORAGE_STATE,
    amazon_input_path,
    auto_postprocess_after_download,
    chrome_cdp_url,
    chrome_channel,
    chrome_user_data_dir,
    download_timeout_ms,
    headless as default_headless,
    report_ready_max_attempts,
    report_ready_poll_interval_s,
    request_report_settle_s,
    resolve_input_dir,
    uses_chrome_session,
)
from amazon_seller_credentials import AmazonSellerCredentials, load_amazon_seller_credentials


def _log(msg: str) -> None:
    print(f"[amazon-seller] {msg}", flush=True)


class AmazonSellerDownloadError(Exception):
    pass


def _load_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _sel(cfg: dict[str, Any], key: str, default: str = "") -> str:
    return (cfg.get("selectors", {}).get(key) or default).strip()


def _split_selectors(raw: str) -> list[str]:
    return [s.strip() for s in raw.split(",") if s.strip()]


def _click_first(page: Page, selectors: str, *, timeout_ms: int = 12_000) -> bool:
    for sel in _split_selectors(selectors):
        try:
            loc = page.locator(sel).first
            if page.locator(sel).count() == 0:
                continue
            loc.wait_for(state="visible", timeout=timeout_ms)
            loc.click(timeout=timeout_ms)
            return True
        except Exception:
            continue
    return False


def _login_scopes(page: Page) -> list[Page | Frame]:
    scopes: list[Page | Frame] = [page]
    for frame in page.frames:
        if frame != page.main_frame:
            scopes.append(frame)
    return scopes


def _selector_list(cfg: dict[str, Any], key: str, default: str) -> list[str]:
    raw = _sel(cfg, key, default)
    return _split_selectors(raw) if raw else []


def _is_reports_page(page: Page) -> bool:
    url = page.url.lower()
    return "reports-repository" in url or "reports_repository" in url


def _email_field_selectors(cfg: dict[str, Any]) -> list[str]:
    return _selector_list(
        cfg,
        "username_input",
        "#ap_email, input#ap_email[name='email'], input[type='email'][name='email']",
    )


def _password_field_selectors(cfg: dict[str, Any]) -> list[str]:
    return _selector_list(
        cfg,
        "password_input",
        "#ap_password, input#ap_password[name='password'], input[type='password'][name='password']",
    )


def _find_login_field(
    page: Page,
    selectors: list[str],
    *,
    require_visible: bool = True,
) -> tuple[Any, Page | Frame] | None:
    """Find #ap_email / #ap_password on main page, iframes, or auth frame_locator."""
    iframe_sels = [
        "iframe#auth-coverage-v2-iframe",
        "iframe[name='authentication']",
        "iframe[src*='signin']",
        "iframe[src*='ap/signin']",
        "iframe",
    ]
    state = "visible" if require_visible else "attached"
    for iframe_sel in iframe_sels:
        try:
            fl = page.frame_locator(iframe_sel)
            for sel in selectors:
                try:
                    loc = fl.locator(sel).first
                    loc.wait_for(state=state, timeout=1500)
                    return loc, page
                except Exception:
                    continue
        except Exception:
            continue

    for scope in _login_scopes(page):
        for sel in selectors:
            try:
                root = scope.locator(sel)
                if root.count() == 0:
                    continue
                field = root.first
                field.wait_for(state=state, timeout=1500)
                return field, scope
            except Exception:
                continue
    return None


def _wait_for_email_field(page: Page, cfg: dict[str, Any], *, timeout_ms: int = 90_000):
    selectors = _email_field_selectors(cfg)
    deadline = time.monotonic() + timeout_ms / 1000.0
    last_url = ""
    while time.monotonic() < deadline:
        if page.url != last_url:
            last_url = page.url
            _log(f"Waiting for login email field… ({last_url})")
        for require_visible in (True, False):
            hit = _find_login_field(page, selectors, require_visible=require_visible)
            if hit:
                _log("Found #ap_email — filling username.")
                return hit
        try:
            by_label = page.get_by_label("Email", exact=False).first
            by_label.wait_for(state="visible", timeout=800)
            _log("Found email field by label.")
            return by_label, page
        except Exception:
            pass
        page.wait_for_timeout(500)
    raise AmazonSellerDownloadError(
        f"Amazon login email field (#ap_email) not found (last URL: {page.url})."
    )


def _find_password_field(page: Page, cfg: dict[str, Any]) -> tuple[Any, Page | Frame] | None:
    selectors = _password_field_selectors(cfg)
    for require_visible in (True, False):
        hit = _find_login_field(page, selectors, require_visible=require_visible)
        if hit:
            return hit
    try:
        by_label = page.get_by_label("Password", exact=False).first
        by_label.wait_for(state="visible", timeout=1500)
        return by_label, page
    except Exception:
        return None


def _is_sign_in_page(page: Page, cfg: dict[str, Any]) -> bool:
    if _find_login_field(page, _email_field_selectors(cfg), require_visible=False):
        return True
    url = page.url.lower()
    return "signin" in url or "amazon.com/ap/" in url


def _fill_login_field(field, value: str, *, label: str) -> None:
    page = getattr(field, "page", None)
    want = value.strip()
    try:
        field.scroll_into_view_if_needed(timeout=10_000)
    except Exception:
        pass
    try:
        field.click(timeout=15_000, force=True)
    except Exception:
        field.focus(timeout=10_000)
    if page is not None:
        page.wait_for_timeout(200)
    try:
        field.fill("", timeout=10_000)
        field.press_sequentially(want, delay=40)
    except Exception:
        field.fill(want, timeout=10_000, force=True)
    if page is not None:
        page.wait_for_timeout(300)
    try:
        got = (field.input_value() or "").strip()
    except Exception:
        got = ""
    if got != want:
        _log(f"WARN: {label} value {got!r} != expected; using JS input events.")
        field.evaluate(
            """(el, v) => {
            el.focus();
            el.value = v;
            el.dispatchEvent(new InputEvent('input', { bubbles: true, data: v, inputType: 'insertText' }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }""",
            want,
        )
        if page is not None:
            page.wait_for_timeout(300)
    _log(f"Filled {label} ({len(want)} chars).")


def _click_in_scopes(page: Page, selectors: str, *, label: str, timeout_ms: int = 8000) -> bool:
    for scope in _login_scopes(page):
        for sel in _split_selectors(selectors):
            try:
                root = scope.locator(sel)
                if root.count() == 0:
                    continue
                btn = root.first
                btn.wait_for(state="visible", timeout=timeout_ms)
                _log(f"Clicking {label} ({sel!r}).")
                try:
                    with page.expect_navigation(timeout=60_000, wait_until="domcontentloaded"):
                        btn.click(timeout=15_000)
                except PlaywrightTimeout:
                    btn.click(timeout=15_000)
                if page is not None:
                    page.wait_for_timeout(800)
                return True
            except Exception:
                continue
    for sel in _split_selectors(selectors):
        if _click_first(page, sel, timeout_ms=timeout_ms):
            _log(f"Clicking {label} ({sel!r}) on main page.")
            return True
    return False


def _click_login_continue(page: Page, cfg: dict[str, Any]) -> bool:
    continue_sel = _sel(cfg, "login_continue", "input#continue, button#continue, button:has-text('Continue')")
    return _click_in_scopes(page, continue_sel, label="Continue")


def _click_login_submit(page: Page, cfg: dict[str, Any]) -> None:
    submit_sel = _sel(cfg, "login_submit", "input#signInSubmit, button#signInSubmit, button[type='submit']")
    if _click_in_scopes(page, submit_sel, label="Sign in", timeout_ms=5000):
        return
    page.keyboard.press("Enter")


def _perform_amazon_login(page: Page, cfg: dict[str, Any], creds: AmazonSellerCredentials) -> None:
    if not creds.email or not creds.password:
        raise AmazonSellerDownloadError("Amazon credentials are empty — check AMAZON_SELLER_EMAIL/PASSWORD in .env")

    email, _scope = _wait_for_email_field(page, cfg)
    _log(f"Entering Amazon email for {creds.email!r}")
    _fill_login_field(email, creds.email, label="email")
    page.wait_for_timeout(500)

    pw_hit = _find_password_field(page, cfg)
    if pw_hit is None:
        if not _click_login_continue(page, cfg):
            _log("No Continue button — checking for password on same page.")
        page.wait_for_timeout(1200)

    pw_hit = _find_password_field(page, cfg)
    if pw_hit is None:
        raise AmazonSellerDownloadError(
            f"Amazon password field (#ap_password) not found after Continue (URL: {page.url})."
        )
    pw, _pw_scope = pw_hit
    _fill_login_field(pw, creds.password, label="password")
    page.wait_for_timeout(400)
    _click_login_submit(page, cfg)
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(2500)


def _has_reports_access(page: Page, cfg: dict[str, Any]) -> bool:
    return _reports_ui_ready(page, cfg)


def _wait_for_logged_in(page: Page, cfg: dict[str, Any], *, timeout_ms: int = 120_000) -> None:
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        if _has_reports_access(page, cfg):
            _log("Logged in — Reports Repository form is available.")
            return
        if _is_reports_page(page) and not _is_sign_in_page(page, cfg):
            _log(f"Logged in — on reports URL ({page.url}).")
            return
        page.wait_for_timeout(500)
    raise AmazonSellerDownloadError(
        f"Amazon login did not complete within {timeout_ms / 1000:.0f}s (still at {page.url})."
    )


def _open_sign_in_page(page: Page, cfg: dict[str, Any]) -> None:
    login_url = (cfg.get("amazon", {}).get("login_url") or "").strip()
    reports_url = (cfg.get("amazon", {}).get("reports_url") or "").strip()
    if not login_url:
        login_url = reports_url or "https://sellercentral.amazon.com/ap/signin"
    _log(f"Opening Amazon sign-in: {login_url}")
    page.goto(login_url, wait_until="domcontentloaded", timeout=120_000)
    page.wait_for_timeout(1200)


def _has_seller_central_access(page: Page, cfg: dict[str, Any]) -> bool:
    if _reports_ui_ready(page, cfg):
        return True
    if _is_sign_in_page(page, cfg):
        return False
    url = page.url.lower()
    if "sellercentral.amazon.com" not in url:
        return False
    menu = _sel(
        cfg,
        "hamburger_menu",
        "button[aria-label*='menu' i], header button:has(svg)",
    )
    if page.locator(menu).count() > 0:
        return True
    if page.locator("text=Seller Central").count() > 0:
        return True
    return False


def _open_seller_central_home(page: Page, cfg: dict[str, Any]) -> None:
    home_url = (cfg.get("amazon", {}).get("home_url") or "").strip()
    if not home_url:
        home_url = "https://sellercentral.amazon.com/home"
    _log(f"Opening Seller Central home: {home_url}")
    page.goto(home_url, wait_until="domcontentloaded", timeout=120_000)
    page.wait_for_timeout(2000)


def _ensure_authenticated(page: Page, cfg: dict[str, Any], creds: AmazonSellerCredentials | None) -> None:
    _open_seller_central_home(page, cfg)

    if _has_seller_central_access(page, cfg):
        _log("Already signed in to Seller Central (Chrome session).")
        return

    if _is_sign_in_page(page, cfg):
        if creds is None:
            raise AmazonSellerDownloadError(
                "Amazon sign-in page is showing but no credentials are configured. "
                "Log in once in the Chrome profile (AMAZON_CHROME_USER_DATA_DIR) or set "
                "AMAZON_SELLER_EMAIL / AMAZON_SELLER_PASSWORD in invoice report/.env."
            )
        _log("Sign-in page — logging in with credentials from .env.")
        _perform_amazon_login(page, cfg, creds)
        _wait_for_logged_in(page, cfg)
        return

    reports_url = (cfg.get("amazon", {}).get("reports_url") or "").strip()
    if reports_url:
        _log(f"Trying reports URL: {reports_url}")
        page.goto(reports_url, wait_until="domcontentloaded", timeout=120_000)
        page.wait_for_timeout(2000)
        if _has_seller_central_access(page, cfg) or _is_reports_page(page):
            return

    if _is_sign_in_page(page, cfg):
        if creds is None:
            raise AmazonSellerDownloadError("Amazon sign-in required — configure credentials or Chrome profile.")
        _perform_amazon_login(page, cfg, creds)
        _wait_for_logged_in(page, cfg)
        return

    if not _has_seller_central_access(page, cfg):
        raise AmazonSellerDownloadError(
            f"Could not confirm Seller Central login (URL: {page.url})."
        )


def _login_if_needed(page: Page, cfg: dict[str, Any], creds: AmazonSellerCredentials | None) -> None:
    if uses_chrome_session():
        _ensure_authenticated(page, cfg, creds)
        return

    if creds is None:
        raise AmazonSellerDownloadError("Amazon credentials are required when not using Chrome session reuse.")

    reports_url = (cfg.get("amazon", {}).get("reports_url") or "").strip()
    _log(f"Opening reports page: {reports_url}")
    page.goto(reports_url, wait_until="domcontentloaded", timeout=120_000)
    page.wait_for_timeout(2000)

    if _has_reports_access(page, cfg):
        _log("Already logged in — Reports Repository form is ready.")
        return

    if _is_sign_in_page(page, cfg):
        _log("Sign-in page open — logging in on current page.")
        _perform_amazon_login(page, cfg, creds)
        _wait_for_logged_in(page, cfg)
        if not _has_reports_access(page, cfg) and reports_url:
            page.goto(reports_url, wait_until="domcontentloaded", timeout=120_000)
            page.wait_for_timeout(2000)
        return

    _open_sign_in_page(page, cfg)
    _perform_amazon_login(page, cfg, creds)
    _wait_for_logged_in(page, cfg)

    if reports_url and not _has_reports_access(page, cfg):
        page.goto(reports_url, wait_until="domcontentloaded", timeout=120_000)
        page.wait_for_timeout(2000)

    if not _has_reports_access(page, cfg) and not _is_reports_page(page):
        raise AmazonSellerDownloadError(
            f"Could not reach Reports Repository after login (URL: {page.url})."
        )


def _reports_ui_ready(page: Page, cfg: dict[str, Any]) -> bool:
    """True when the Deferred Transaction / Request Report form is on screen."""
    for sel in _selector_list(
        cfg,
        "request_report_button",
        "button:has-text('Request Report'), text=Request Report",
    ):
        if page.locator(sel).count() > 0:
            return True
    for sel in _selector_list(
        cfg,
        "report_type_dropdown",
        ".select-header, [part='dropdown-header']",
    ):
        if page.locator(sel).count() > 0:
            return True
    if page.locator("text=Payments Reports").count() > 0:
        return True
    return False


def _wait_for_reports_repository_ui(page: Page, cfg: dict[str, Any], *, timeout_ms: int = 90_000) -> None:
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        if _reports_ui_ready(page, cfg):
            _log("Reports Repository form is ready.")
            return
        page.wait_for_timeout(500)
    raise AmazonSellerDownloadError(
        f"Reports Repository page loaded but report form did not appear (URL: {page.url})."
    )


def _open_reports_repository(page: Page, cfg: dict[str, Any]) -> None:
    reports_url = (cfg.get("amazon", {}).get("reports_url") or "").strip()

    if _is_reports_page(page):
        _log("Already on Reports Repository — skipping menu navigation.")
        _wait_for_reports_repository_ui(page, cfg)
        return

    if page.locator("text=Reports Repository").count() > 0:
        tab = _sel(cfg, "reports_repository_tab", "text=Reports Repository")
        if _click_first(page, tab, timeout_ms=5000):
            page.wait_for_timeout(800)
            _wait_for_reports_repository_ui(page, cfg)
            return
        _log("Reports Repository tab visible — waiting for form.")
        _wait_for_reports_repository_ui(page, cfg)
        return

    if reports_url:
        _log(f"Navigating to Reports Repository: {reports_url}")
        page.goto(reports_url, wait_until="domcontentloaded", timeout=120_000)
        page.wait_for_timeout(1500)
        if _is_reports_page(page):
            _wait_for_reports_repository_ui(page, cfg)
            return

    menu = _sel(
        cfg,
        "hamburger_menu",
        "button[aria-label*='menu' i], header button:has(svg)",
    )
    if not _click_first(page, menu, timeout_ms=15_000):
        raise AmazonSellerDownloadError("Could not open Seller Central navigation menu (hamburger).")

    page.wait_for_timeout(700)
    payments = _sel(
        cfg,
        "payments_menu_item",
        ".menu__button-item-link:has(.menu__button-item-label:has-text('Payments')), "
        ".menu__button-item-label:has-text('Payments'), text=Payments",
    )
    if not _click_first(page, payments, timeout_ms=15_000):
        raise AmazonSellerDownloadError('Could not click "Payments" in the navigation menu.')

    page.wait_for_timeout(900)

    tab = _sel(
        cfg,
        "reports_repository_tab",
        "span[slot='label']:has-text('Reports Repository'), text=Reports Repository",
    )
    if not _click_first(page, tab, timeout_ms=20_000):
        raise AmazonSellerDownloadError('Could not open "Reports Repository" tab.')
    _wait_for_reports_repository_ui(page, cfg)


def _select_deferred_transaction(page: Page, cfg: dict[str, Any]) -> None:
    report_type = (cfg.get("amazon", {}).get("report_type") or "Deferred Transaction").strip()
    dropdown = _sel(cfg, "report_type_dropdown", ".select-header, [part='dropdown-header']")
    option = _sel(
        cfg,
        "report_type_option",
        f"text={report_type}, [role='option']:has-text('{report_type}')",
    )

    header_root = page.locator(dropdown)
    try:
        if header_root.count() > 0:
            text = (header_root.first.inner_text() or "").strip()
            if report_type.lower() in text.lower():
                _log(f"Report type already set to {report_type!r}.")
                return
    except Exception:
        pass

    if not _click_first(page, dropdown, timeout_ms=12_000):
        raise AmazonSellerDownloadError("Could not open Report Type dropdown.")
    page.wait_for_timeout(500)
    if not _click_first(page, option, timeout_ms=12_000):
        raise AmazonSellerDownloadError(f'Could not select report type {report_type!r}.')


def _request_report(page: Page, cfg: dict[str, Any]) -> None:
    btn = _sel(cfg, "request_report_button", "button:has-text('Request Report'), text=Request Report")
    if not _click_first(page, btn, timeout_ms=15_000):
        raise AmazonSellerDownloadError('Could not click "Request Report".')
    settle_s = request_report_settle_s()
    _log(f"Report requested — waiting {settle_s:.0f}s before Refresh…")
    page.wait_for_timeout(int(settle_s * 1000))


def _top_report_row(page: Page, cfg: dict[str, Any]):
    row_sel = _sel(cfg, "payments_report_row", "table tbody tr")
    rows = page.locator(row_sel)
    if rows.count() == 0:
        return None
    return rows.first


def _row_status_text(row) -> str:
    try:
        return (row.inner_text() or "").strip()
    except Exception:
        return ""


def _click_top_row_action(row, selectors: str, *, label: str, timeout_ms: int = 12_000) -> bool:
    for sel in _split_selectors(selectors):
        try:
            btn = row.locator(sel).first
            btn.wait_for(state="visible", timeout=timeout_ms)
            btn.click(timeout=timeout_ms)
            _log(f"Clicked {label} on top report row ({sel!r}).")
            return True
        except Exception:
            continue
    return False


def _wait_and_download_csv(page: Page, cfg: dict[str, Any], dest: Path) -> None:
    ready_sel = _sel(cfg, "status_ready", "text=Ready")
    progress_sel = _sel(cfg, "status_in_progress", "text=In Progress")
    download_sel = _sel(cfg, "download_csv", "text=Download CSV, button:has-text('Download CSV')")
    refresh_sel = _sel(cfg, "refresh_button", "button:has-text('Refresh'), text=Refresh")
    max_attempts = report_ready_max_attempts()
    interval_s = report_ready_poll_interval_s()
    timeout_ms = download_timeout_ms()

    for attempt in range(1, max_attempts + 1):
        row = _top_report_row(page, cfg)
        if row is None:
            _log(f"Attempt {attempt}/{max_attempts}: no report rows yet — refreshing page…")
        else:
            body = _row_status_text(row)
            is_ready = row.locator(ready_sel).count() > 0 or "Ready" in body
            if is_ready:
                _log(f"Attempt {attempt}/{max_attempts}: top report is Ready — downloading CSV.")
                try:
                    with page.expect_download(timeout=timeout_ms) as dl_info:
                        if not _click_top_row_action(row, download_sel, label="Download CSV"):
                            raise AmazonSellerDownloadError(
                                'Top report row is Ready but "Download CSV" was not found.'
                            )
                    download = dl_info.value
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if dest.exists():
                        dest.unlink()
                    download.save_as(str(dest))
                    if not dest.is_file() or dest.stat().st_size < 50:
                        raise AmazonSellerDownloadError(f"Download failed or file empty: {dest}")
                    _log(f"Saved {dest.name} ({dest.stat().st_size:,} bytes) → {dest}")
                    return
                except PlaywrightTimeout as exc:
                    raise AmazonSellerDownloadError(
                        "Download CSV click on the top row did not start a file download."
                    ) from exc

            if row.locator(progress_sel).count() > 0 or "In Progress" in body:
                _log(f"Attempt {attempt}/{max_attempts}: top row still In Progress…")
            else:
                _log(f"Attempt {attempt}/{max_attempts}: top row status={body[:80]!r}")

            if _click_top_row_action(row, refresh_sel, label="Refresh", timeout_ms=5000):
                page.wait_for_timeout(1200)
                continue

        page.wait_for_timeout(int(interval_s * 1000))
        if _click_first(page, refresh_sel, timeout_ms=4000):
            page.wait_for_timeout(1200)
        else:
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(1500)

    raise AmazonSellerDownloadError(
        f"Report did not reach Ready status after {max_attempts} refresh attempts."
    )


def _maybe_postprocess(dest: Path) -> None:
    if not auto_postprocess_after_download():
        _log("Skipping post-process (AMAZON_DOWNLOAD_AUTO_POSTPROCESS=false). Watcher can pick up the file.")
        return
    try:
        from amazon_invoice_postprocess import process_amazon_export

        _log("Running Amazon format + print pipeline on downloaded file…")
        process_amazon_export(dest)
    except Exception as exc:
        _log(f"WARN: post-process failed (CSV saved): {exc}")


def _launch_page(p, cfg: dict[str, Any]) -> tuple[Page, Callable[[], None]]:
    """Return (page, cleanup_fn). cleanup is no-op for CDP attach."""
    from amazon_seller_config import (
        amazon_browser_cdp_port,
        use_system_chrome_profile,
    )

    browser_cfg = cfg.get("browser", {})
    use_headless = bool(browser_cfg.get("headless", default_headless()))
    slow_mo = int(browser_cfg.get("slow_mo_ms", 0))
    default_timeout = int(browser_cfg.get("default_timeout_ms", 120_000))
    home_url = (cfg.get("amazon", {}).get("home_url") or "").strip() or "https://sellercentral.amazon.com/home"

    cdp = chrome_cdp_url()
    if cdp:
        _log(f"Connecting to Chrome over CDP: {cdp}")
        browser = p.chromium.connect_over_cdp(cdp)
        context = browser.contexts[0] if browser.contexts else browser.new_context(accept_downloads=True)
        from amazon_chrome_launch import goto_seller_central_home, pick_seller_central_page

        page = pick_seller_central_page(context, home_url=home_url)
        page.set_default_timeout(default_timeout)
        goto_seller_central_home(page, home_url)
        return page, lambda: None

    if use_system_chrome_profile():
        from amazon_chrome_launch import connect_system_chrome

        port = amazon_browser_cdp_port()
        _log(f"Using installed Chrome profile (CDP port {port}) — same login as your daily browser.")
        _browser, page = connect_system_chrome(
            p,
            home_url=home_url,
            port=port,
            log_dir=Path(__file__).resolve().parent,
        )
        page.set_default_timeout(default_timeout)
        return page, lambda: None

    profile_dir = chrome_user_data_dir()
    if profile_dir:
        profile_dir.mkdir(parents=True, exist_ok=True)
        _log(f"Launching Chrome with profile: {profile_dir}")
        context = p.chromium.launch_persistent_context(
            str(profile_dir),
            channel=chrome_channel(),
            headless=use_headless,
            slow_mo=slow_mo,
            accept_downloads=True,
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(default_timeout)
        return page, context.close

    browser = p.chromium.launch(headless=use_headless, slow_mo=slow_mo)
    storage = STORAGE_STATE if STORAGE_STATE.is_file() else None
    context = browser.new_context(
        accept_downloads=True,
        storage_state=str(storage) if storage else None,
    )
    page = context.new_page()
    page.set_default_timeout(default_timeout)

    def _close_ephemeral() -> None:
        context.close()
        browser.close()

    return page, _close_ephemeral


def run_amazon_seller_download(
    *,
    config_path: Path,
    run_date: date | None = None,
    dest_path: Path | None = None,
    skip_postprocess: bool = False,
) -> Path:
    cfg = _load_config(config_path)
    creds = load_amazon_seller_credentials(required=not uses_chrome_session())
    dest = (dest_path or amazon_input_path(run_date)).resolve()
    input_dir = resolve_input_dir()
    if not input_dir.is_dir():
        raise AmazonSellerDownloadError(f"Amazon Input folder not accessible: {input_dir}")

    _log(f"Target file: {dest.name}")
    _log(f"Input folder: {input_dir}")
    if uses_chrome_session():
        _log("Chrome session reuse enabled — sign-in may be skipped if already logged in.")

    with sync_playwright() as p:
        page, cleanup = _launch_page(p, cfg)
        try:
            _login_if_needed(page, cfg, creds)
            _open_reports_repository(page, cfg)
            _select_deferred_transaction(page, cfg)
            _request_report(page, cfg)
            _wait_and_download_csv(page, cfg, dest)
            if not uses_chrome_session() and chrome_user_data_dir() is None:
                try:
                    page.context.storage_state(path=str(STORAGE_STATE))
                    _log(f"Session saved to {STORAGE_STATE}")
                except Exception as exc:
                    _log(f"WARN: could not save session state: {exc}")
        finally:
            cleanup()

    if not skip_postprocess:
        _maybe_postprocess(dest)
    return dest
