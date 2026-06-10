"""UPS.com batch file shipping — Home Depot lane (up to 250 shipments)."""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    sync_playwright,
)

from automation.ups_batch_config import (
    DEFAULT_BATCH_LANDING_URL,
    DEFAULT_BROWSER_PROFILE_DIR,
    DEFAULT_HOME_URL,
    STORAGE_STATE,
    allow_unsafe_cdp,
    browser_cdp_port,
    browser_display_name,
    browser_profile_directory,
    chrome_cdp_env_disabled,
    dedicated_ups_profile_dir,
    depot_labels_pdf_path,
    label_save_timeout_s,
    resolve_browser_user_data_dir,
    system_browser_user_data_dir,
    system_chrome_user_data_dir,
    ups_browser_channel,
    ups_browser_mode,
    use_chrome_cdp_launch,
    use_system_chrome_profile,
)
from automation.ups_credentials import UpsCredentials, load_ups_credentials
from automation.ups_chrome_launch import (
    close_browser_processes,
    close_chrome_processes,
    connect_playwright_cdp,
    launch_browser_for_cdp,
    pick_ups_page_from_context,
    wait_for_cdp_endpoint,
)
from automation.ups_popup_dismiss import clear_blocking_overlays, dismiss_ups_startup_popups
from automation.ups_depot_csv import DepotCsvSkip, resolve_upload_csv
from automation.windows_open_file import fill_open_file_dialog
from automation.windows_save_as import fill_save_as_dialog, wait_for_save_as_dialog


def _log(msg: str) -> None:
    print(f"[ups] {msg}", flush=True)


class UpsBatchError(Exception):
    pass


_STEALTH_INIT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
"""


@dataclass(frozen=True)
class UpsBatchResult:
    csv_path: Path
    labels_path: Path | None
    shipment_count: int | None


def _load_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _sel(cfg: dict[str, Any], key: str, default: str = "") -> str:
    return (cfg.get("selectors", {}).get(key) or default).strip()


def _timing_ms(cfg: dict[str, Any], key: str, env_key: str, default: int) -> int:
    timing = cfg.get("timing") or {}
    raw = str(timing.get(key) or os.environ.get(env_key) or default).strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return default


def _env_bool(name: str, *, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    if raw in ("0", "false", "no", "off"):
        return False
    return raw in ("1", "true", "yes", "on")


def _resolve_channel(browser_cfg: dict[str, Any]) -> str | None:
    channel = ups_browser_channel(browser_cfg)
    if channel == "msedge":
        return "msedge"
    return "chrome"


def _using_system_chrome_profile(
    user_data_dir: Path | None,
    browser_cfg: dict[str, Any] | None = None,
) -> bool:
    if user_data_dir is None or not use_system_chrome_profile(browser_cfg):
        return False
    system_root = system_browser_user_data_dir(browser_cfg)
    if system_root is None:
        return False
    try:
        return user_data_dir.resolve() == system_root.resolve()
    except OSError:
        return str(user_data_dir).lower() == str(system_root).lower()


def _launch_args(browser_cfg: dict[str, Any], *, user_data_dir: Path | None) -> list[str]:
    extra = browser_cfg.get("args") or []
    if isinstance(extra, str):
        extra = [extra]
    base = ["--disable-blink-features=AutomationControlled"]
    if _using_system_chrome_profile(user_data_dir, browser_cfg):
        profile = browser_profile_directory()
        base.extend(
            [
                f"--profile-directory={profile}",
                "--disable-session-crashed-bubble",
                "--disable-restore-session-state",
                "--no-first-run",
                "--no-default-browser-check",
            ]
        )
    out: list[str] = []
    for arg in [*base, *extra]:
        text = str(arg).strip()
        if text and text not in out:
            out.append(text)
    return out


def _ups_home_url(cfg: dict[str, Any]) -> str:
    ups = cfg.get("ups") or {}
    return str(ups.get("home_url") or DEFAULT_HOME_URL).strip()


def _pick_ups_page(context: BrowserContext, *, home_url: str) -> Page:
    for pg in context.pages:
        try:
            if "ups.com" in (pg.url or "").lower():
                pg.bring_to_front()
                return pg
        except Exception:
            continue
    for pg in context.pages:
        try:
            url = (pg.url or "").strip()
            if url and url not in ("about:blank", "chrome://newtab/", ""):
                pg.bring_to_front()
                return pg
        except Exception:
            continue
    if context.pages:
        pg = context.pages[0]
        pg.bring_to_front()
        return pg
    return context.new_page()


def _ensure_ups_home_page(page: Page, cfg: dict[str, Any]) -> None:
    home_url = _ups_home_url(cfg)
    current = (page.url or "").strip().lower()
    if "ups.com" not in current:
        _log(f"Navigating to {home_url} (current tab: {page.url!r})")
        last_err: Exception | None = None
        for attempt in range(1, 4):
            try:
                page.goto(home_url, wait_until="domcontentloaded", timeout=120_000)
                if "ups.com" in (page.url or "").lower():
                    break
            except Exception as exc:
                last_err = exc
                _log(f"WARN: UPS navigation attempt {attempt}/3: {exc}")
                if attempt < 3:
                    try:
                        page = page.context.new_page()
                        page.bring_to_front()
                    except Exception:
                        pass
        else:
            raise UpsBatchError(
                f"Could not open UPS home page (stuck on {page.url!r}). {last_err}"
            )
    else:
        _log(f"On UPS: {page.url}")
    page.wait_for_timeout(_timing_ms(cfg, "micro_pause_ms", "UPS_MICRO_PAUSE_MS", 400))
    clear_blocking_overlays(page, cfg, log=_log)


def _attach_playwright_to_cdp(
    p: Playwright,
    cfg: dict[str, Any],
    *,
    port: int,
) -> tuple[Browser, BrowserContext, Page]:
    home_url = _ups_home_url(cfg)
    last_err: Exception | None = None
    for _ in range(20):
        try:
            browser = connect_playwright_cdp(p, port)
            if not browser.contexts:
                time.sleep(0.5)
                continue
            context = browser.contexts[0]
            page = pick_ups_page_from_context(context, home_url=home_url)
            _ensure_ups_home_page(page, cfg)
            _log(f"Attached to Chrome on port {port} — {page.url}")
            return browser, context, page
        except Exception as exc:
            last_err = exc
            time.sleep(1.0)

    raise UpsBatchError(
        f"Chrome debug port {port} was ready but Playwright could not attach. {last_err}"
    )


def _open_system_chrome_via_cdp(
    p: Playwright,
    cfg: dict[str, Any],
) -> tuple[Browser, BrowserContext, Page]:
    browser_cfg = cfg.get("browser", {})
    home_url = _ups_home_url(cfg)
    profile = browser_profile_directory()
    channel = ups_browser_channel(browser_cfg)
    name = browser_display_name(channel)
    log_dir = Path(__file__).resolve().parent.parent / "logs"
    _log(
        f"Launching real {name} with profile {profile!r} and opening UPS "
        "(Playwright will attach — it does not launch the browser)."
    )

    try:
        port = launch_browser_for_cdp(
            home_url=home_url,
            log_dir=log_dir,
            browser_cfg=browser_cfg,
        )
    except RuntimeError as exc:
        raise UpsBatchError(str(exc)) from exc

    return _attach_playwright_to_cdp(p, cfg, port=port)


def _open_manual_cdp_attach(
    p: Playwright,
    cfg: dict[str, Any],
) -> tuple[Browser, BrowserContext, Page]:
    browser_cfg = cfg.get("browser", {})
    port = browser_cdp_port(browser_cfg)
    channel = ups_browser_channel(browser_cfg)
    debug_bat = (
        "Run UPS Edge Debug.bat" if channel == "msedge" else "Run UPS Chrome Debug.bat"
    )
    _log(
        f"Manual attach mode — start {browser_display_name(channel)} with "
        f"'{debug_bat}', then press Enter here when UPS is open (port {port})…"
    )
    try:
        input()
    except EOFError:
        pass

    if not wait_for_cdp_endpoint(port, timeout_s=5.0):
        raise UpsBatchError(
            f"No debug port on {port}. Run '{debug_bat}' first."
        )
    return _attach_playwright_to_cdp(p, cfg, port=port)


def _open_dedicated_profile_browser(
    p: Playwright,
    cfg: dict[str, Any],
    *,
    headless: bool,
    slow_mo: int,
) -> tuple[None, BrowserContext, Page, bool]:
    """
    FedEx-style local profile under Inventory Submissions/ups_browser_profile.
    Run with --setup-login once to save a UPS session here.
    """
    browser_cfg = cfg.get("browser", {})
    profile_dir = dedicated_ups_profile_dir(browser_cfg)
    profile_dir.mkdir(parents=True, exist_ok=True)
    args = _launch_args(browser_cfg, user_data_dir=profile_dir)
    home = _ups_home_url(cfg)
    channel = _resolve_channel(browser_cfg) or "chrome"
    name = browser_display_name(channel)

    _log(f"Using dedicated UPS profile at {profile_dir} ({name})")
    context = p.chromium.launch_persistent_context(
        str(profile_dir),
        channel=channel,
        headless=headless,
        slow_mo=slow_mo,
        args=args,
        ignore_default_args=["--enable-automation", "--no-sandbox"],
        accept_downloads=True,
    )
    context.add_init_script(_STEALTH_INIT)
    page = _pick_ups_page(context, home_url=home)
    page.bring_to_front()
    _ensure_ups_home_page(page, cfg)
    _log(f"{name} ready (dedicated profile) — {page.url}")
    return None, context, page, True


def _open_browser(
    p: Playwright,
    cfg: dict[str, Any],
    *,
    headless: bool,
    slow_mo: int,
) -> tuple[Browser | None, BrowserContext, Page, bool, str]:
    browser_cfg = cfg.get("browser", {})
    user_data_dir = resolve_browser_user_data_dir(browser_cfg)
    args = _launch_args(browser_cfg, user_data_dir=user_data_dir)
    ignore_default_args = ["--enable-automation", "--no-sandbox"]
    channels: list[str | None] = []
    primary = _resolve_channel(browser_cfg)
    if primary:
        channels.append(primary)
    for alt in ("chrome", "msedge"):
        if alt not in channels:
            channels.append(alt)
    channels.append(None)

    mode = ups_browser_mode(browser_cfg)
    if mode in ("cdp", "manual") and not allow_unsafe_cdp():
        _log(
            "UPS_BROWSER_MODE=cdp/manual is disabled (matches Huntress infostealer alerts). "
            "Using isolated browser profile. One-time setup: "
            "python run_ups_online_batch.py --setup-login"
        )
        mode = "dedicated"

    if mode == "manual":
        browser, context, page = _open_manual_cdp_attach(p, cfg)
        return browser, context, page, True, "manual"

    if mode == "dedicated":
        none_ctx, context, page, persistent = _open_dedicated_profile_browser(
            p, cfg, headless=headless, slow_mo=slow_mo
        )
        return none_ctx, context, page, persistent, "dedicated"

    if _using_system_chrome_profile(user_data_dir, browser_cfg) and use_chrome_cdp_launch(
        browser_cfg
    ):
        close_browser_processes(browser_cfg=browser_cfg)
        try:
            browser, context, page = _open_system_chrome_via_cdp(p, cfg)
            return browser, context, page, True, "cdp"
        except UpsBatchError as exc:
            _log(f"WARN: CDP attach failed: {exc}")
            _log(
                "Falling back to dedicated UPS profile "
                f"({dedicated_ups_profile_dir(browser_cfg)}). "
                "You will be prompted to log in if needed."
            )
            close_browser_processes(force=True, browser_cfg=browser_cfg)
            none_ctx, context, page, persistent = _open_dedicated_profile_browser(
                p, cfg, headless=headless, slow_mo=slow_mo
            )
            return none_ctx, context, page, persistent, "dedicated"

    last_err: Exception | None = None
    seen_channels: set[str | None] = set()
    for channel in channels:
        if channel in seen_channels:
            continue
        seen_channels.add(channel)
        label = channel or "playwright chromium"
        try:
            launch_kwargs: dict[str, Any] = {
                "headless": headless,
                "slow_mo": slow_mo,
                "args": args,
                "ignore_default_args": ignore_default_args,
            }
            if channel:
                launch_kwargs["channel"] = channel

            if user_data_dir is not None:
                if not _using_system_chrome_profile(user_data_dir, browser_cfg):
                    user_data_dir.mkdir(parents=True, exist_ok=True)
                home = _ups_home_url(cfg)
                launch_kwargs["args"] = list(args)
                context = p.chromium.launch_persistent_context(
                    str(user_data_dir),
                    accept_downloads=True,
                    **launch_kwargs,
                )
                context.add_init_script(_STEALTH_INIT)
                page = _pick_ups_page(context, home_url=home)
                page.bring_to_front()
                _ensure_ups_home_page(page, cfg)
                _log(f"Browser: {label} (profile {user_data_dir}) — {page.url}")
                return None, context, page, True, "persistent"

            browser = p.chromium.launch(**launch_kwargs)
            storage = STORAGE_STATE if STORAGE_STATE.is_file() else None
            context = browser.new_context(
                accept_downloads=True,
                storage_state=str(storage) if storage else None,
                locale="en-US",
                viewport={"width": 1440, "height": 900},
            )
            context.add_init_script(_STEALTH_INIT)
            page = context.new_page()
            _log(f"Browser: {label} (ephemeral)")
            return browser, context, page, False, "ephemeral"
        except Exception as exc:
            last_err = exc
            _log(f"WARN: launch failed ({label}): {exc}")

    raise UpsBatchError(f"Could not launch browser. Last error: {last_err}")


def _save_session(context: BrowserContext, *, persistent: bool) -> None:
    if persistent:
        _log(f"Session kept in profile ({DEFAULT_BROWSER_PROFILE_DIR}).")
        return
    context.storage_state(path=str(STORAGE_STATE))
    _log(f"Session saved to {STORAGE_STATE}")


def _click_any(page: Page, selectors: str, *, label: str = "", timeout_ms: int = 15_000) -> bool:
    for sel in [s.strip() for s in selectors.split(",") if s.strip()]:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=timeout_ms)
            loc.scroll_into_view_if_needed(timeout=3000)
            loc.click(timeout=timeout_ms)
            if label:
                _log(f"Clicked {label}.")
            return True
        except Exception as exc:
            _log(f"WARN: {label or sel} — {exc}")
    return False


def _fill_field(page: Page, selector: str, value: str, *, label: str) -> None:
    for sel in [s.strip() for s in selector.split(",") if s.strip()]:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=12_000)
            loc.click(timeout=5000)
            loc.fill("")
            loc.fill(value)
            _log(f"Filled {label}: {value!r}")
            return
        except Exception:
            continue
    raise UpsBatchError(f"Could not fill {label}")


def _select_dropdown(page: Page, selector: str, *, value: str | None = None, label_text: str | None = None, field: str) -> None:
    for sel in [s.strip() for s in selector.split(",") if s.strip()]:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=12_000)
            if value:
                loc.select_option(value=value)
            elif label_text:
                loc.select_option(label=label_text)
            _log(f"Selected {field}.")
            return
        except Exception:
            continue
    raise UpsBatchError(f"Could not select {field}")


def _type_login_field(page: Page, selector: str, text: str) -> None:
    loc = page.locator(selector).first
    loc.wait_for(state="visible", timeout=20_000)
    loc.click()
    loc.fill("")
    loc.press_sequentially(text, delay=40)


def _is_ups_logged_in(page: Page, cfg: dict[str, Any]) -> bool:
    """Only True on positive logged-in signals — never guess when UI is blocked."""
    try:
        if page.locator("#username").first.is_visible(timeout=1200):
            return False
    except Exception:
        pass
    for sel in (
        "a:has-text('Log Out')",
        "button:has-text('Log Out')",
        "[data-test-id='user-menu']",
        ".ups-userProfile",
        "a:has-text('My Profile')",
    ):
        try:
            if page.locator(sel).first.is_visible(timeout=1500):
                return True
        except Exception:
            continue
    try:
        login = page.locator(_sel(cfg, "header_login")).first
        if login.is_visible(timeout=2500):
            text = (login.inner_text(timeout=2000) or "").lower()
            if "log in" in text:
                return False
    except Exception:
        pass
    return False


def _wait_for_manual_ups_login(page: Page, cfg: dict[str, Any]) -> None:
    _log(
        "Log into UPS in the Chrome window (dismiss cookie/location popups if shown), "
        "then press Enter here to continue."
    )
    try:
        input("[ups] Press Enter when logged in… ")
    except EOFError:
        pass
    clear_blocking_overlays(page, cfg, log=_log)
    if not _is_ups_logged_in(page, cfg):
        raise UpsBatchError(
            "Still not logged into UPS. Dismiss any popups blocking the page and retry."
        )


def _ups_login(
    page: Page,
    cfg: dict[str, Any],
    creds: UpsCredentials,
    *,
    manual: bool,
    launch_source: str,
) -> None:
    _log("Step 1/6: Open UPS home and clear popups…")
    _ensure_ups_home_page(page, cfg)
    clear_blocking_overlays(page, cfg, log=_log)

    if manual or _env_bool("UPS_MANUAL_LOGIN", default=False):
        _wait_for_manual_ups_login(page, cfg)
        return

    if _is_ups_logged_in(page, cfg):
        _log("Already logged in (Chrome profile session) — skipping login.")
        return

    has_creds = bool((creds.username or "").strip() and (creds.password or "").strip())
    skip_auto = launch_source in ("cdp", "manual") and _env_bool(
        "UPS_SKIP_AUTO_LOGIN",
        default=use_system_chrome_profile(cfg.get("browser")),
    )

    if skip_auto:
        clear_blocking_overlays(page, cfg, log=_log)
        if _is_ups_logged_in(page, cfg):
            _log("Logged in after clearing popups — continuing.")
            return
        raise UpsBatchError(
            "Not logged into UPS (Log In still visible). Popups may be blocking the page. "
            "Dismiss them manually in Chrome, or set UPS_SKIP_AUTO_LOGIN=0 with "
            "UPS_USERNAME/UPS_PASSWORD in .env."
        )

    if not has_creds:
        if launch_source == "dedicated":
            raise UpsBatchError(
                "UPS credentials required for auto-login. Add to Inventory Submissions/.env:\n"
                "  UPS_USERNAME=your-ups-login\n"
                "  UPS_PASSWORD=your-ups-password\n"
                "Or run once with: python run_ups_online_batch.py --setup-login"
            )
        raise UpsBatchError(
            "Missing UPS_USERNAME / UPS_PASSWORD in .env for auto-login."
        )

    _log(f"Step 2/6: Logging into UPS as {creds.username[:3]}…")

    if not _click_any(page, _sel(cfg, "header_login"), label="header Log In"):
        raise UpsBatchError("Could not click header Log In")
    page.wait_for_timeout(600)
    if not _click_any(page, _sel(cfg, "dropdown_login"), label="dropdown Log In"):
        _log("WARN: dropdown Log In not found — may already be on login page.")

    _type_login_field(page, _sel(cfg, "username_input"), creds.username)
    verify_ms = _timing_ms(cfg, "verify_wait_ms", "UPS_VERIFY_WAIT_MS", 8000)
    try:
        page.locator(_sel(cfg, "verify_success")).first.wait_for(
            state="visible", timeout=verify_ms
        )
        _log("Username verification succeeded.")
    except Exception:
        _log("WARN: verification box not detected — continuing after short wait.")
        page.wait_for_timeout(2000)

    if not _click_any(page, _sel(cfg, "login_continue"), label="Continue (username)"):
        raise UpsBatchError("Could not click Continue after username")

    page.wait_for_timeout(800)
    _type_login_field(page, _sel(cfg, "password_input"), creds.password)
    if not _click_any(page, _sel(cfg, "login_continue"), label="Continue (password)"):
        raise UpsBatchError("Could not click Continue after password")

    page.wait_for_timeout(_timing_ms(cfg, "after_login_ms", "UPS_AFTER_LOGIN_MS", 2000))
    _log("Login submitted.")


def _on_create_shipment_page(page: Page, cfg: dict[str, Any]) -> bool:
    browse = _sel(cfg, "browse_file")
    try:
        return page.locator(browse).first.is_visible(timeout=2000)
    except Exception:
        return False


def _navigate_batch_shipping(page: Page, cfg: dict[str, Any]) -> None:
    _log("Step 3/6: Navigate to Batch File Shipping…")
    ups = cfg.get("ups") or {}
    landing = str(ups.get("batch_landing_url") or DEFAULT_BATCH_LANDING_URL).strip()

    if _on_create_shipment_page(page, cfg):
        _log("Already on Create a Shipment page.")
        return

    menu_ok = False
    for attempt in range(1, 4):
        clear_blocking_overlays(page, cfg, log=_log)
        if _click_any(
            page, _sel(cfg, "shipping_tab"), label=f"Shipping tab (try {attempt})", timeout_ms=10_000
        ):
            page.wait_for_timeout(700)
            if _click_any(
                page,
                _sel(cfg, "batch_file_shipping"),
                label="Batch File Shipping",
                timeout_ms=10_000,
            ):
                menu_ok = True
                break
        _log(f"WARN: Shipping menu not ready (attempt {attempt}/3).")
        page.wait_for_timeout(800)

    if not menu_ok:
        _log(f"Opening batch page directly: {landing}")
        page.goto(landing, wait_until="domcontentloaded", timeout=90_000)
        clear_blocking_overlays(page, cfg, log=_log)

    if not _click_any(page, _sel(cfg, "ship_now"), label="Ship Now", timeout_ms=15_000):
        if _on_create_shipment_page(page, cfg):
            _log("Ship Now skipped — upload form already visible.")
        else:
            raise UpsBatchError(
                "Could not reach Batch File Shipping (Ship Now / upload form not found). "
                "Popups may still be blocking the page."
            )
    page.wait_for_timeout(1200)
    _log("Create a Shipment page ready.")


def _upload_csv(page: Page, cfg: dict[str, Any], csv_path: Path) -> None:
    _log(f"Step 4/6: Upload CSV {csv_path.name}…")
    browse = _sel(cfg, "browse_file")
    file_input = _sel(cfg, "file_input")

    uploaded = False
    if file_input:
        try:
            loc = page.locator(file_input).first
            if loc.count() > 0:
                loc.set_input_files(str(csv_path))
                uploaded = True
                _log(f"Uploaded via file input: {csv_path.name}")
        except Exception as exc:
            _log(f"WARN: set_input_files failed: {exc}")

    if not uploaded:
        try:
            with page.expect_file_chooser(timeout=15_000) as fc_info:
                _click_any(page, browse, label="Browse for File", timeout_ms=10_000)
            fc_info.value.set_files(str(csv_path))
            uploaded = True
            _log(f"Uploaded via file chooser: {csv_path.name}")
        except Exception as exc:
            _log(f"WARN: file chooser failed: {exc}")

    if not uploaded:
        _click_any(page, browse, label="Browse for File", timeout_ms=10_000)
        if not fill_open_file_dialog(csv_path, timeout_s=45.0):
            raise UpsBatchError(f"Could not select CSV via Open dialog: {csv_path}")
        uploaded = True

    page.wait_for_timeout(_timing_ms(cfg, "after_upload_ms", "UPS_AFTER_UPLOAD_MS", 3000))


def _fill_ship_from(page: Page, cfg: dict[str, Any]) -> None:
    _log("Step 5/6: Fill Ship From and payment…")
    ship = cfg.get("ship_from") or {}
    _fill_field(page, _sel(cfg, "company_name"), str(ship.get("company") or "HomeDepot.com"), label="Company")
    _fill_field(page, _sel(cfg, "contact_name"), str(ship.get("contact") or "Cornerstone Products Group"), label="Contact")
    _fill_field(page, _sel(cfg, "address_line1"), str(ship.get("address") or "1106 E Turner Rd"), label="Address")
    _fill_field(page, _sel(cfg, "city"), str(ship.get("city") or "Lodi"), label="City")
    _select_dropdown(page, _sel(cfg, "state"), value=str(ship.get("state") or "CA"), field="State")
    _fill_field(page, _sel(cfg, "zip"), str(ship.get("zip") or "95240"), label="ZIP")


def _fill_payment(page: Page, cfg: dict[str, Any]) -> None:
    pay = cfg.get("payment") or {}
    if not _click_any(page, _sel(cfg, "bill_other_account"), label="Bill Other Account"):
        raise UpsBatchError("Could not select Bill Other Account")
    page.wait_for_timeout(500)
    _fill_field(
        page,
        _sel(cfg, "third_party_account"),
        str(pay.get("third_party_account") or "1YA668"),
        label="Third-party account",
    )
    _fill_field(
        page,
        _sel(cfg, "third_party_zip"),
        str(pay.get("third_party_zip") or "30339"),
        label="Third-party ZIP",
    )
    country = str(pay.get("third_party_country") or "United States")
    try:
        _select_dropdown(page, _sel(cfg, "third_party_country"), label_text=country, field="Country")
    except UpsBatchError:
        _select_dropdown(page, _sel(cfg, "third_party_country"), value="252", field="Country")

    acct_label = str(pay.get("billing_account_label") or "186Y47 - Worldship")
    if not _click_any(page, _sel(cfg, "billing_account"), label=f"Billing account ({acct_label})"):
        try:
            page.get_by_label(acct_label, exact=False).click(timeout=8000)
        except Exception as exc:
            raise UpsBatchError(f"Could not select billing account: {exc}") from exc


def _preview_and_process(page: Page, cfg: dict[str, Any], context: BrowserContext) -> Page:
    _log("Step 6/6: Preview Batch and Process All…")
    if not _click_any(page, _sel(cfg, "preview_batch"), label="Preview Batch"):
        raise UpsBatchError("Could not click Preview Batch")
    page.wait_for_timeout(3000)
    _log("Batch Processing page loaded.")

    process_sel = _sel(cfg, "process_all")
    with context.expect_page(timeout=120_000) as page_info:
        if not _click_any(page, process_sel, label="Process All", timeout_ms=30_000):
            raise UpsBatchError("Could not click Process All")
    labels_page = page_info.value
    labels_page.wait_for_load_state("domcontentloaded", timeout=60_000)
    _log("Label print/preview page opened.")
    return labels_page


def _save_labels_pdf(labels_page: Page, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    timeout_s = label_save_timeout_s()

    # CDP printToPDF (works for print-preview tabs)
    try:
        cdp = labels_page.context.new_cdp_session(labels_page)
        result = cdp.send(
            "Page.printToPDF",
            {"printBackground": True, "preferCSSPageSize": True},
        )
        data = base64.b64decode(result.get("data") or "")
        if len(data) > 2000:
            dest.write_bytes(data)
            _log(f"Saved labels via printToPDF: {dest}")
            return
    except Exception as exc:
        _log(f"WARN: printToPDF failed: {exc}")

    # Download event
    try:
        with labels_page.expect_download(timeout=int(timeout_s * 1000)) as dl_info:
            labels_page.keyboard.press("Control+s")
        dl_info.value.save_as(str(dest))
        if dest.is_file() and dest.stat().st_size > 2000:
            _log(f"Saved labels via download: {dest}")
            return
    except Exception as exc:
        _log(f"WARN: download save failed: {exc}")

    # Native Save As
    try:
        labels_page.keyboard.press("Control+s")
        if wait_for_save_as_dialog(timeout_s=min(30.0, timeout_s)):
            if fill_save_as_dialog(dest, timeout_s=timeout_s):
                if dest.is_file():
                    _log(f"Saved labels via Save As dialog: {dest}")
                    return
    except Exception as exc:
        _log(f"WARN: Save As dialog failed: {exc}")

    raise UpsBatchError(f"Could not save label PDF to {dest}")


def run_ups_depot_batch(
    *,
    config_path: Path,
    csv_path: Path | None = None,
    order_date: date | None = None,
    manual_login: bool = False,
    skip_upload: bool = False,
    headless: bool | None = None,
) -> UpsBatchResult:
    cfg = _load_config(config_path)
    browser_cfg = cfg.get("browser", {})
    mode = ups_browser_mode(browser_cfg)
    creds_optional = mode != "dedicated" and _env_bool(
        "UPS_SKIP_AUTO_LOGIN",
        default=mode == "cdp" and use_system_chrome_profile(browser_cfg),
    )
    creds = load_ups_credentials(cfg, optional=creds_optional)
    if creds.username:
        _log(f"UPS credentials loaded for {creds.username[:3]}… (auto-login enabled)")
    slow_mo = int(browser_cfg.get("slow_mo_ms") or 80)
    if headless is None:
        headless = bool(browser_cfg.get("headless", False))

    upload_csv = resolve_upload_csv(order_date=order_date, explicit_path=csv_path) if not skip_upload else None
    labels_dest = depot_labels_pdf_path(order_date)

    leave_browser_open = False
    with sync_playwright() as p:
        browser, context, page, persistent, launch_source = _open_browser(
            p, cfg, headless=headless, slow_mo=slow_mo
        )
        try:
            _ups_login(
                page, cfg, creds, manual=manual_login, launch_source=launch_source
            )
            _navigate_batch_shipping(page, cfg)
            if upload_csv is not None:
                _upload_csv(page, cfg, upload_csv)
            _fill_ship_from(page, cfg)
            _fill_payment(page, cfg)
            labels_page = _preview_and_process(page, cfg, context)
            _save_labels_pdf(labels_page, labels_dest)
            _save_session(context, persistent=persistent)
        except Exception as exc:
            leave_browser_open = _env_bool("UPS_LEAVE_BROWSER_OPEN_ON_ERROR", default=True)
            _log(f"ERROR: {exc}")
            if leave_browser_open:
                _log(
                    "Leaving Chrome open so you can inspect the page. "
                    "Close Chrome manually when done."
                )
            raise
        finally:
            if leave_browser_open:
                pass
            elif browser is not None:
                browser.close()
            else:
                context.close()

    return UpsBatchResult(
        csv_path=upload_csv or Path(""),
        labels_path=labels_dest,
        shipment_count=None,
    )


def run_ups_browser_setup(*, config_path: Path) -> None:
    """
    One-time UPS login into the dedicated local profile (FedEx-style).
    Log in manually, then press Enter in the console to save the session.
    """
    cfg = _load_config(config_path)
    browser_cfg = cfg.get("browser", {})
    slow_mo = int(browser_cfg.get("slow_mo_ms") or 80)
    home = _ups_home_url(cfg)
    profile_dir = dedicated_ups_profile_dir(browser_cfg)
    profile_dir.mkdir(parents=True, exist_ok=True)
    channel = _resolve_channel(browser_cfg) or "chrome"
    name = browser_display_name(channel)

    _log(f"Opening dedicated UPS browser profile in {name}: {profile_dir}")
    _log("Log into UPS in the browser window, then return here.")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(profile_dir),
            channel=channel,
            headless=False,
            slow_mo=slow_mo,
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation", "--no-sandbox"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(home, wait_until="domcontentloaded", timeout=120_000)
        print("\n>>> Log into UPS in Chrome, then press Enter here to save the session…")
        try:
            input()
        except EOFError:
            pass
        context.close()

    _log(f"Session saved in {profile_dir}. Set UPS_BROWSER_MODE=dedicated to use it.")
