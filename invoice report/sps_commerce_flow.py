"""
SPS Commerce (Tractor Supply path): login, fulfillment transactions, Advanced Search,
previous-business-day filters, bulk CSV download, and save to the Tractor Supply share.

Credentials and SPS_URL match the Inventory Feed project
(`Depot and Lowe's Automation with Inventory Feed / Inventory Submissions`):
  SPS_URL, SPS_USERNAME, SPS_PASSWORD  (+ optional TIMEOUT_MS / SPS_TIMEOUT_MS)

Navigation: after sign-in, SPS may land on https://commerce.spscommerce.com/home/apps/ (Apps hub)
or the classic tile launcher — both are treated as signed-in before opening the fulfillment
transactions list URL directly. Optional cookie banner (Reload Page) is handled on /fulfillment/ only.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import (
    BrowserContext,
    Download,
    Page,
    TimeoutError as PlaywrightTimeout,
)

# Same defaults as Inventory Submissions/.env.example
DEFAULT_SPS_DOTENV = Path(
    r"C:\Chat GPT Automation\Depot and Lowe's Automation with Inventory Feed\Inventory Submissions\.env"
)
TRANSACTIONS_LIST_URL = "https://commerce.spscommerce.com/fulfillment/transactions/list/"
_SPS_MODULE_DIR = Path(__file__).resolve().parent

# Username fields on SPS / embedded IdP (check main page + iframes).
_USERNAME_FIELD_SELECTORS: tuple[str, ...] = (
    "input[name='username']",
    "input#username",
    "input[type='email']",
    "input[name='email']",
    "input[name='identifier']",
    "input#okta-signin-username",
)


def sps_chromium_launch_args() -> list[str]:
    """
    Match Inventory Submissions automation/sps.py so cookies and IdP embeds work in headless Chromium.
    Optional extra flags: SPS_CHROMIUM_EXTRA_ARGS (space-separated, e.g. --disable-gpu).
    """
    import shlex

    base = [
        "--disable-features=BlockThirdPartyCookies,TrackingProtection3pcd",
        "--disable-blink-features=AutomationControlled",
    ]
    extra = (os.environ.get("SPS_CHROMIUM_EXTRA_ARGS") or "").strip()
    if extra:
        try:
            base.extend(shlex.split(extra))
        except ValueError:
            pass
    return base


async def _save_sps_debug_screenshot(page: Page, stem: str) -> Path:
    path = _SPS_MODULE_DIR / f"{stem}.png"
    try:
        await page.screenshot(path=str(path), full_page=True)
    except Exception:
        return path
    return path


def default_sps_dotenv_path() -> Path:
    raw = (os.environ.get("COMMERCEHUB_SPS_DOTENV") or "").strip()
    return Path(raw).expanduser() if raw else DEFAULT_SPS_DOTENV


def load_sps_env_from_inventory_project(*, override: bool = False) -> None:
    """Load SPS_* (and optional shared keys) from the Inventory Feed .env if present."""
    p = default_sps_dotenv_path()
    if p.is_file():
        load_dotenv(p, override=override)


def _sps_run_enabled() -> bool:
    return (os.environ.get("COMMERCEHUB_RUN_SPS_TRACTOR") or "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def load_sps_login_settings() -> tuple[str, str, str, int]:
    start_url = (os.environ.get("SPS_URL") or "").strip() or "https://commerce.spscommerce.com"
    username = (os.environ.get("SPS_USERNAME") or "").strip()
    password = (os.environ.get("SPS_PASSWORD") or "").strip()
    raw_t = (os.environ.get("SPS_TIMEOUT_MS") or os.environ.get("TIMEOUT_MS") or "30000").strip()
    try:
        timeout_ms = max(5_000, int(raw_t))
    except ValueError:
        timeout_ms = 30_000
    return start_url, username, password, timeout_ms


def _contexts(page: Page) -> list:
    """Page plus all frames (detached frames may fail individual locators; caller tries next)."""
    return [page, *page.frames]


async def _sps_locator_physically_usable(loc) -> bool:
    """
    True if the control exists and can be targeted (visible, or attached with real layout).
    Does not require is_editable() — SPS date fields are often readOnly with calendar-only entry.
    """
    try:
        if await loc.count() == 0:
            return False
        el = loc.first
        if await el.is_visible():
            return True
        await el.wait_for(state="attached", timeout=2_000)
        ok = await el.evaluate(
            """(e) => {
                const s = window.getComputedStyle(e);
                if (s.display === 'none' || s.visibility === 'hidden' || parseFloat(s.opacity||'1') < 0.05)
                  return false;
                const r = e.getBoundingClientRect();
                return r.width > 2 && r.height > 2;
            }"""
        )
        return bool(ok)
    except Exception:
        return False


async def _locate_sps_custom_date_input(page: Page):
    """First usable custom-date input on the main document or in an iframe."""
    for ctx in _contexts(page):
        for sel in (
            '[data-testid="customDate_date_input"]',
            '[data-testid*="customDate" i][data-testid*="input" i]',
            'input[placeholder*="MM/DD" i]',
        ):
            loc = ctx.locator(sel).first
            try:
                if await _sps_locator_physically_usable(loc):
                    return loc
            except Exception:
                continue
    return None


async def _locate_sps_doc_type_input(page: Page):
    """Document type multiselect input (main or iframe)."""
    for ctx in _contexts(page):
        for sel in (
            '[data-testid="advancedSearchDocTypesMultiselect__option-list-input"]',
            '[data-testid*="DocTypes" i][data-testid*="Multiselect" i]',
            'input[placeholder*="Select a Document Type" i]',
        ):
            loc = ctx.locator(sel).first
            try:
                if await _sps_locator_physically_usable(loc):
                    return loc
            except Exception:
                continue
    return None


async def _locate_sps_bottom_search_button(page: Page):
    """Advanced Search bottom-bar Search (criteria toolbar is sometimes inside an iframe)."""
    for ctx in _contexts(page):
        for sel in (
            '[data-testid="advSearchBottomSearchButton"]',
            '[data-testid*="advSearchBottomSearch" i]',
        ):
            loc = ctx.locator(sel).first
            try:
                if await _sps_locator_physically_usable(loc):
                    return loc
            except Exception:
                continue
    return None


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


async def _is_login_page_visible(page: Page) -> bool:
    try:
        url = page.url
        if _looks_logged_out(url):
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
        for ctx in _contexts(page):
            try:
                loc = ctx.locator(sel)
                if await loc.count() == 0:
                    continue
                if await loc.first.is_visible():
                    return True
            except Exception:
                continue
    return False


async def _sps_strict_cookie_wall_visible(page: Page) -> bool:
    """True only when SPS shows the real cookie interstitial (not a substring in unrelated HTML)."""
    try:
        warn = page.get_by_text(re.compile(r"cookies\s+are\s+disabled", re.I)).first
        reload = page.get_by_role("button", name=re.compile(r"reload\s+page", re.I)).first
        if await warn.count() == 0 or await reload.count() == 0:
            return False
        return await warn.is_visible() and await reload.is_visible()
    except Exception:
        return False


def _url_is_sps_signed_in_apps_home(url: str) -> bool:
    """SPS signed-in 'Apps' hub (Fulfillment tiles), e.g. …/home/apps/ — not /fulfillment/ yet."""
    u = (url or "").lower()
    return "commerce.spscommerce.com" in u and "/home/apps" in u


async def _on_sps_signed_in_launcher_home(page: Page) -> bool:
    """
    Signed-in commerce shell without being on fulfillment/transactions yet: classic tile
    launcher, or the newer /home/apps/ hub (screenshot: SPS Applications + Fulfillment tile).
    """
    try:
        url = (page.url or "").lower()
    except Exception:
        return False
    if "commerce.spscommerce.com" not in url:
        return False
    if _looks_logged_out(url):
        return False
    if "/fulfillment/" in url or "/transactions/" in url or "/dashboard/" in url:
        if await _is_login_page_visible(page):
            return False
        return True
    if _url_is_sps_signed_in_apps_home(url):
        if await _is_login_page_visible(page):
            return False
        return True
    if await _is_login_page_visible(page):
        return False
    for sel in (
        ".sps-tile",
        "img.sps-tile--image",
        "[class*='sps-tile']",
        "[class*='application-tile']",
        "a[href*='/fulfillment/']",
    ):
        try:
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                return True
        except Exception:
            continue
    try:
        apps = page.get_by_text(re.compile(r"SPS\s+Applications", re.I)).first
        if await apps.count() and await apps.is_visible():
            return True
    except Exception:
        pass
    return False


async def _sps_session_active(page: Page) -> bool:
    """Signed-in commerce (fulfillment, /home/apps hub, or tile launcher) and not on a login form."""
    if not (
        await _looks_authenticated_sps(page) or await _on_sps_signed_in_launcher_home(page)
    ):
        return False
    return not await _is_login_page_visible(page)


async def _wait_sps_post_nav_settle(page: Page, *, max_ms: int = 12_000) -> None:
    """Wait for SPA redirect to /home/apps/ or for a real login form — avoids missing an existing session."""
    deadline = time.monotonic() + max_ms / 1000.0
    while time.monotonic() < deadline:
        if await _sps_session_active(page):
            return
        try:
            u = (page.url or "").lower()
        except Exception:
            u = ""
        if "commerce.spscommerce.com" in u and await _is_login_page_visible(page):
            return
        await asyncio.sleep(0.25)


async def _looks_authenticated_sps(page: Page) -> bool:
    """
    True only when we appear to be inside the signed-in SPS app shell — not on the public
    marketing homepage (which often hides login fields and used to false-trigger "already signed in").
    """
    try:
        url = (page.url or "").lower()
    except Exception:
        url = ""
    if _looks_logged_out(url):
        return False
    if _url_is_sps_signed_in_apps_home(url):
        if await _is_login_page_visible(page):
            return False
        return True
    if any(x in url for x in ("/fulfillment/", "/dashboard/", "/transactions/")):
        if await _is_login_page_visible(page):
            return False
        return True
    appish = "/fulfillment/" in url or "/dashboard/" in url
    if not appish or await _is_login_page_visible(page):
        return False
    for sel in (
        "a[data-testid='dashboard_tab']",
        "a[href*='/fulfillment/transactions/list/']",
        "button:has-text('Advanced Search')",
        "button[data-testid='advSearchBottomSearchButton']",
    ):
        for ctx in _contexts(page):
            try:
                loc = ctx.locator(sel)
                if await loc.count() == 0:
                    continue
                if await loc.first.is_visible():
                    return True
            except Exception:
                continue
    return False


async def _raise_if_cookie_or_auth_wall(page: Page) -> None:
    if await _sps_strict_cookie_wall_visible(page):
        if await _click_first_visible(
            page,
            [
                "button:has-text('Reload Page')",
                "a:has-text('Reload Page')",
                "[role='button']:has-text('Reload Page')",
            ],
            timeout_ms=2_500,
        ):
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=90_000)
            except Exception:
                pass
            await page.wait_for_timeout(600)
            return
        raise RuntimeError(
            "SPS Commerce shows the cookie warning but no Reload Page control could be clicked. "
            "Enable cookies for Chromium / Playwright, then retry."
        )
    try:
        url = page.url
    except Exception:
        url = ""
    if _looks_logged_out(url) or await _is_login_page_visible(page):
        await page.wait_for_timeout(900)
        if await _sps_session_active(page):
            return
        raise RuntimeError(
            "SPS Commerce is not logged in (sign-in page or session expired). "
            "Confirm SPS_USERNAME / SPS_PASSWORD in .env (see Inventory Submissions/.env.example), "
            "or complete MFA if your org requires it (interactive run)."
        )


async def _click_first_visible(
    page: Page, selectors: list[str], *, timeout_ms: int = 10_000
) -> bool:
    for sel in selectors:
        for ctx in _contexts(page):
            try:
                loc = ctx.locator(sel)
                if await loc.count() == 0:
                    continue
                target = loc.first
                await target.wait_for(state="visible", timeout=timeout_ms)
                try:
                    await target.scroll_into_view_if_needed()
                except Exception:
                    pass
                try:
                    await target.click(timeout=timeout_ms)
                except Exception:
                    await target.click(timeout=timeout_ms, force=True)
                return True
            except Exception:
                continue
    return False


async def _maybe_reload_sps_cookie_banner(page: Page, log) -> None:
    """
    On the fulfillment transactions URL, SPS may show a cookie interstitial with Reload Page.
    Do not run this on the signed-in product home — substring checks there used to reload forever.
    """
    u = (page.url or "").lower()
    if "/fulfillment/" not in u:
        return
    for i in range(3):
        if not await _sps_strict_cookie_wall_visible(page):
            return
        if log:
            log(f"SPS: cookie interstitial on fulfillment URL; Reload Page ({i + 1}/3)…")
        clicked = await _click_first_visible(
            page,
            [
                "button:has-text('Reload Page')",
                "a:has-text('Reload Page')",
                "[role='button']:has-text('Reload Page')",
            ],
            timeout_ms=4_000,
        )
        if not clicked:
            break
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=120_000)
        except Exception:
            pass
        await page.wait_for_timeout(900)


async def _sps_disable_pointer_blocking_backdrops(page: Page, log=None) -> None:
    """
    Large tinted scrims (MUI/SPS backdrops) often sit above the form with pointer-events:auto.
    Temporarily set pointer-events:none on matching nodes so clicks reach date/inputs.
    Runs in the main document and every child frame (iframes can host their own color walls).
    """
    js = """() => {
      let k = 0;
      const mark = (el) => {
        if (k >= 45) return;
        const s = window.getComputedStyle(el);
        if (s.pointerEvents === 'none') return;
        const r = el.getBoundingClientRect();
        const vw = window.innerWidth;
        const vh = window.innerHeight;
        const area = r.width * r.height;
        const vp = vw * vh;
        const wide = r.width >= vw * 0.22;
        const tall = r.height >= vh * 0.12;
        const bigEnough = (wide && r.height >= 80) || (tall && r.width >= vw * 0.22)
          || area >= vp * 0.18;
        if (!bigEnough) return;
        el.dataset.spsPlaywrightPe = el.style.pointerEvents || '';
        el.style.pointerEvents = 'none';
        k++;
      };
      const sel =
        'div[class*="backdrop" i], div[class*="Backdrop"], div[class*="MuiBackdrop"], ' +
        'div[class*="overlay" i][class*="fixed" i], div[class*="scrim" i], ' +
        '[class*="loading-overlay" i], [class*="LoadingOverlay" i], ' +
        '[class*="loading-veil" i], [class*="LoadingVeil" i], ' +
        'div[class*="sps-loading" i], div[class*="sps"][class*="overlay" i], ' +
        'div[class*="page-loader" i], [class*="full-page" i][class*="overlay" i], ' +
        '[class*="blocking-overlay" i], [class*="color-wall" i]';
      document.querySelectorAll(sel).forEach(mark);
      return k;
    }"""
    total = 0
    for fr in page.frames:
        try:
            n = await fr.evaluate(js)
            if isinstance(n, int):
                total += n
        except Exception:
            continue
    if log and total > 0:
        log(
            f"SPS: set pointer-events:none on {total} backdrop/overlay layer(s) "
            f"across {len(page.frames)} frame(s) so clicks reach controls."
        )


async def _sps_wait_loading_veils_gone(page: Page, *, timeout_ms: int = 10_000) -> None:
    """Wait out tinted loading layers / spinners that sit above the form (best-effort, all frames)."""
    deadline = time.monotonic() + timeout_ms / 1000.0
    selectors = (
        "[class*='sps-loading']",
        "[class*='sps-Spinner']",
        "[class*='spinner'][class*='visible' i]",
        "[class*='loading-overlay' i]",
        "[class*='LoadingOverlay' i]",
        "[class*='progress'][class*='indeterminate' i]",
        "[class*='MuiBackdrop']",
        "[class*='backdrop'][class*='open' i]",
        "[class*='loading-veil' i]",
        "[class*='LoadingVeil' i]",
    )
    while time.monotonic() < deadline:
        visible_any = False
        for fr in page.frames:
            for sel in selectors:
                loc = fr.locator(sel).first
                try:
                    if await loc.count() == 0:
                        continue
                    if await loc.is_visible():
                        visible_any = True
                        try:
                            await loc.wait_for(state="hidden", timeout=2_500)
                        except Exception:
                            pass
                except Exception:
                    continue
        if not visible_any:
            return
        await asyncio.sleep(0.25)


async def _sps_bulk_download_followup_ui_open(page: Page) -> bool:
    """True after bulk download opens a menu, combine-CSV UI, or similar follow-up layer."""
    for ctx in _contexts(page):
        probes = (
            ctx.get_by_text(re.compile(r"Combine\s+documents", re.I)).first,
            ctx.locator('[data-testid="modalOkBtn"]').first,
            ctx.locator("[role='menuitem']").first,
            ctx.locator("[role='menu']").locator("[role='menuitem']").first,
            ctx.locator("[role='menu']:visible").first,
            ctx.locator("[class*='MuiPopover' i]:visible").first,
            ctx.locator("[class*='MuiPaper-root' i]:visible")
            .filter(has_text=re.compile(r"download|export|csv|document|format", re.I))
            .first,
            ctx.locator("[class*='sps-menu' i]:visible").first,
            ctx.locator("[class*='sps-dropdown' i]:visible").first,
        )
        for loc in probes:
            try:
                if await loc.count() == 0:
                    continue
                if await loc.is_visible():
                    return True
            except Exception:
                continue
    return False


async def _sps_cdp_click_viewport(page: Page, x: float, y: float) -> None:
    """Chromium CDP mouse at viewport CSS pixels (bypasses some hit-target quirks)."""
    cdp = await page.context.new_cdp_session(page)
    await cdp.send(
        "Input.dispatchMouseEvent",
        {
            "type": "mousePressed",
            "x": x,
            "y": y,
            "button": "left",
            "buttons": 1,
            "clickCount": 1,
        },
    )
    await cdp.send(
        "Input.dispatchMouseEvent",
        {
            "type": "mouseReleased",
            "x": x,
            "y": y,
            "button": "left",
            "buttons": 0,
            "clickCount": 1,
        },
    )


async def _sps_click_through_wall_for_locator(page: Page, loc) -> None:
    """
    Peel pointer-blocking layers (all frames already handled inside disable_backdrops),
    then force-click, elementFromPoint + host walk, OS mouse, and CDP at the control center.
    Used for header list / notification icons that sit under the same color walls as bulk download.
    """
    await _sps_disable_pointer_blocking_backdrops(page, None)
    await _sps_wait_loading_veils_gone(page, timeout_ms=3_500)
    try:
        await loc.scroll_into_view_if_needed(timeout=8_000)
    except Exception:
        pass
    try:
        await loc.click(timeout=8_000, force=True)
        return
    except Exception:
        pass
    try:
        await loc.evaluate(
            """(el) => {
              const doc = el.ownerDocument;
              const r = el.getBoundingClientRect();
              const x = r.left + Math.max(1, Math.min(r.width - 1, r.width / 2));
              const y = r.top + Math.max(1, Math.min(r.height - 1, r.height / 2));
              let hit = doc.elementFromPoint(x, y);
              for (let i = 0; i < 14 && hit; i++) {
                const t = hit.tagName;
                const role = (hit.getAttribute('role') || '').toLowerCase();
                if (t === 'BUTTON' || t === 'A' || role === 'button') {
                  try { hit.click(); return; } catch (e) {}
                }
                if (hit.getAttribute('tabindex') === '0') {
                  try { hit.click(); return; } catch (e) {}
                }
                const cls = (hit.getAttribute('class') || '');
                if (/sps-icon-button|toolbar|clickable|action/i.test(cls)) {
                  try { hit.click(); return; } catch (e) {}
                }
                hit = hit.parentElement;
              }
              try { el.click(); } catch (e) {}
            }"""
        )
        return
    except Exception:
        pass
    try:
        box = await loc.bounding_box()
        if box and box.get("width", 0) > 0.5:
            cx = box["x"] + box["width"] / 2
            cy = box["y"] + box["height"] / 2
            await page.mouse.move(cx, cy)
            await page.wait_for_timeout(35)
            await page.mouse.down()
            await page.wait_for_timeout(25)
            await page.mouse.up()
    except Exception:
        pass
    try:
        box = await loc.bounding_box()
        if box and box.get("width", 0) > 0.5:
            await _sps_cdp_click_viewport(page, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    except Exception:
        pass


async def _sps_try_bulk_download_target(page: Page, loc) -> bool:
    """
    Peel overlays, then run several activation strategies until a download-related UI opens.
    Returns False if nothing appears (caller should try another locator).
    """
    await _sps_disable_pointer_blocking_backdrops(page, None)
    await _sps_wait_loading_veils_gone(page, timeout_ms=4_500)
    try:
        await loc.scroll_into_view_if_needed(timeout=6_000)
    except Exception:
        pass

    async def _snip(max_ms: int = 900) -> bool:
        steps = max(1, (max_ms + 99) // 100)
        for _ in range(steps):
            if await _sps_bulk_download_followup_ui_open(page):
                return True
            await page.wait_for_timeout(100)
        return False

    async def _element_from_point_click() -> None:
        try:
            await loc.evaluate(
                """(el) => {
                  const doc = el.ownerDocument;
                  const r = el.getBoundingClientRect();
                  const x = r.left + Math.max(1, Math.min(r.width - 1, r.width / 2));
                  const y = r.top + Math.max(1, Math.min(r.height - 1, r.height / 2));
                  let hit = doc.elementFromPoint(x, y);
                  for (let i = 0; i < 14 && hit; i++) {
                    const tag = hit.tagName;
                    const role = (hit.getAttribute('role') || '').toLowerCase();
                    if (tag === 'BUTTON' || tag === 'A' || role === 'button') {
                      try { hit.click(); return; } catch (e) {}
                    }
                    if (hit.getAttribute('tabindex') === '0') {
                      try { hit.click(); return; } catch (e) {}
                    }
                    const cls = (hit.getAttribute('class') || '');
                    if (/sps-icon-button|sps-toolbar|clickable|action-btn/i.test(cls)) {
                      try { hit.click(); return; } catch (e) {}
                    }
                    hit = hit.parentElement;
                  }
                  try { el.click(); } catch (e) {}
                }"""
            )
        except Exception:
            pass

    async def _walk_ancestors_native_click() -> None:
        try:
            await loc.evaluate(
                """(el) => {
                  let n = el;
                  for (let i = 0; i < 16 && n; i++) {
                    const tag = n.tagName;
                    const role = (n.getAttribute('role') || '').toLowerCase();
                    if (tag === 'BUTTON' || tag === 'A' || role === 'button') {
                      try { n.click(); return; } catch (e) {}
                    }
                    if (n.getAttribute('tabindex') === '0' && typeof n.click === 'function') {
                      try { n.click(); return; } catch (e) {}
                    }
                    const cls = (n.getAttribute('class') || '');
                    if (/sps-icon-button|toolbar|clickable|action/i.test(cls)) {
                      try { n.click(); return; } catch (e) {}
                    }
                    n = n.parentElement;
                  }
                }"""
            )
        except Exception:
            pass

    async def _pointer_synth_on_host() -> None:
        try:
            await loc.evaluate(
                """(el) => {
                  let t = el;
                  for (let i = 0; i < 10 && t; i++) {
                    const tag = t.tagName;
                    const role = (t.getAttribute('role') || '').toLowerCase();
                    if (tag === 'BUTTON' || tag === 'A' || role === 'button') break;
                    t = t.parentElement;
                  }
                  if (!t) t = el;
                  const v = window;
                  const o = { bubbles: true, cancelable: true, view: v };
                  try {
                    t.dispatchEvent(new PointerEvent('pointerdown', {
                      bubbles: true, cancelable: true, view: v, pointerId: 1,
                      pointerType: 'mouse', isPrimary: true,
                    }));
                  } catch (e1) {
                    t.dispatchEvent(new MouseEvent('mousedown', o));
                  }
                  try {
                    t.dispatchEvent(new PointerEvent('pointerup', {
                      bubbles: true, cancelable: true, view: v, pointerId: 1,
                      pointerType: 'mouse', isPrimary: true,
                    }));
                  } catch (e2) {
                    t.dispatchEvent(new MouseEvent('mouseup', o));
                  }
                  t.dispatchEvent(new MouseEvent('click', o));
                }"""
            )
        except Exception:
            pass

    for round_i in range(2):
        await _element_from_point_click()
        if await _snip(900):
            return True
        await _walk_ancestors_native_click()
        if await _snip(900):
            return True
        try:
            await loc.evaluate(
                """(el) => {
                  let n = el;
                  for (let i = 0; i < 14 && n; i++) {
                    if (typeof n.click === 'function') {
                      try { n.click(); } catch (e) {}
                    }
                    const tag = n.tagName;
                    const role = (n.getAttribute('role') || '').toLowerCase();
                    if (tag === 'BUTTON' || tag === 'A' || role === 'button') break;
                    n = n.parentElement;
                  }
                }"""
            )
        except Exception:
            pass
        if await _snip(900):
            return True
        await _pointer_synth_on_host()
        if await _snip(900):
            return True
        try:
            await loc.click(timeout=8_000, force=True)
        except Exception:
            pass
        if await _snip(900):
            return True
        try:
            box = await loc.bounding_box()
            if box and box.get("width", 0) > 0.5 and box.get("height", 0) > 0.5:
                cx = box["x"] + box["width"] / 2
                cy = box["y"] + box["height"] / 2
                await page.mouse.move(cx, cy)
                await page.wait_for_timeout(35)
                await page.mouse.down()
                await page.wait_for_timeout(25)
                await page.mouse.up()
                if await _snip(900):
                    return True
                try:
                    await _sps_cdp_click_viewport(page, cx, cy)
                except Exception:
                    pass
                if await _snip(900):
                    return True
        except Exception:
            pass
        try:
            await loc.dispatch_event("click", {"bubbles": True})
        except Exception:
            pass
        if await _snip(900):
            return True
        if round_i == 0:
            await _sps_disable_pointer_blocking_backdrops(page, None)
            await _sps_wait_loading_veils_gone(page, timeout_ms=3_000)
    if await _snip(5_000):
        return True
    return False


async def _lift_sps_ui_blockers(page: Page, log=None) -> None:
    """
    Dismiss modals, cookie interstitial, and loading 'color walls' that block Advanced Search clicks.
    Call before each major step on the transactions / Advanced Search UI.
    """
    await _maybe_reload_sps_cookie_banner(page, log)
    await _sps_disable_pointer_blocking_backdrops(page, log)
    await _sps_wait_loading_veils_gone(page, timeout_ms=8_000)
    for _ in range(3):
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(150)
    dismiss = [
        "button:has-text('Got it')",
        "button:has-text('Dismiss')",
        "button:has-text('I Understand')",
        "button:has-text('Accept')",
        "button:has-text('Continue')",
        "button:has-text('Close')",
        "button:has-text('Not now')",
        "button:has-text('Skip')",
        "[data-testid*='close' i][role='button']",
        "[aria-label='Close' i]",
        "button[aria-label='Close']",
        ".sps-modal__close",
        "button.sps-modal__close",
    ]
    for _ in range(2):
        clicked = False
        for sel in dismiss:
            if await _click_first_visible(page, [sel], timeout_ms=1_200):
                clicked = True
                if log:
                    log(f"SPS: dismissed blocking UI ({sel!r}).")
                await page.wait_for_timeout(450)
                break
        if not clicked:
            break
    await _sps_disable_pointer_blocking_backdrops(page, log)
    await _sps_wait_loading_veils_gone(page, timeout_ms=6_000)


async def _sps_light_transactions_form_prep(page: Page, log=None) -> None:
    """Cheaper than _lift_sps_ui_blockers — use between date and doc type / Search (fewer Escapes)."""
    await _maybe_reload_sps_cookie_banner(page, log)
    await _sps_disable_pointer_blocking_backdrops(page, log)
    await _sps_wait_loading_veils_gone(page, timeout_ms=3_500)


async def _wait_sps_advanced_search_actionable(
    page: Page, *, timeout_ms: int, log=None
) -> None:
    """
    Wait until the custom date field exists on the main page or in an iframe and can be targeted.
    Does not require is_editable() (SPS often uses readOnly + calendar). Does not pre-click the field
    (fill step handles focus).
    """
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        await _lift_sps_ui_blockers(page, log)
        await _raise_if_cookie_or_auth_wall(page)
        date_loc = await _locate_sps_custom_date_input(page)
        if date_loc is not None:
            if log:
                log("SPS: custom date field is usable (page or iframe); continuing.")
            return
        await asyncio.sleep(0.28)
    raise RuntimeError(
        "SPS Advanced Search: custom date field not found or not targetable on the page or in iframes. "
        "If a colored overlay remains, run with COMMERCEHUB_HEADLESS=false and complete any prompts."
    )


async def _goto_transactions_list_direct(
    page: Page,
    *,
    ready_timeout: int,
    nav_timeout_ms: int,
    log,
) -> None:
    """Open the fulfillment transactions list URL (retry if SPA leaves you on launcher home)."""
    last_url = ""
    for nav_try in range(1, 4):
        if log:
            log(f"SPS: navigating to transactions list (try {nav_try}/3)…")
        await page.goto(TRANSACTIONS_LIST_URL, wait_until="domcontentloaded", timeout=nav_timeout_ms)
        await _maybe_reload_sps_cookie_banner(page, log)
        try:
            last_url = page.url or ""
        except Exception:
            last_url = ""
        if "/fulfillment/transactions" in last_url.lower():
            break
        if log:
            log(f"SPS: expected transactions URL but got {last_url!r}; retrying…")
        await page.wait_for_timeout(900)
    else:
        raise RuntimeError(
            f"SPS: could not open fulfillment transactions list after 3 tries. Last URL: {last_url!r}"
        )

    if log:
        log("SPS: waiting for transactions page (Advanced Search / table)…")
    await wait_for_transactions_page_ready(page, timeout_ms=ready_timeout)


async def wait_for_transactions_page_ready(page: Page, *, timeout_ms: int = 45_000) -> None:
    deadline = time.monotonic() + timeout_ms / 1000.0
    ready_selectors = [
        "button:has-text('Advanced Search')",
        "input[placeholder*='Search here for a document']",
        "button[data-testid='advSearchBottomSearchButton']",
        "table",
        "tbody",
    ]
    while time.monotonic() < deadline:
        await _raise_if_cookie_or_auth_wall(page)
        for sel in ready_selectors:
            for ctx in _contexts(page):
                try:
                    loc = ctx.locator(sel)
                    if await loc.count() == 0:
                        continue
                    if await loc.first.is_visible():
                        return
                except Exception:
                    continue
        await asyncio.sleep(0.2)
    raise RuntimeError("SPS transactions page did not become ready in time.")


async def _maybe_click_sps_marketing_login(page: Page, log) -> None:
    """Home/marketing shell often hides the username form until Log in / Sign in is clicked."""
    if await _looks_authenticated_sps(page) or await _on_sps_signed_in_launcher_home(page):
        return
    if log:
        log("SPS: looking for marketing Log in / Sign in…")
    await _click_first_visible(
        page,
        [
            "button:has-text('Log in')",
            "a:has-text('Log in')",
            "button:has-text('Sign in')",
            "a:has-text('Sign in')",
            "a[href*='signin']",
            "a[href*='sign-in']",
        ],
        timeout_ms=5_000,
    )
    await page.wait_for_timeout(800)


async def _find_username_locator(
    page: Page, *, timeout_ms: int
) -> tuple[object, object] | tuple[None, None]:
    """Return (context, locator) for first visible username/email field (page or iframe)."""
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        for ctx in _contexts(page):
            for sel in _USERNAME_FIELD_SELECTORS:
                try:
                    loc = ctx.locator(sel).first
                    if await loc.count() == 0:
                        continue
                    if await loc.is_visible():
                        return ctx, loc
                except Exception:
                    continue
        await asyncio.sleep(0.25)
    return None, None


async def _password_locator(ctx: object, page: Page) -> object:
    for sel in ("input[name='password']", "input#password", "input[type='password']"):
        loc = ctx.locator(sel).first
        try:
            if await loc.count() and await loc.is_visible():
                return loc
        except Exception:
            continue
    loc = page.locator("input[name='password']").first
    if await loc.count():
        return loc
    return page.locator("input[type='password']").first


async def _perform_sps_login(
    page: Page, username: str, password: str, timeout_ms: int, log=None
) -> tuple[bool, str]:
    """
    Returns (True, "") on success, or (False, reason) for diagnostics.
    Resolves fields in iframes (Okta / Azure embeds) like Inventory sps.py needs for cookies.
    """
    if not username or not password:
        return False, "SPS_USERNAME or SPS_PASSWORD is empty."
    per_attempt = max(12_000, min(timeout_ms, 45_000))
    last_detail = "Unknown failure."
    for attempt in range(1, 4):
        try:
            if log:
                log(f"SPS: login attempt {attempt}/3…")

            if await _sps_session_active(page):
                if log:
                    log(
                        "SPS: already signed in (fulfillment or Apps home); "
                        "skipping username/password for this attempt."
                    )
                return True, ""

            ctx, user_loc = await _find_username_locator(page, timeout_ms=4_000)
            if ctx is None or user_loc is None:
                if await _sps_session_active(page):
                    if log:
                        log(
                            "SPS: signed-in shell detected (e.g. https://…/home/apps/) "
                            "with no credential form; treating login as complete."
                        )
                    return True, ""
                await _maybe_click_sps_marketing_login(page, log)
                ctx, user_loc = await _find_username_locator(page, timeout_ms=per_attempt)
            if ctx is None or user_loc is None:
                last_detail = (
                    "Could not find username/email field on the page or in iframes. "
                    "If you use SSO only, run with COMMERCEHUB_HEADLESS=false and sign in manually once."
                )
                if log:
                    log(f"SPS: {last_detail}")
                raise RuntimeError(last_detail)

            await user_loc.click(timeout=3_000)
            await user_loc.fill("")
            await user_loc.fill(username)
            next_btn = ctx.locator("button._button-login-id").first
            if await next_btn.count() == 0:
                next_btn = page.locator("button._button-login-id").first
            if await next_btn.count() > 0:
                try:
                    await next_btn.click(timeout=5_000)
                except Exception:
                    await next_btn.click(timeout=5_000, force=True)
            else:
                await user_loc.press("Enter", timeout=3_000)

            pwd_loc = await _password_locator(ctx, page)
            await pwd_loc.wait_for(state="visible", timeout=per_attempt)
            await pwd_loc.click(timeout=2_000)
            await pwd_loc.fill("")
            await pwd_loc.fill(password)
            submit_btn = ctx.locator("button._button-login-password").first
            if await submit_btn.count() == 0:
                submit_btn = page.locator("button._button-login-password").first
            if await submit_btn.count() > 0:
                try:
                    await submit_btn.click(timeout=5_000)
                except Exception:
                    await submit_btn.click(timeout=5_000, force=True)
            else:
                await pwd_loc.press("Enter", timeout=3_000)

            await page.wait_for_load_state("domcontentloaded", timeout=per_attempt)
            await page.wait_for_timeout(600)
            if await _sps_session_active(page):
                return True, ""
            await page.wait_for_timeout(900)
            if await _sps_session_active(page):
                return True, ""
            last_detail = "Submitted username/password but SPS did not reach an authenticated app URL (MFA, wrong password, or captcha)."
        except Exception as exc:
            last_detail = str(exc)
            if log:
                log(f"SPS: attempt {attempt} error: {exc!r}")

        if attempt < 3:
            try:
                await page.goto(
                    "https://commerce.spscommerce.com",
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
                await page.wait_for_timeout(600)
                await _wait_sps_post_nav_settle(page, max_ms=12_000)
            except Exception:
                pass

    return False, last_detail


async def _wait_for_authenticated_sps(page: Page, *, timeout_ms: int = 120_000) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        if await _sps_session_active(page):
            return True
        await asyncio.sleep(0.4)
    return False


async def login_sps_and_open_transactions(
    page: Page,
    *,
    nav_timeout_ms: int = 120_000,
    auth_wait_ms: int = 180_000,
    log=None,
) -> None:
    start_url, username, password, settings_timeout = load_sps_login_settings()
    if log:
        log(
            f"SPS: opening {start_url} (username {'set' if username else 'EMPTY'}, "
            f"password {'set' if password else 'EMPTY'})."
        )
    try:
        await page.goto(start_url, wait_until="load", timeout=min(nav_timeout_ms, 180_000))
    except PlaywrightTimeout:
        await page.goto(start_url, wait_until="domcontentloaded", timeout=nav_timeout_ms)

    await _wait_sps_post_nav_settle(page, max_ms=12_000)

    ready_timeout = max(45_000, nav_timeout_ms // 2)
    session_confirmed = False

    if log:
        log("SPS: checking for an existing session (transactions / Apps / Fulfillment launcher)…")

    if await _sps_session_active(page):
        if log:
            log("SPS: signed-in session detected; opening transactions list…")
        try:
            await _goto_transactions_list_direct(
                page,
                ready_timeout=ready_timeout,
                nav_timeout_ms=nav_timeout_ms,
                log=log,
            )
            session_confirmed = True
        except RuntimeError as exc:
            low = str(exc).lower()
            if "not logged in" in low or "session expired" in low:
                if log:
                    log(
                        "SPS: session was not actually usable (sign-in likely pending); "
                        "continuing with username/password…"
                    )
            else:
                raise

    if session_confirmed:
        return

    if not username or not password:
        raise RuntimeError(
            "SPS Commerce requires sign-in but SPS_USERNAME or SPS_PASSWORD is empty."
        )
    if log:
        log("SPS: signing in…")
    try:
        await page.goto(start_url, wait_until="load", timeout=min(nav_timeout_ms, 180_000))
    except PlaywrightTimeout:
        await page.goto(start_url, wait_until="domcontentloaded", timeout=nav_timeout_ms)

    await _wait_sps_post_nav_settle(page, max_ms=12_000)

    ok, login_detail = await _perform_sps_login(
        page, username, password, settings_timeout, log=log
    )
    if not ok:
        snap = await _save_sps_debug_screenshot(page, "debug_sps_login_failed")
        if log:
            log(f"SPS: saved login debug screenshot: {snap}")
        raise RuntimeError(
            f"SPS Commerce login did not complete. {login_detail} "
            f"If the page uses MFA or SSO, set COMMERCEHUB_HEADLESS=false. Screenshot: {snap}"
        )
    if not await _wait_for_authenticated_sps(page, timeout_ms=auth_wait_ms):
        snap = await _save_sps_debug_screenshot(page, "debug_sps_auth_timeout")
        if log:
            log(f"SPS: saved post-login screenshot: {snap}")
        raise RuntimeError(
            "SPS credentials may have been accepted but the app never reached a signed-in state "
            f"(MFA, captcha, or slow IdP). Run with COMMERCEHUB_HEADLESS=false. Screenshot: {snap}"
        )
    if log:
        log("SPS: sign-in finished; opening transactions list…")
    await _goto_transactions_list_direct(
        page,
        ready_timeout=ready_timeout,
        nav_timeout_ms=nav_timeout_ms,
        log=log,
    )


async def open_advanced_search_panel(
    page: Page, *, step_timeout_ms: int = 15_000, log=None
) -> None:
    """
    Open the Advanced Search drawer if it is not already open.

    Readiness uses multiple signals (data-testid, placeholders, labels, all frames) because
    SPS may render criteria in an iframe, use variant test ids, or keep inputs attached but not
    yet passing strict Playwright visibility during animations / overlays.
    """

    def _candidate_selectors() -> tuple[str, ...]:
        # Do NOT use advSearchBottomSearchButton here — it stays visible on the collapsed
        # transactions toolbar and caused a false "panel already open" (no expand click).
        return (
            '[data-testid="customDate_date_input"]',
            '[data-testid*="customDate" i][data-testid*="input" i]',
            '[data-testid="advancedSearchDocTypesMultiselect__option-list-input"]',
            '[data-testid*="DocTypes" i][data-testid*="Multiselect" i]',
            'input[data-testid="advancedSearchWorkflowsMultiselect__option-list-input"]',
            'input[placeholder*="MM/DD" i]',
            'input[placeholder*="Select a Document Type" i]',
        )

    async def _any_advanced_form_ready() -> bool:
        for ctx in _contexts(page):
            for sel in _candidate_selectors():
                try:
                    if await _sps_locator_physically_usable(ctx.locator(sel)):
                        return True
                except Exception:
                    continue
            # Section heading + text inputs (handles renamed test ids)
            try:
                hdr = ctx.get_by_text(re.compile(r"Custom\s+Date\s+Range", re.I)).first
                if await hdr.count() == 0:
                    continue
                if not await hdr.is_visible():
                    continue
                row = ctx.locator("div, section, form").filter(
                    has_text=re.compile(r"Custom\s+Date\s+Range", re.I)
                ).filter(has=ctx.locator("input.sps-text-input__input")).first
                if await row.count() and await _sps_locator_physically_usable(
                    row.locator("input.sps-text-input__input").first
                ):
                    return True
            except Exception:
                continue
        return False

    await _lift_sps_ui_blockers(page, log)
    if await _any_advanced_form_ready():
        if log:
            log("SPS: Advanced Search panel already open.")
        return

    async def _click_expand_advanced_search() -> bool:
        for ctx in _contexts(page):
            for name in (
                re.compile(r"^\s*advanced\s*search\s*$", re.I),
                re.compile(r"advanced\s*search", re.I),
            ):
                try:
                    btn = ctx.get_by_role("button", name=name).first
                    if await btn.count() == 0:
                        continue
                    await btn.wait_for(state="visible", timeout=3_000)
                    await btn.scroll_into_view_if_needed()
                    try:
                        await btn.click(timeout=4_000)
                    except Exception:
                        await btn.click(timeout=4_000, force=True)
                    return True
                except Exception:
                    continue
        return await _click_first_visible(
            page,
            [
                "xpath=//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'advanced search')]",
                "xpath=//button[normalize-space()='Advanced Search']",
                "button:has-text('Advanced Search')",
                "[role='button']:has-text('Advanced Search')",
                "a:has-text('Advanced Search')",
                "[aria-label*='Advanced Search' i]",
                "[data-testid*='advancedSearch' i][data-testid*='toggle' i]",
                "[data-testid*='expand' i][data-testid*='advanced' i]",
                "text=/advanced\\s*search/i",
            ],
            timeout_ms=4_000,
        )

    if log:
        log("SPS: clicking Advanced Search to expand criteria…")
    if not await _click_expand_advanced_search():
        raise RuntimeError(
            "Could not click Advanced Search on SPS transactions page "
            "(no matching control in main page or iframes)."
        )

    if log:
        log("SPS: waiting for Advanced Search form (date / document type / search controls)…")
    deadline = time.monotonic() + step_timeout_ms / 1000.0
    nudge = 0
    while time.monotonic() < deadline:
        await _lift_sps_ui_blockers(page, log)
        if await _any_advanced_form_ready():
            await _lift_sps_ui_blockers(page, log)
            if log:
                log("SPS: Advanced Search criteria controls are ready.")
            return
        nudge += 1
        if nudge % 20 == 0:
            if log:
                log("SPS: still waiting for Advanced Search fields; nudging expand again…")
            await _click_expand_advanced_search()
        if nudge % 60 == 0 and log:
            log(f"SPS: still waiting for Advanced Search form (~{nudge * 0.35:.0f}s)…")
        await asyncio.sleep(0.35)

    raise RuntimeError(
        "Advanced Search did not show expected controls in time. "
        "Tried data-testid variants, placeholders, and 'Custom Date Range' section on the main page "
        "and in iframes. Run with COMMERCEHUB_HEADLESS=false and confirm the criteria strip is visible."
    )


def sps_custom_date_range_value(report_day: date) -> str:
    """Same calendar day twice: MM/DD/YYYY-MM/DD/YYYY (SPS Custom Date Range)."""
    token = report_day.strftime("%m/%d/%Y")
    return f"{token}-{token}"


async def _sps_matching_results_count(page: Page) -> int | None:
    for ctx in _contexts(page):
        try:
            loc = ctx.get_by_text(re.compile(r"Matching\s+Results:\s*(\d+)", re.I)).first
            if await loc.count() == 0:
                continue
            if not await loc.is_visible():
                continue
            txt = await loc.inner_text()
            m = re.search(r"Matching\s+Results:\s*(\d+)", txt, re.I)
            if m:
                return int(m.group(1))
        except Exception:
            continue
    return None


async def _sps_wait_stable_matching_results(
    page: Page, *, timeout_ms: int, log=None
) -> int:
    await _lift_sps_ui_blockers(page, log)
    deadline = time.monotonic() + timeout_ms / 1000.0
    last: int | None = None
    same_streak = 0
    tick = 0
    while time.monotonic() < deadline:
        tick += 1
        if tick % 12 == 1:
            await _lift_sps_ui_blockers(page, log)
        await _raise_if_cookie_or_auth_wall(page)
        n = await _sps_matching_results_count(page)
        if n is not None:
            if n == last:
                same_streak += 1
            else:
                same_streak = 1
                last = n
            if same_streak >= 2:
                return n
        await asyncio.sleep(0.35)
    raise RuntimeError(
        "SPS Advanced Search did not show a stable 'Matching Results: N' line in time."
    )


async def _set_sps_text_input_value(loc, value: str, *, field_label: str) -> None:
    """Focus, fill, and if React ignores fill, assign value + dispatch input/change (common under overlays)."""
    await loc.click(timeout=10_000, force=True)
    try:
        await loc.fill("")
        await loc.fill(value)
    except Exception:
        await loc.evaluate(
            """(el, v) => {
                el.focus();
                el.value = v;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                el.dispatchEvent(new Event('blur', { bubbles: true }));
            }""",
            value,
        )
    try:
        cur = await loc.input_value()
    except Exception:
        return
    if (cur or "").strip() != (value or "").strip():
        await loc.evaluate(
            """(el, v) => {
                el.focus();
                el.value = v;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }""",
            value,
        )
        cur2 = await loc.input_value()
        if (cur2 or "").strip() != (value or "").strip():
            raise RuntimeError(
                f"SPS: could not set {field_label} (expected {value!r}, got {cur2!r})."
            )


_SPS_REACT_SET_INPUT_JS = """([selectors, value]) => {
  let el = null;
  for (const sel of selectors) {
    el = document.querySelector(sel);
    if (el) break;
  }
  if (!el || el.tagName !== 'INPUT') return { ok: false, reason: 'no-input' };
  try {
    const desc = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
    if (desc && desc.set) desc.set.call(el, value);
    else el.value = value;
  } catch (e) {
    el.value = value;
  }
  el.focus();
  try {
    el.dispatchEvent(new InputEvent('input', { bubbles: true, data: value, inputType: 'insertFromPaste' }));
  } catch (e) {
    el.dispatchEvent(new Event('input', { bubbles: true }));
  }
  el.dispatchEvent(new Event('change', { bubbles: true }));
  el.dispatchEvent(new Event('blur', { bubbles: true }));
  return { ok: true, value: el.value };
}"""


async def _sps_force_react_value_on_locator(date_loc, value: str, log) -> bool:
    """Native HTMLInputElement value setter + events on the exact date field (avoids wrong-frame matches)."""
    try:
        res = await date_loc.evaluate(
            """(el, val) => {
                if (!el || el.tagName !== 'INPUT') return { ok: false, reason: 'not-input' };
                try {
                  const desc = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value'
                  );
                  if (desc && desc.set) desc.set.call(el, val);
                  else el.value = val;
                } catch (e) {
                  el.value = val;
                }
                el.focus();
                try {
                  el.dispatchEvent(new InputEvent('input', {
                    bubbles: true, data: val, inputType: 'insertFromPaste'
                  }));
                } catch (e) {
                  el.dispatchEvent(new Event('input', { bubbles: true }));
                }
                el.dispatchEvent(new Event('change', { bubbles: true }));
                el.dispatchEvent(new Event('blur', { bubbles: true }));
                return { ok: true, value: el.value };
            }""",
            value,
        )
        if isinstance(res, dict) and res.get("ok") and (res.get("value") or "").strip():
            if log:
                log(f"SPS: React setter on date locator → {res.get('value')!r}")
            return True
    except Exception as exc:
        if log:
            log(f"SPS: React setter on locator failed: {exc!r}")
    return False


async def _sps_force_custom_date_react_setter_all_contexts(page: Page, value: str, log) -> bool:
    """Bypass React read-only wrappers using the native HTMLInputElement value setter (per frame)."""
    selectors = [
        '[data-testid="customDate_date_input"]',
        '[data-testid*="customDate" i][data-testid*="input" i]',
        'input[placeholder*="MM/DD" i]',
    ]
    for ctx in _contexts(page):
        try:
            res = await ctx.evaluate(_SPS_REACT_SET_INPUT_JS, [selectors, value])
            if isinstance(res, dict) and res.get("ok") and (res.get("value") or "").strip():
                if log:
                    log(f"SPS: applied React/native value setter in a frame → {res.get('value')!r}")
                return True
        except Exception as exc:
            if log:
                log(f"SPS: React setter skipped in one context: {exc!r}")
    return False


def _sps_normalize_date_range_text(s: str) -> str:
    """Collapse whitespace and unify dash variants for comparing SPS custom range strings."""
    t = re.sub(r"\s+", "", (s or "").strip())
    for ch in ("\u2013", "\u2014", "\u2212"):  # en dash, em dash, minus sign
        t = t.replace(ch, "-")
    return t


async def _sps_custom_date_value_acceptable(date_loc, *, expect: str) -> bool:
    try:
        v = (await date_loc.input_value() or "").strip()
    except Exception:
        return False
    if not v or re.match(r"^MM/DD/YYYY", v, re.I):
        return False
    vn = _sps_normalize_date_range_text(v)
    en = _sps_normalize_date_range_text(expect)
    return en in vn or vn == en


async def _sps_keyboard_type_date_range(page: Page, date_loc, value: str, log) -> None:
    await date_loc.click(timeout=10_000, force=True)
    await date_loc.press("Control+A")
    await date_loc.press("Backspace")
    try:
        await date_loc.press_sequentially(value, delay=45)
        if log:
            log("SPS: typed date range with press_sequentially.")
    except Exception as exc:
        if log:
            log(f"SPS: press_sequentially failed ({exc!r}); typing keys slowly…")
        for ch in value:
            await date_loc.press(ch)
            await page.wait_for_timeout(25)


def _sps_parse_month_year_from_text(text: str) -> tuple[int, int] | None:
    m = re.search(
        r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})",
        text or "",
        re.I,
    )
    if not m:
        return None
    try:
        d = datetime.strptime(f"{m.group(1)} 1, {m.group(2)}", "%B %d, %Y")
        return (d.year, d.month)
    except ValueError:
        return None


async def _sps_date_picker_popup_visible(page: Page) -> bool:
    """True when a date-picker layer is visible (React Datepicker, MUI, SPS poppers, etc.)."""
    checks = (
        ".react-datepicker-popper:not([aria-hidden='true'])",
        ".react-datepicker:visible",
        "[class*='react-datepicker']:visible",
        "[role='presentation']:has(.react-datepicker):visible",
        "[role='presentation']:has(table):visible",
        "[class*='MuiPickersPopper']:visible",
        "[class*='MuiPaper-root']:has([role='grid']):visible",
        "[class*='DayPicker']:visible",
        "[class*='calendar-popover' i]:visible",
        "[role='dialog']:has([role='grid']):visible",
        "[role='dialog']:has(table):visible",
    )
    for sel in checks:
        loc = page.locator(sel).first
        try:
            if await loc.count() == 0:
                continue
            if await loc.is_visible():
                return True
        except Exception:
            continue
    return False


async def _sps_calendar_surface(page: Page):
    """Locator for the visible calendar surface (for month nav + day cells)."""
    for sel in (
        ".react-datepicker:visible",
        "[class*='react-datepicker']:visible",
        "[role='presentation']:visible",
        "[class*='MuiPickersPopper']:visible",
        "[role='dialog']:visible",
    ):
        loc = page.locator(sel).first
        try:
            if await loc.count() == 0:
                continue
            if await loc.is_visible():
                return loc
        except Exception:
            continue
    return page.locator("body")


async def _sps_read_calendar_month_year(page: Page) -> tuple[int, int] | None:
    surf = await _sps_calendar_surface(page)
    try:
        if await surf.count() == 0:
            return None
        t = await surf.inner_text(timeout=3_000)
    except Exception:
        return None
    return _sps_parse_month_year_from_text(t or "")


async def _sps_mouse_left_click_box(page: Page, loc, *, x_ratio: float = 0.5) -> None:
    """Real OS-style left click at horizontal position within the element (0=left, 1=right)."""
    await loc.scroll_into_view_if_needed()
    box = await loc.bounding_box()
    if box is None:
        await loc.click(button="left", timeout=12_000)
        return
    x = box["x"] + max(4.0, min(box["width"] - 4.0, box["width"] * x_ratio))
    y = box["y"] + box["height"] / 2
    await page.mouse.move(x, y)
    await page.wait_for_timeout(50)
    await page.mouse.down(button="left")
    await page.wait_for_timeout(30)
    await page.mouse.up(button="left")
    await page.wait_for_timeout(80)


async def _sps_open_custom_date_calendar(page: Page, date_loc, log=None) -> bool:
    await _lift_sps_ui_blockers(page, log)
    await _sps_disable_pointer_blocking_backdrops(page, log)
    try:
        await date_loc.scroll_into_view_if_needed()
    except Exception:
        pass

    # User flow: normal left click on the date field (opens calendar). Try text area then icon area.
    for x_ratio in (0.45, 0.12, 0.88):
        try:
            await _sps_mouse_left_click_box(page, date_loc, x_ratio=x_ratio)
        except Exception:
            try:
                await date_loc.click(button="left", timeout=8_000)
            except Exception:
                await date_loc.click(button="left", timeout=8_000, force=True)
        await page.wait_for_timeout(700)
        if await _sps_date_picker_popup_visible(page):
            if log:
                log(f"SPS: calendar opened (left click on date field, x_ratio={x_ratio}).")
            return True

    for sel in (
        '[data-testid*="customDate"] button',
        '[data-testid*="customDate"] [role="button"]',
        'xpath=//*[@data-testid="customDate_date_input"]/ancestor::div[contains(@class,"sps")][1]//button',
    ):
        btn = page.locator(sel).first
        try:
            if await btn.count() == 0 or not await btn.is_visible():
                continue
            await btn.click(button="left", timeout=5_000)
            await page.wait_for_timeout(700)
            if await _sps_date_picker_popup_visible(page):
                if log:
                    log(f"SPS: calendar opened via related control ({sel!r}).")
                return True
        except Exception:
            continue

    try:
        await date_loc.click(button="left", timeout=8_000, force=True)
        await page.wait_for_timeout(700)
    except Exception:
        pass
    ok = await _sps_date_picker_popup_visible(page)
    if ok and log:
        log("SPS: calendar opened (fallback click).")
    return ok


async def _sps_calendar_navigate_to_month(page: Page, report_day: date, log=None) -> None:
    target = (report_day.year, report_day.month)
    next_sel = [
        ".react-datepicker__navigation--next",
        "[class*='react-datepicker'] [class*='navigation--next' i]",
        "button[aria-label*='Next' i]",
    ]
    prev_sel = [
        ".react-datepicker__navigation--previous",
        "[class*='react-datepicker'] [class*='navigation--previous' i]",
        "button[aria-label*='Previous' i]",
        "button[aria-label*='Prev' i]",
    ]
    for _ in range(40):
        if not await _sps_date_picker_popup_visible(page):
            return
        cur = await _sps_read_calendar_month_year(page)
        if cur == target:
            return
        if cur is None:
            await _click_first_visible(page, next_sel, timeout_ms=2_500)
            await page.wait_for_timeout(400)
            continue
        if cur < target:
            await _click_first_visible(page, next_sel, timeout_ms=2_500)
        else:
            await _click_first_visible(page, prev_sel, timeout_ms=2_500)
        await page.wait_for_timeout(400)
    if log:
        log("SPS: calendar month navigation may not have converged; continuing with visible days.")


async def _sps_calendar_click_day(page: Page, day: int) -> None:
    surf = await _sps_calendar_surface(page)
    cell_groups = (
        surf.locator('[role="gridcell"]').filter(has_text=re.compile(rf"^{day}$")),
        surf.locator("td").filter(has_text=re.compile(rf"^{day}$")),
        surf.locator("button").filter(has_text=re.compile(rf"^{day}$")),
        page.locator(".react-datepicker__day").filter(has_text=re.compile(rf"^{day}$")),
    )
    for cells in cell_groups:
        try:
            n = await cells.count()
            for i in range(min(n, 56)):
                c = cells.nth(i)
                if not await c.is_visible():
                    continue
                try:
                    cls = ((await c.get_attribute("class")) or "").lower()
                    if "outside-month" in cls or "disabled" in cls:
                        continue
                except Exception:
                    pass
                box = await c.bounding_box()
                if box:
                    await page.mouse.click(
                        box["x"] + box["width"] / 2,
                        box["y"] + box["height"] / 2,
                        button="left",
                        delay=40,
                    )
                else:
                    await c.click(button="left", timeout=5_000)
                return
        except Exception:
            continue
    raise RuntimeError(f"SPS: could not left-click calendar day {day}.")


async def _fill_sps_custom_date_range_calendar_or_type(
    page: Page, report_day: date, date_loc, *, log=None
) -> None:
    """Set custom range: React setter on locator → all-frame setter → calendar → fill → keyboard. Verify each tier."""
    date_val = sps_custom_date_range_value(report_day)
    await _lift_sps_ui_blockers(page, log)
    await _sps_disable_pointer_blocking_backdrops(page, log)

    if log:
        log(f"SPS: setting custom date range target {date_val!r}…")

    if await _sps_force_react_value_on_locator(date_loc, date_val, log):
        await page.wait_for_timeout(200)
        if await _sps_custom_date_value_acceptable(date_loc, expect=date_val):
            if log:
                log("SPS: custom date OK after React setter on locator.")
            return

    if await _sps_force_custom_date_react_setter_all_contexts(page, date_val, log):
        await page.wait_for_timeout(200)
        if await _sps_custom_date_value_acceptable(date_loc, expect=date_val):
            if log:
                log("SPS: custom date OK after React setter (frame scan).")
            return

    opened = await _sps_open_custom_date_calendar(page, date_loc, log=log)
    if opened:
        if log:
            log(
                f"SPS: calendar open — picking day {report_day.day} twice for "
                f"{report_day.strftime('%m/%d/%Y')}."
            )
        await _sps_calendar_navigate_to_month(page, report_day, log=log)
        await _sps_calendar_click_day(page, report_day.day)
        await page.wait_for_timeout(220)
        if not await _sps_date_picker_popup_visible(page):
            await _sps_open_custom_date_calendar(page, date_loc, log=log)
            await _sps_calendar_navigate_to_month(page, report_day, log=log)
        await _sps_calendar_click_day(page, report_day.day)
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(200)
        if await _sps_custom_date_value_acceptable(date_loc, expect=date_val):
            if log:
                log("SPS: custom date OK after calendar clicks.")
            return
        if log:
            log("SPS: calendar path ran but value not acceptable; trying fill/keyboard…")

    if log:
        log("SPS: trying Playwright fill/events on custom date field…")
    try:
        await _set_sps_text_input_value(date_loc, date_val, field_label="custom date range")
    except Exception as exc:
        if log:
            log(f"SPS: fill/events path error: {exc!r}")
    if await _sps_custom_date_value_acceptable(date_loc, expect=date_val):
        if log:
            log("SPS: custom date OK after fill/events.")
        return

    if log:
        log("SPS: trying keyboard entry for custom date range…")
    await _sps_keyboard_type_date_range(page, date_loc, date_val, log)
    await page.wait_for_timeout(220)
    if await _sps_custom_date_value_acceptable(date_loc, expect=date_val):
        if log:
            log("SPS: custom date OK after keyboard.")
        return

    snap = await _save_sps_debug_screenshot(page, "debug_sps_custom_date_failed")
    raise RuntimeError(
        "SPS: custom date range is still not set after React setter, calendar, fill, and keyboard. "
        f"Expected substring resembling {date_val!r}. Screenshot: {snap}"
    )


async def _sps_click_invoice_doc_type_option(page: Page, *, log=None) -> bool:
    """Click an Invoice row in an open MUI/React listbox (main page and iframes)."""
    for ctx in _contexts(page):
        try:
            o = ctx.get_by_role("option", name=re.compile(r"^\s*invoice\s*$", re.I)).first
            if await o.count() and await o.is_visible():
                try:
                    await o.click(timeout=5_000)
                except Exception:
                    await o.click(timeout=5_000, force=True)
                if log:
                    log("SPS: clicked Document Type option Invoice (accessible name).")
                return True
        except Exception:
            pass
        try:
            opts = ctx.locator(
                "[role='listbox'] [role='option'], [role='menu'] [role='option'], [role='option']"
            )
            n = await opts.count()
            for i in range(min(n, 80)):
                oi = opts.nth(i)
                try:
                    if not await oi.is_visible():
                        continue
                    txt = (await oi.inner_text() or "").strip()
                except Exception:
                    continue
                tl = re.sub(r"\s+", " ", txt.lower())
                if tl == "invoice" or tl.endswith(" invoice"):
                    if "non-invoice" in tl or tl.startswith("non-"):
                        continue
                    try:
                        await oi.click(timeout=5_000)
                    except Exception:
                        await oi.click(timeout=5_000, force=True)
                    if log:
                        log(f"SPS: clicked Document Type list option {txt!r}.")
                    return True
        except Exception:
            continue
    return False


async def _sps_select_document_type_invoice(page: Page, doc, *, log=None) -> None:
    """
    Document Type is a combobox: MUI/React often ignores fill() and drops portaled listboxes
    outside the main frame — type like a user, then click [role=option] in every frame.
    """
    await _sps_light_transactions_form_prep(page, log)
    await doc.wait_for(state="attached", timeout=15_000)
    await _sps_disable_pointer_blocking_backdrops(page, log)
    try:
        await doc.scroll_into_view_if_needed()
    except Exception:
        pass
    try:
        await _sps_mouse_left_click_box(page, doc, x_ratio=0.5)
    except Exception:
        try:
            await doc.click(button="left", timeout=10_000, force=True)
        except Exception:
            pass
    await page.wait_for_timeout(90)
    try:
        await doc.press("Control+A")
    except Exception:
        pass
    try:
        await doc.press("Backspace")
    except Exception:
        pass
    try:
        await doc.fill("")
    except Exception:
        pass
    await page.wait_for_timeout(70)
    try:
        await doc.press_sequentially("Invoice", delay=22)
    except Exception:
        for ch in "Invoice":
            try:
                await doc.press(ch)
                await page.wait_for_timeout(22)
            except Exception:
                pass
    await page.wait_for_timeout(250)
    if log:
        log("SPS: Document Type — typed Invoice; choosing matching list row…")

    if await _sps_click_invoice_doc_type_option(page, log=log):
        await page.wait_for_timeout(100)
        return

    if log:
        log("SPS: Document Type — no Invoice row yet; ArrowDown + Enter…")
    try:
        await doc.press("ArrowDown")
        await page.wait_for_timeout(90)
        await doc.press("Enter")
    except Exception:
        try:
            await doc.press("Enter")
        except Exception:
            pass
    await page.wait_for_timeout(160)
    if await _sps_click_invoice_doc_type_option(page, log=log):
        return

    if log:
        log("SPS: Document Type — retrying fill + option click…")
    try:
        await doc.click(timeout=8_000, force=True)
        await doc.fill("Invoice")
    except Exception:
        pass
    await page.wait_for_timeout(240)
    if await _sps_click_invoice_doc_type_option(page, log=log):
        return

    snap = await _save_sps_debug_screenshot(page, "debug_sps_doc_type_invoice_failed")
    raise RuntimeError(
        "SPS: could not select Document Type Invoice (typed and searched all frames for list options). "
        f"Screenshot: {snap}"
    )


async def _fill_sps_advanced_search_date_and_invoice(
    page: Page, report_day: date, *, step_timeout_ms: int, log=None
) -> None:
    await _lift_sps_ui_blockers(page, log)
    date_loc = await _locate_sps_custom_date_input(page)
    if date_loc is None:
        raise RuntimeError(
            "SPS: custom date input not found on the main page or in iframes after Advanced Search opened."
        )
    await date_loc.wait_for(state="attached", timeout=step_timeout_ms)
    if log:
        log("SPS: setting Custom Date Range (calendar or text)…")
    await _fill_sps_custom_date_range_calendar_or_type(page, report_day, date_loc, log=log)
    try:
        await date_loc.press("Tab")
    except Exception:
        pass
    await page.wait_for_timeout(120)
    await _sps_light_transactions_form_prep(page, log)

    doc = await _locate_sps_doc_type_input(page)
    if doc is None:
        raise RuntimeError(
            "SPS: Document Type multiselect not found on the main page or in iframes."
        )
    await doc.wait_for(state="attached", timeout=step_timeout_ms)
    if log:
        log("SPS: setting Document Type to Invoice…")
    await _sps_select_document_type_invoice(page, doc, log=log)
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass
    await page.wait_for_timeout(90)


async def _click_sps_advanced_search_run(
    page: Page, *, step_timeout_ms: int, log=None
) -> None:
    """
    Run criteria search. Uses all frames (toolbar may be in an iframe). Avoids full
    _lift_sps_ui_blockers here — its repeated Escape can disrupt the criteria strip.
    """
    await _sps_disable_pointer_blocking_backdrops(page, log)
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass
    await page.wait_for_timeout(90)
    deadline = time.monotonic() + step_timeout_ms / 1000.0
    while time.monotonic() < deadline:
        btn = await _locate_sps_bottom_search_button(page)
        if btn is not None:
            try:
                await btn.scroll_into_view_if_needed()
            except Exception:
                pass
            try:
                await btn.click(timeout=12_000, force=True)
            except Exception:
                await btn.click(timeout=12_000, force=True)
            if log:
                log("SPS: running Advanced Search…")
            return
        await asyncio.sleep(0.22)
    snap = await _save_sps_debug_screenshot(page, "debug_sps_search_button_missing")
    raise RuntimeError(
        "SPS: bottom Search never became targetable (main page and iframes). "
        f"Screenshot: {snap}"
    )


async def _click_sps_bulk_download_cloud(page: Page) -> None:
    await _lift_sps_ui_blockers(page, None)

    for ctx in _contexts(page):
        icon = ctx.locator("i.sps-icon.sps-icon-download-cloud").first
        try:
            if await icon.count() == 0:
                continue
            if not await _sps_locator_physically_usable(icon):
                continue
        except Exception:
            continue
        parent = icon.locator(
            "xpath=ancestor-or-self::*[self::button or self::a or @role='button'][1]"
        ).first
        target = parent if await parent.count() else icon
        if await _sps_try_bulk_download_target(page, target):
            return

    async def _try_viewing_toolbar_by_aria(ctx) -> bool:
        """Toolbar under 'Viewing 1 - 4 of 4': prefer control whose name mentions download/export."""
        v = ctx.get_by_text(
            re.compile(r"Viewing\s+\d+\s*-\s*\d+\s+of\s+\d+", re.I)
        ).first
        if await v.count() == 0:
            return False
        try:
            await v.scroll_into_view_if_needed(timeout=8_000)
        except Exception:
            pass
        scope = ctx.locator("div, section, main, article, header, form").filter(has=v).first
        if await scope.count() == 0:
            scope = ctx.locator("body")
        actions = scope.locator(
            "button:has(svg), [role='button']:has(svg), a:has(svg), "
            "button:has(i), [role='button']:has(i), a:has(i)"
        )
        try:
            n = await actions.count()
        except Exception:
            n = 0
        for i in range(min(n, 14)):
            el = actions.nth(i)
            try:
                if not await el.is_visible():
                    continue
            except Exception:
                continue
            try:
                parts: list[str] = []
                for attr in ("aria-label", "title", "data-tooltip"):
                    try:
                        val = await el.get_attribute(attr)
                        if val:
                            parts.append(val)
                    except Exception:
                        pass
                aria = " ".join(parts)
            except Exception:
                aria = ""
            if re.search(
                r"bulk|download|export|csv|cloud", aria, re.I
            ) and not re.search(r"upload|import", aria, re.I):
                if await _sps_try_bulk_download_target(page, el):
                    return True
        for idx in (3, 2, 4, 5):
            if idx < n:
                el = actions.nth(idx)
                try:
                    if await el.is_visible():
                        if await _sps_try_bulk_download_target(page, el):
                            return True
                except Exception:
                    continue
        return False

    for ctx in _contexts(page):
        if await _try_viewing_toolbar_by_aria(ctx):
            return

    for ctx in _contexts(page):
        try:
            b = ctx.get_by_role(
                "button",
                name=re.compile(r"bulk|download|export|csv", re.I),
            ).first
            if await b.count() and await b.is_visible():
                if not re.search(
                    r"upload|import",
                    (await b.get_attribute("aria-label") or "")
                    + (await b.get_attribute("title") or ""),
                    re.I,
                ):
                    if await _sps_try_bulk_download_target(page, b):
                        return
        except Exception:
            pass

    icon_selectors = (
        "i.sps-icon.sps-icon-download-cloud",
        "i.sps-icon-download-cloud",
        "svg.sps-icon-download-cloud",
        "[class*='sps-icon-download-cloud' i]",
        "[class*='download-cloud' i]",
        "[class*='icon-download-cloud' i]",
        "svg[class*='DownloadCloud' i]",
        "svg[class*='downloadCloud' i]",
    )
    for ctx in _contexts(page):
        for isel in icon_selectors:
            icon = ctx.locator(isel).first
            try:
                if await icon.count() == 0 or not await icon.is_visible():
                    continue
            except Exception:
                continue
            for xp in (
                "xpath=ancestor-or-self::*[self::button or self::a or @role='button'][1]",
                "xpath=ancestor::*[@role='button'][1]",
                "xpath=ancestor::button[1]",
                "xpath=ancestor::a[1]",
            ):
                parent = icon.locator(xp).first
                try:
                    if await parent.count() and await parent.is_visible():
                        if await _sps_try_bulk_download_target(page, parent):
                            return
                except Exception:
                    continue
            try:
                if await _sps_try_bulk_download_target(page, icon):
                    return
            except Exception:
                pass

    sel_str = (
        "button:has(i.sps-icon-download-cloud), "
        "[role='button']:has(i.sps-icon-download-cloud), "
        "a:has(i.sps-icon-download-cloud), "
        "button:has(svg.sps-icon-download-cloud), "
        "button:has(svg[class*='download-cloud' i]), "
        "[role='button']:has(svg[class*='cloud' i]), "
        "div[role='button']:has(svg[class*='cloud' i]), "
        "a:has(svg[class*='download' i]), "
        "[data-testid*='bulkDownload' i], "
        "[data-testid*='BulkDownload' i], "
        "[data-testid*='downloadCloud' i], "
        "[aria-label*='Bulk' i][aria-label*='Download' i], "
        "[aria-label*='Download' i][aria-label*='Cloud' i]"
    )
    best_y = -1.0
    chosen = None
    for ctx in _contexts(page):
        candidates = ctx.locator(sel_str)
        try:
            n = await candidates.count()
        except Exception:
            continue
        for i in range(n):
            el = candidates.nth(i)
            try:
                if not await el.is_visible():
                    continue
                box = await el.bounding_box()
                if box is None:
                    continue
                if box["y"] > best_y:
                    best_y = box["y"]
                    chosen = el
            except Exception:
                continue
    if chosen is None:
        for ctx in _contexts(page):
            try:
                icon = ctx.locator("i.sps-icon.sps-icon-download-cloud, i.sps-icon-download-cloud").first
                if await icon.count() == 0:
                    continue
                if not await icon.is_visible():
                    continue
                wrap = icon.locator("xpath=ancestor::button[1]").first
                if await wrap.count() and await wrap.is_visible():
                    chosen = wrap
                    break
                chosen = icon
                break
            except Exception:
                continue
    if chosen is None:
        raise RuntimeError(
            "SPS: could not find a visible bulk download control (cloud icon) on the page or in iframes."
        )
    if await _sps_try_bulk_download_target(page, chosen):
        return
    for ctx in _contexts(page):
        try:
            all_icons = ctx.locator("i.sps-icon.sps-icon-download-cloud, i.sps-icon-download-cloud")
            n = await all_icons.count()
        except Exception:
            continue
        for i in range(min(n, 8)):
            ic = all_icons.nth(i)
            try:
                if not await ic.is_visible():
                    continue
            except Exception:
                continue
            if await _sps_try_bulk_download_target(page, ic):
                return
    snap = await _save_sps_debug_screenshot(page, "debug_sps_bulk_download_no_popup")
    raise RuntimeError(
        "SPS: bulk download cloud was found but no download menu / combine-CSV UI appeared "
        f"(UI may use different markup). Screenshot: {snap}"
    )


async def _maybe_confirm_combine_csv_modal(page: Page, log) -> None:
    """
    Select 'combine into one CSV' and confirm. Do not call _lift_sps_ui_blockers here — its Escape
    keys dismiss this dialog immediately after the bulk-download menu opens.
    """
    await _sps_disable_pointer_blocking_backdrops(page, log)
    await _sps_wait_loading_veils_gone(page, timeout_ms=2_500)

    combine_re = re.compile(r"Combine\s+documents\s+into\s+one\s+CSV", re.I)
    deadline = time.monotonic() + 20.0
    dialog_ctx = None
    while time.monotonic() < deadline:
        for ctx in _contexts(page):
            hint = ctx.get_by_text(combine_re).first
            try:
                if await hint.count() == 0 or not await hint.is_visible():
                    continue
            except Exception:
                continue
            await page.wait_for_timeout(400)
            try:
                if await hint.is_visible():
                    dialog_ctx = ctx
                    break
            except Exception:
                continue
        if dialog_ctx is not None:
            break
        await asyncio.sleep(0.12)
    if dialog_ctx is None:
        return

    if log:
        log("SPS: combine-documents modal — selecting one CSV file…")

    container = None
    for cand in (
        dialog_ctx.get_by_role("dialog").filter(has_text=combine_re).first,
        dialog_ctx.locator(".sps-modal:visible").filter(has_text=combine_re).first,
        dialog_ctx.locator("[class*='modal' i]:visible").filter(has_text=combine_re).first,
    ):
        try:
            if await cand.count() and await cand.is_visible():
                container = cand
                break
        except Exception:
            continue
    if container is None:
        container = dialog_ctx.locator("body")

    clicked = False
    for sel in (
        "input[type='radio']",
        "input[type='checkbox']",
        "[role='radio']",
        "[role='checkbox']",
        ".sps-checkable input",
        ".sps-checkable__input",
    ):
        try:
            inp = container.locator(sel).first
            if await inp.count() == 0:
                continue
            if not await inp.is_visible():
                continue
            await inp.scroll_into_view_if_needed()
            try:
                await inp.click(timeout=5_000, force=True)
            except Exception:
                await inp.click(timeout=5_000, force=True)
            clicked = True
            break
        except Exception:
            continue

    if not clicked:
        lbl = container.locator("label.sps-checkable__label").filter(has_text=combine_re).first
        try:
            if await lbl.count() and await lbl.is_visible():
                await lbl.scroll_into_view_if_needed()
                try:
                    await lbl.click(timeout=5_000, force=True)
                except Exception:
                    await lbl.click(timeout=5_000, force=True)
                clicked = True
        except Exception:
            pass

    await page.wait_for_timeout(350)

    ok_btn = None
    for ctx in _contexts(page):
        ob = ctx.locator('[data-testid="modalOkBtn"]').first
        try:
            if await ob.count() and await ob.is_visible():
                ok_btn = ob
                break
        except Exception:
            continue
    if ok_btn is None:
        return
    await ok_btn.wait_for(state="visible", timeout=12_000)
    try:
        await ok_btn.click(timeout=8_000)
    except Exception:
        await ok_btn.click(timeout=8_000, force=True)


async def _sps_dismiss_success_download_toast(page: Page, log=None) -> None:
    """Close 'Success / Your download is complete' toasts so the header notifications control is clickable."""
    for ctx in _contexts(page):
        try:
            line = ctx.get_by_text(re.compile(r"download\s+is\s+complete", re.I)).first
            if await line.count() == 0:
                line = ctx.get_by_text(re.compile(r"\bSuccess\b", re.I)).first
            if await line.count() == 0:
                continue
            if not await line.is_visible():
                continue
            panel = line.locator(
                "xpath=ancestor-or-self::*[self::div or self::aside or self::section][position()<=10]"
            ).first
            for closer_sel in (
                "button[aria-label*='close' i]",
                "[role='button'][aria-label*='close' i]",
                "button:has-text('×')",
                "button.sps-toast__close",
                "[class*='toast' i] button",
            ):
                try:
                    btn = panel.locator(closer_sel).first
                    if await btn.count() and await btn.is_visible():
                        await btn.click(timeout=3_000, force=True)
                        if log:
                            log("SPS: dismissed success / download-complete toast.")
                        await page.wait_for_timeout(400)
                        return
                except Exception:
                    continue
        except Exception:
            continue


async def _click_notifications_list_and_first_download_cloud(page: Page, log) -> None:
    """
    Open the header notifications / downloads tray (list icon, often with a numeric badge),
    then trigger download on the first row that shows a cloud icon.
    """
    await _sps_dismiss_success_download_toast(page, log)
    await _sps_disable_pointer_blocking_backdrops(page, log)
    await _sps_wait_loading_veils_gone(page, timeout_ms=3_000)
    if log:
        log("SPS: opening downloads / notifications list (top bar)…")

    list_icon_sel = "i.sps-icon.sps-icon-list, i.sps-icon-list"
    cloud_icon_sel = "i.sps-icon.sps-icon-download-cloud, i.sps-icon-download-cloud"

    target = None
    for ctx in _contexts(page):
        candidates = (
            ctx.locator("button, [role='button'], a, div[role='button']").filter(
                has=ctx.locator(list_icon_sel)
            ).filter(
                has=ctx.locator(
                    "[class*='badge' i], [class*='count' i], [class*='notification-count' i], "
                    "span[class*='sps' i]"
                )
            ),
            ctx.locator("header button, header [role='button'], nav button").filter(
                has=ctx.locator(list_icon_sel)
            ),
            ctx.locator("button:has(i.sps-icon.sps-icon-list), [role='button']:has(i.sps-icon.sps-icon-list)"),
            ctx.locator("button:has(i.sps-icon-list), [role='button']:has(i.sps-icon-list)"),
            ctx.locator("a:has(i.sps-icon.sps-icon-list), a:has(i.sps-icon-list)"),
        )
        for group in candidates:
            try:
                if await group.count() == 0:
                    continue
                el = group.first
                if await el.is_visible():
                    target = el
                    break
            except Exception:
                continue
        if target is not None:
            break
    if target is None:
        for ctx in _contexts(page):
            try:
                ic = ctx.locator(list_icon_sel).first
                if await ic.count() == 0 or not await ic.is_visible():
                    continue
                for xp in (
                    "xpath=ancestor-or-self::button[1]",
                    "xpath=ancestor::*[@role='button'][1]",
                    "xpath=ancestor::a[1]",
                    "xpath=ancestor::div[@role='button'][1]",
                ):
                    wrap = ic.locator(xp).first
                    if await wrap.count() and await wrap.is_visible():
                        target = wrap
                        break
                if target is None:
                    target = ic
                break
            except Exception:
                continue

    if target is None:
        raise RuntimeError(
            "SPS: notifications / downloads list control not found (list icon + badge in header)."
        )
    await target.wait_for(state="visible", timeout=30_000)
    try:
        await target.scroll_into_view_if_needed()
    except Exception:
        pass
    await _sps_click_through_wall_for_locator(page, target)
    await page.wait_for_timeout(900)

    row = None
    for ctx in _contexts(page):
        try:
            named = ctx.locator("div, li, tr").filter(has_text=re.compile(r"Document\s+Download", re.I))
            nn = await named.count()
            for i in range(min(nn, 20)):
                r0 = named.nth(i)
                try:
                    if await r0.locator(cloud_icon_sel).count() == 0:
                        continue
                    if await r0.is_visible():
                        row = r0
                        break
                except Exception:
                    continue
            if row is not None:
                break
        except Exception:
            pass
        tray = ctx.locator(
            "[role='dialog'], [role='menu'], [class*='popover' i], [class*='MuiPaper-root' i], "
            "[class*='drawer' i], [class*='flyout' i], [class*='notification' i], [class*='dropdown' i]"
        ).first
        try:
            if await tray.count() and await tray.is_visible():
                cells = tray.locator("li, tr, div[role='row'], div[role='menuitem'], div")
                nn = await cells.count()
                for i in range(min(nn, 18)):
                    r = cells.nth(i)
                    try:
                        if await r.locator(cloud_icon_sel).count() == 0:
                            continue
                        if await r.is_visible():
                            row = r
                            break
                    except Exception:
                        continue
        except Exception:
            pass
        if row is not None:
            break

    if row is None:
        for ctx in _contexts(page):
            try:
                roots = ctx.locator("li, tr, div[role='menuitem']")
                n = await roots.count()
                for i in range(min(n, 25)):
                    r = roots.nth(i)
                    try:
                        if await r.locator(cloud_icon_sel).count() == 0:
                            continue
                        if await r.is_visible():
                            row = r
                            break
                    except Exception:
                        continue
            except Exception:
                continue
            if row is not None:
                break

    if row is None:
        raise RuntimeError(
            "SPS: notification tray opened but no row with cloud download icon was found."
        )
    await row.wait_for(state="visible", timeout=120_000)
    cloud = row.locator(cloud_icon_sel).first
    wrap = cloud.locator("xpath=ancestor::button[1]").first
    click_loc = wrap if await wrap.count() else cloud
    await _sps_click_through_wall_for_locator(page, click_loc)


async def _wait_tractor_download_file_ready(
    path: Path, *, log, max_wait_s: float = 45.0, stable_reads: int = 4, poll_s: float = 0.25
) -> None:
    """After ``save_as``, wait until the file has a stable non-zero size (disk / AV)."""
    deadline = time.monotonic() + max_wait_s
    last = -1
    same = 0
    while time.monotonic() < deadline:
        try:
            sz = path.stat().st_size
        except OSError:
            await asyncio.sleep(poll_s)
            continue
        if sz > 0 and sz == last:
            same += 1
            if same >= stable_reads:
                if log:
                    log(f"SPS: download file ready ({sz} bytes, stable).")
                return
        else:
            same = 0
            last = sz
        await asyncio.sleep(poll_s)
    if log:
        log(f"SPS: download file did not reach a stable size within {max_wait_s}s; continuing anyway.")


async def _download_tractor_bulk_csv(
    page: Page,
    *,
    download_dir: Path,
    report_day: date,
    download_timeout_ms: int,
    log,
) -> Path:
    # Avoid Escape here — it can dismiss the combine-CSV dialog opened right after bulk download.
    await _sps_disable_pointer_blocking_backdrops(page, log)
    await _sps_wait_loading_veils_gone(page, timeout_ms=3_000)
    download_dir.mkdir(parents=True, exist_ok=True)
    phase1 = min(90_000, max(30_000, download_timeout_ms // 8))
    try:
        async with page.expect_download(timeout=phase1) as di:
            await _click_sps_bulk_download_cloud(page)
            await _maybe_confirm_combine_csv_modal(page, log)
        dl: Download = await di.value
    except PlaywrightTimeout:
        if log:
            log(
                f"SPS: no browser download within {phase1 // 1000}s "
                "(queued export); completing from notifications list…"
            )
        rest = max(120_000, download_timeout_ms - phase1)
        async with page.expect_download(timeout=rest) as di2:
            await _click_notifications_list_and_first_download_cloud(page, log)
        dl = await di2.value

    dest = download_dir / f"_sps_tractor_raw_{report_day.isoformat()}.csv"
    await dl.save_as(str(dest))
    # Notifications-path downloads can still be flushing; give the OS time before teardown.
    await asyncio.sleep(2.5)
    await _wait_tractor_download_file_ready(dest, log=log)
    return dest


async def _click_select_all_transactions(page: Page, log=None) -> None:
    await _lift_sps_ui_blockers(page, log)
    selectors = (
        "thead input[type='checkbox']",
        "thead th input[type='checkbox']",
        "table thead input[type='checkbox']",
        "th:first-of-type input[type='checkbox']",
        "[data-testid*='selectAll' i]",
        "[data-testid*='SelectAll' i]",
        "[aria-label*='Select all' i]",
        "[title*='Select all' i]",
        "thead .sps-checkable__label",
        "table thead .sps-checkable__label",
        "thead label.sps-checkable__label",
        "th .sps-checkable__label",
        "thead .sps-checkable input[type='checkbox']",
        "thead th .MuiCheckbox-root input[type='checkbox']",
        "thead .MuiCheckbox-root input[type='checkbox']",
        "table thead label:has(input[type='checkbox'])",
        "[role='grid'] [role='row']:first-child [role='columnheader'] input[type='checkbox']",
        "[role='grid'] [role='row']:first-child input[type='checkbox']",
        "[role='treegrid'] [role='row']:first-child [type='checkbox']",
    )
    deadline = time.monotonic() + 28.0
    while time.monotonic() < deadline:
        for ctx in _contexts(page):
            for sel in selectors:
                loc = ctx.locator(sel).first
                try:
                    if await loc.count() == 0:
                        continue
                    if not await _sps_locator_physically_usable(loc):
                        continue
                    await loc.scroll_into_view_if_needed()
                    try:
                        await loc.click(timeout=6_000)
                    except Exception:
                        await loc.click(timeout=6_000, force=True)
                    await page.wait_for_timeout(250)
                    if log:
                        log(f"SPS: clicked header select-all ({sel!r}).")
                    return
                except Exception:
                    continue
            try:
                cb = ctx.get_by_role(
                    "checkbox", name=re.compile(r"select\s*all", re.I)
                ).first
                if await cb.count() and await _sps_locator_physically_usable(cb):
                    await cb.scroll_into_view_if_needed()
                    try:
                        await cb.click(timeout=6_000)
                    except Exception:
                        await cb.click(timeout=6_000, force=True)
                    await page.wait_for_timeout(250)
                    if log:
                        log("SPS: clicked select-all (checkbox role + name).")
                    return
            except Exception:
                pass
        await asyncio.sleep(0.35)
    snap = await _save_sps_debug_screenshot(page, "debug_sps_select_all_missing")
    raise RuntimeError(
        "SPS: could not find the header 'select all' checkbox / label (tried page + iframes, "
        f"table and grid patterns). Screenshot: {snap}"
    )


async def run_tractor_sps_search_and_download_csv(
    page: Page,
    *,
    report_day: date,
    download_dir: Path,
    nav_timeout_ms: int,
    download_timeout_ms: int,
    log,
) -> Path | None:
    """
    Advanced Search must already be open. Applies previous-business-day range + Invoice, runs search,
    selects all when results > 0, downloads combined CSV (modal when needed), saves to download_dir.
    Returns path to the saved temp CSV, or None when Matching Results is 0.
    """
    step_timeout = min(180_000, max(45_000, nav_timeout_ms))
    await _wait_sps_advanced_search_actionable(page, timeout_ms=step_timeout, log=log)
    if log:
        log(
            f"SPS: Advanced Search — custom date range {sps_custom_date_range_value(report_day)} "
            f"(report day {report_day.isoformat()}), Document Type Invoice…"
        )
    await _fill_sps_advanced_search_date_and_invoice(
        page, report_day, step_timeout_ms=step_timeout, log=log
    )

    await _click_sps_advanced_search_run(page, step_timeout_ms=step_timeout, log=log)

    results_timeout = min(120_000, max(60_000, nav_timeout_ms // 2))
    n = await _sps_wait_stable_matching_results(
        page, timeout_ms=results_timeout, log=log
    )
    if log:
        log(f"SPS: Matching Results: {n}.")
    if n == 0:
        if log:
            log("SPS: no invoices for this report day — skipping download.")
        return None

    await _click_select_all_transactions(page, log=log)
    if log:
        log("SPS: bulk download (cloud)…")
    raw_path = await _download_tractor_bulk_csv(
        page,
        download_dir=download_dir,
        report_day=report_day,
        download_timeout_ms=download_timeout_ms,
        log=log,
    )
    return raw_path


async def run_sps_tractor_transactions_and_advanced_search(
    context: BrowserContext,
    *,
    report_day: date,
    download_dir: Path,
    nav_timeout_ms: int = 120_000,
    download_timeout_ms: int = 600_000,
    log,
) -> Path | None:
    """
    New tab: SPS login → transactions list → Advanced Search → previous business day + Invoice →
    Search → select all (if any rows) → CSV download → temp file under ``download_dir``.
    Returns ``None`` when there are zero matching results.
    """
    download_dir = Path(download_dir).resolve()
    page = await context.new_page()
    try:
        log("SPS Commerce: logging in and opening transactions…")
        await login_sps_and_open_transactions(
            page,
            nav_timeout_ms=nav_timeout_ms,
            auth_wait_ms=min(300_000, max(nav_timeout_ms, 180_000)),
            log=log,
        )
        await _raise_if_cookie_or_auth_wall(page)
        log("SPS Commerce: opening Advanced Search…")
        await open_advanced_search_panel(
            page, step_timeout_ms=min(180_000, nav_timeout_ms), log=log
        )
        log("SPS Commerce: running Tractor Supply invoice search and CSV export…")
        return await run_tractor_sps_search_and_download_csv(
            page,
            report_day=report_day,
            download_dir=download_dir,
            nav_timeout_ms=nav_timeout_ms,
            download_timeout_ms=download_timeout_ms,
            log=log,
        )
    finally:
        await asyncio.sleep(2.0)
        await page.close()
