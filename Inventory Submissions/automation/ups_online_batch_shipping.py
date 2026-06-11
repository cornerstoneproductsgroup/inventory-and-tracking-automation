"""UPS.com batch file shipping — Depot, Special Order, and Tractor lanes."""

from __future__ import annotations

import base64
import json
import os
import time
from urllib.parse import quote
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
    lane_labels_pdf_path,
    post_void_browser_wait_s,
    normalize_ups_lane,
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
from automation.ups_lane_csv import UpsCsvSkip, resolve_upload_csv
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
    base: list[str] = [
        "--disable-session-crashed-bubble",
        "--disable-restore-session-state",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if ups_browser_channel(browser_cfg) != "msedge":
        base.insert(0, "--disable-blink-features=AutomationControlled")
    if _using_system_chrome_profile(user_data_dir, browser_cfg):
        profile = browser_profile_directory()
        base.append(f"--profile-directory={profile}")
    out: list[str] = []
    for arg in [*base, *extra]:
        text = str(arg).strip()
        if text and text not in out:
            out.append(text)
    return out


def _ups_home_url(cfg: dict[str, Any]) -> str:
    ups = cfg.get("ups") or {}
    return str(ups.get("home_url") or DEFAULT_HOME_URL).strip()


def _ups_login_url(cfg: dict[str, Any]) -> str:
    ups = cfg.get("ups") or {}
    explicit = (os.environ.get("UPS_LOGIN_URL") or str(ups.get("login_url") or "")).strip()
    if explicit:
        return explicit
    home = _ups_home_url(cfg)
    returnto = quote(home, safe="")
    return f"https://www.ups.com/lasso/login?loc=en_US&returnto={returnto}"


_BLANK_TAB_URLS = frozenset(
    {
        "about:blank",
        "chrome://newtab/",
        "edge://newtab/",
        "",
    }
)


def _page_url(page: Page) -> str:
    try:
        return (page.url or "").strip()
    except Exception:
        return ""


def _is_blank_tab_url(url: str | None) -> bool:
    text = (url or "").strip().lower()
    return text in _BLANK_TAB_URLS or text.startswith("chrome://newtab") or text.startswith("edge://newtab")


def _is_ups_tab_url(url: str | None) -> bool:
    text = (url or "").strip().lower()
    return "ups.com" in text or "id.ups.com" in text


def _log_browser_tabs(context: BrowserContext) -> None:
    try:
        urls = [_page_url(pg) for pg in context.pages]
        _log(f"Browser tabs ({len(urls)}): {urls!r}")
    except Exception:
        pass


def _close_extra_tabs(context: BrowserContext, *, keep: Page) -> None:
    for pg in list(context.pages):
        if pg is keep:
            continue
        try:
            _log(f"Closing extra tab: {_page_url(pg)!r}")
            pg.close()
        except Exception:
            pass


def _navigate_current_tab(page: Page, url: str, *, label: str) -> Page:
    """Load a URL in the current tab (same as pasting into the address bar)."""
    current = _page_url(page)
    cur_l = current.lower()
    tgt_l = url.lower()
    if not _is_blank_tab_url(current):
        if cur_l.rstrip("/") == tgt_l.rstrip("/"):
            return page
        if "lasso/login" in tgt_l and "lasso/login" in cur_l:
            return page
        if "id.ups.com" in tgt_l and "id.ups.com" in cur_l:
            return page
    _log(f"{label} — tab was {current!r}")
    page.goto(url, wait_until="domcontentloaded", timeout=120_000)
    page.bring_to_front()
    _log(f"Now on: {_page_url(page)!r}")
    return page


def _ensure_ups_tab(page: Page, cfg: dict[str, Any]) -> Page:
    """
    Drive the first browser tab only. If it is about:blank, load UPS in that tab
    (do not hop to a restored UPS tab in the background).
    """
    context = page.context
    _log_browser_tabs(context)
    driver = context.pages[0] if context.pages else page
    driver.bring_to_front()
    _close_extra_tabs(context, keep=driver)

    current = _page_url(driver)
    if _is_blank_tab_url(current) or not _is_ups_tab_url(current):
        driver = _navigate_current_tab(
            driver,
            _ups_login_url(cfg),
            label="Blank tab — loading UPS login in this tab",
        )
    return driver


def _ensure_ups_home_page(page: Page, cfg: dict[str, Any]) -> Page:
    page = _ensure_ups_tab(page, cfg)
    home_url = _ups_home_url(cfg)
    current = _page_url(page).lower()
    if _is_blank_tab_url(current) or not _is_ups_tab_url(current):
        page = _navigate_current_tab(
            page,
            home_url,
            label="Loading UPS home in this tab",
        )
    else:
        _log(f"On UPS: {page.url}")
    page.wait_for_timeout(_timing_ms(cfg, "micro_pause_ms", "UPS_MICRO_PAUSE_MS", 400))
    clear_blocking_overlays(page, cfg, log=_log)
    return page


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
            page = _ensure_ups_home_page(page, cfg)
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
    page = context.pages[0] if context.pages else context.new_page()
    page = _ensure_ups_home_page(page, cfg)
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
                page = context.pages[0] if context.pages else context.new_page()
                page = _ensure_ups_home_page(page, cfg)
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
        loc = page.locator(sel).first
        for force in (False, True):
            try:
                loc.wait_for(state="visible", timeout=timeout_ms)
                loc.scroll_into_view_if_needed(timeout=3000)
                loc.click(timeout=timeout_ms, force=force)
                if label:
                    suffix = " (force)" if force else ""
                    _log(f"Clicked {label}{suffix}.")
                return True
            except Exception as exc:
                if force:
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


def _clear_field(page: Page, selector: str, *, label: str) -> None:
    for sel in [s.strip() for s in selector.split(",") if s.strip()]:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=12_000)
            loc.click(timeout=5000)
            loc.fill("")
            _log(f"Cleared {label}.")
            return
        except Exception:
            continue
    raise UpsBatchError(f"Could not clear {label}")


def _batch_lane_key(cfg: dict[str, Any]) -> str:
    raw = (os.environ.get("UPS_BATCH_LANE") or cfg.get("lane") or "depot").strip().lower()
    if raw in ("thdso", "depot_special", "depot_special_order", "special_order"):
        return "thdso"
    if raw in ("tractor", "tsc", "tractor_supply"):
        return "tractor"
    return "depot"


def _resolve_lane_settings(cfg: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Ship-from + payment for the active lane (depot, thdso, tractor)."""
    lane = _batch_lane_key(cfg)
    lanes = cfg.get("lanes") if isinstance(cfg.get("lanes"), dict) else {}
    lane_cfg = lanes.get(lane) or {}

    ship: dict[str, Any] = dict(cfg.get("ship_from") or {})
    ship.update(lane_cfg.get("ship_from") or {})
    pay: dict[str, Any] = dict(cfg.get("payment") or {})
    pay.update(lane_cfg.get("payment") or {})

    if not str(ship.get("company") or "").strip():
        ship["company"] = "TractorSupply" if lane == "tractor" else "HomeDepot.com"
    if not str(ship.get("contact") or "").strip():
        ship["contact"] = "Cornerstone Products Group"
    if not str(pay.get("third_party_account") or "").strip():
        pay["third_party_account"] = "87W6A8" if lane == "tractor" else "1YA668"
    if not str(pay.get("third_party_zip") or "").strip():
        pay["third_party_zip"] = "37027" if lane == "tractor" else "30339"
    pay.setdefault("third_party_country", "United States")
    pay.setdefault("billing_account_label", "186Y47 - Worldship")
    return ship, pay


def _select_my_default_address(page: Page, cfg: dict[str, Any]) -> None:
    """Pick saved default origin so UPS exposes third-party billing."""
    selectors = [
        s.strip()
        for s in (
            _sel(cfg, "my_addresses_dropdown"),
            "select.ups-dropdown:has(option:text-is('My Default Address'))",
            "xpath=//*[contains(normalize-space(.),'My Addresses')]/following::select[1]",
        )
        if s.strip()
    ]
    last_err: Exception | None = None
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=12_000)
            loc.scroll_into_view_if_needed(timeout=3000)
            loc.select_option(label="My Default Address")
            _log("Selected My Default Address.")
            page.wait_for_timeout(_timing_ms(cfg, "micro_pause_ms", "UPS_MICRO_PAUSE_MS", 400))
            return
        except Exception as exc:
            last_err = exc
    raise UpsBatchError(f"Could not select My Default Address: {last_err}")


def _click_ship_from_edit(page: Page, cfg: dict[str, Any]) -> None:
    if not _click_any(
        page,
        _sel(cfg, "ship_from_edit") or "span:has-text('Edit'), a:has-text('Edit')",
        label="Ship From Edit",
    ):
        raise UpsBatchError("Could not click Edit on Ship From address.")
    page.wait_for_timeout(_timing_ms(cfg, "micro_pause_ms", "UPS_MICRO_PAUSE_MS", 400))


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


def _login_field_selector(cfg: dict[str, Any], key: str, default: str) -> str:
    return _sel(cfg, key) or default


def _find_login_field(page: Page, cfg: dict[str, Any], key: str, *, default: str) -> Any:
    raw = _login_field_selector(cfg, key, default)
    selectors = [s.strip() for s in raw.split(",") if s.strip()]
    contexts = [page, *page.frames]
    for ctx in contexts:
        for sel in selectors:
            try:
                loc = ctx.locator(sel).first
                if loc.count() > 0 and loc.is_visible(timeout=600):
                    return loc
            except Exception:
                continue
    return page.locator(selectors[0]).first


def _open_ups_login_form(page: Page, cfg: dict[str, Any]) -> Page:
    """Navigate to UPS sign-in (avoids homepage teasers blocking Log In)."""
    page = _ensure_ups_tab(page, cfg)
    clear_blocking_overlays(page, cfg, log=_log)
    username_default = "#username, input[name='username'], input[type='email']"
    try:
        if _find_login_field(page, cfg, "username_input", default=username_default).is_visible(
            timeout=1500
        ):
            _log("UPS login form already open.")
            return page
    except Exception:
        pass

    login_url = _ups_login_url(cfg)
    page = _navigate_current_tab(
        page,
        login_url,
        label="Opening UPS login in this tab",
    )
    page.wait_for_timeout(_timing_ms(cfg, "micro_pause_ms", "UPS_MICRO_PAUSE_MS", 800))
    clear_blocking_overlays(page, cfg, log=_log)

    loc = _find_login_field(page, cfg, "username_input", default=username_default)
    loc.wait_for(state="visible", timeout=30_000)
    return page


def _type_login_field(page: Page, cfg: dict[str, Any], key: str, text: str, *, default: str) -> None:
    loc = _find_login_field(page, cfg, key, default=default)
    loc.wait_for(state="visible", timeout=30_000)
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
) -> Page:
    _log("Step 1/6: Open UPS home and clear popups…")
    page = _ensure_ups_home_page(page, cfg)
    clear_blocking_overlays(page, cfg, log=_log)

    if manual or _env_bool("UPS_MANUAL_LOGIN", default=False):
        _wait_for_manual_ups_login(page, cfg)
        return _ensure_ups_tab(page, cfg)

    if _is_ups_logged_in(page, cfg):
        _log("Already logged in (Chrome profile session) — skipping login.")
        return page

    has_creds = bool((creds.username or "").strip() and (creds.password or "").strip())
    skip_auto = launch_source in ("cdp", "manual") and _env_bool(
        "UPS_SKIP_AUTO_LOGIN",
        default=use_system_chrome_profile(cfg.get("browser")),
    )

    if skip_auto:
        clear_blocking_overlays(page, cfg, log=_log)
        if _is_ups_logged_in(page, cfg):
            _log("Logged in after clearing popups — continuing.")
            return page
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

    try:
        page = _open_ups_login_form(page, cfg)
    except Exception as exc:
        _log(f"WARN: direct login page failed ({exc}) — trying header Log In…")
        page = _ensure_ups_tab(page, cfg)
        clear_blocking_overlays(page, cfg, log=_log)
        if not _click_any(page, _sel(cfg, "header_login"), label="header Log In"):
            raise UpsBatchError("Could not open UPS login (direct URL or header Log In).") from exc
        page.wait_for_timeout(600)
        _click_any(page, _sel(cfg, "dropdown_login"), label="dropdown Log In")
        page = _ensure_ups_tab(page, cfg)

    _type_login_field(
        page,
        cfg,
        "username_input",
        creds.username,
        default="#username, input[name='username'], input[type='email']",
    )
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
    _type_login_field(
        page,
        cfg,
        "password_input",
        creds.password,
        default="#password, input[name='password'], input[type='password']",
    )
    if not _click_any(page, _sel(cfg, "login_continue"), label="Continue (password)"):
        raise UpsBatchError("Could not click Continue after password")

    page.wait_for_timeout(_timing_ms(cfg, "after_login_ms", "UPS_AFTER_LOGIN_MS", 2000))
    _log("Login submitted.")
    return _ensure_ups_tab(page, cfg)


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
    ship, _ = _resolve_lane_settings(cfg)
    lane = _batch_lane_key(cfg)
    _log(f"Ship From lane: {lane} — company {ship.get('company')!r}")

    _select_my_default_address(page, cfg)
    _click_ship_from_edit(page, cfg)

    _fill_field(
        page,
        _sel(cfg, "company_name"),
        str(ship.get("company") or "HomeDepot.com"),
        label="Company",
    )
    _fill_field(
        page,
        _sel(cfg, "contact_name"),
        str(ship.get("contact") or "Cornerstone Products Group"),
        label="Contact",
    )
    _clear_field(page, _sel(cfg, "email") or "#origin-cac_email", label="Email")


def _fill_payment(page: Page, cfg: dict[str, Any]) -> None:
    _, pay = _resolve_lane_settings(cfg)
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


def _wait_for_labels_ready(labels_page: Page, cfg: dict[str, Any]) -> None:
    """Give Process All time to finish rendering labels before saving PDF."""
    labels_page.wait_for_load_state("domcontentloaded", timeout=120_000)
    try:
        labels_page.wait_for_load_state("networkidle", timeout=180_000)
        _log("Label page network idle.")
    except Exception:
        _log("WARN: label page still loading — continuing after extra wait.")
    settle_ms = _timing_ms(cfg, "after_process_all_ms", "UPS_AFTER_PROCESS_ALL_MS", 45_000)
    _log(f"Waiting {settle_ms / 1000:.0f}s for all labels to finish loading…")
    labels_page.wait_for_timeout(settle_ms)


def _pause_for_void_window(seconds: float) -> None:
    if seconds <= 0:
        return
    total = int(seconds)
    _log(
        f"Labels saved — leaving browser open for {total}s "
        "so you can void shipments (bulk void is only available before closing the batch)."
    )
    milestones = {total, 90, 60, 30, 15, 10, 5}
    end = time.time() + seconds
    while True:
        remaining = end - time.time()
        if remaining <= 0:
            break
        sec_left = int(remaining) + (1 if remaining % 1 else 0)
        if sec_left in milestones:
            _log(f"  {sec_left}s until browser closes…")
            milestones.discard(sec_left)
        time.sleep(min(5.0, remaining))
    _log("Void window ended — closing browser.")


def _preview_and_process(page: Page, cfg: dict[str, Any], context: BrowserContext) -> Page:
    _log("Step 6/6: Preview Batch and Process All…")
    if not _click_any(page, _sel(cfg, "preview_batch"), label="Preview Batch"):
        raise UpsBatchError("Could not click Preview Batch")
    page.wait_for_timeout(3000)
    _log("Batch Processing page loaded.")

    process_sel = _sel(cfg, "process_all")
    with context.expect_page(timeout=180_000) as page_info:
        if not _click_any(page, process_sel, label="Process All", timeout_ms=30_000):
            raise UpsBatchError("Could not click Process All")
    labels_page = page_info.value
    _log("Label print/preview page opened.")
    _wait_for_labels_ready(labels_page, cfg)
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


def run_ups_batch(
    *,
    lane: str = "depot",
    config_path: Path,
    csv_path: Path | None = None,
    order_date: date | None = None,
    manual_login: bool = False,
    skip_upload: bool = False,
    headless: bool | None = None,
) -> UpsBatchResult:
    lane_key = normalize_ups_lane(lane)
    cfg = _load_config(config_path)
    cfg = dict(cfg)
    cfg["lane"] = lane_key
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

    _log(f"Lane: {lane_key}")
    upload_csv = (
        resolve_upload_csv(lane=lane_key, order_date=order_date, explicit_path=csv_path)
        if not skip_upload
        else None
    )
    labels_dest = lane_labels_pdf_path(lane_key, order_date)

    leave_browser_open = False
    post_void_wait_s = 0.0
    with sync_playwright() as p:
        browser, context, page, persistent, launch_source = _open_browser(
            p, cfg, headless=headless, slow_mo=slow_mo
        )
        try:
            page = _ups_login(
                page, cfg, creds, manual=manual_login, launch_source=launch_source
            )
            page = _ensure_ups_tab(page, cfg)
            _navigate_batch_shipping(page, cfg)
            page = _ensure_ups_tab(page, cfg)
            if upload_csv is not None:
                _upload_csv(page, cfg, upload_csv)
            page = _ensure_ups_tab(page, cfg)
            _fill_ship_from(page, cfg)
            _fill_payment(page, cfg)
            labels_page = _preview_and_process(page, cfg, context)
            _save_labels_pdf(labels_page, labels_dest)
            _save_session(context, persistent=persistent)
            post_void_wait_s = post_void_browser_wait_s(cfg)
        except Exception as exc:
            leave_browser_open = _env_bool("UPS_LEAVE_BROWSER_OPEN_ON_ERROR", default=True)
            _log(f"ERROR: {exc}")
            if leave_browser_open:
                _log(
                    "Leaving browser open so you can inspect the page. "
                    "Close it manually when done."
                )
            raise
        finally:
            if leave_browser_open or post_void_wait_s < 0:
                _log(
                    "Browser left open — void shipments in UPS, then close the window when finished."
                )
            elif post_void_wait_s > 0:
                _pause_for_void_window(post_void_wait_s)
                if browser is not None:
                    browser.close()
                else:
                    context.close()
            elif browser is not None:
                browser.close()
            else:
                context.close()

    return UpsBatchResult(
        csv_path=upload_csv or Path(""),
        labels_path=labels_dest,
        shipment_count=None,
    )


def run_ups_depot_batch(
    *,
    config_path: Path,
    csv_path: Path | None = None,
    order_date: date | None = None,
    manual_login: bool = False,
    skip_upload: bool = False,
    headless: bool | None = None,
) -> UpsBatchResult:
    """Backward-compatible entry point for the Home Depot lane."""
    return run_ups_batch(
        lane="depot",
        config_path=config_path,
        csv_path=csv_path,
        order_date=order_date,
        manual_login=manual_login,
        skip_upload=skip_upload,
        headless=headless,
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
        setup_args = _launch_args(browser_cfg, user_data_dir=profile_dir)
        context = p.chromium.launch_persistent_context(
            str(profile_dir),
            channel=channel,
            headless=False,
            slow_mo=slow_mo,
            args=setup_args,
            ignore_default_args=["--enable-automation", "--no-sandbox"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page = _navigate_current_tab(
            page,
            _ups_login_url(cfg),
            label="Setup — loading UPS login in this tab",
        )
        clear_blocking_overlays(page, cfg, log=_log)
        print("\n>>> Log into UPS in the browser, then press Enter here to save the session…")
        try:
            input()
        except EOFError:
            pass
        context.close()

    _log(f"Session saved in {profile_dir}. Set UPS_BROWSER_MODE=dedicated to use it.")
