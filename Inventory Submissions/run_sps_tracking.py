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
from automation.config import load_sps_settings


CSV_PATH = Path(
    r"\\rygarcorp.com\shares\Cornerstone\Dot Com Packing Slips\zzz - Worldship Shipment Files\Export Info\UPS_CSV_EXPORT.csv"
)
DASHBOARD_URL = "https://commerce.spscommerce.com/fulfillment/dashboard/"
TRANSACTIONS_LIST_URL = "https://commerce.spscommerce.com/fulfillment/transactions/list/"
_HERE = Path(__file__).resolve().parent
DEFAULT_STORAGE_STATE = _HERE / "sps_playwright_storage.json"
# Optional: SKU -> unit weight (lb) for Grainger ASN gross weight. Override with SPS_SKU_WEIGHTS_CSV.
_DEFAULT_SKU_WEIGHTS_CSV_NAMES = (
    "Weights mapping.csv",
    "weights_mapping.csv",
    "SKU_weights.csv",
    "sku_weights.csv",
)


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
        out[po] = tracking  # last row wins for duplicate POs in the sheet
    return out


def resolve_sku_weights_csv_path() -> Path | None:
    """CSV with SKU (or item) and weight in lb per unit. Env SPS_SKU_WEIGHTS_CSV overrides defaults."""
    env = (os.environ.get("SPS_SKU_WEIGHTS_CSV") or "").strip()
    if env:
        p = Path(env)
        return p if p.is_file() else None
    for name in _DEFAULT_SKU_WEIGHTS_CSV_NAMES:
        p = _HERE / name
        if p.is_file():
            return p
    return None


def load_sku_weight_map(csv_path: Path) -> dict[str, float]:
    """
    Load SKU -> pounds per unit. Flexible headers (SKU/Item/Part and Weight/Lbs/LB).
    Keys stored UPPER for case-insensitive lookup.
    """
    if not csv_path.is_file():
        return {}

    rows: list[list[str]] | None = None
    for enc in ("utf-8-sig", "latin1"):
        try:
            with csv_path.open("r", newline="", encoding=enc) as f:
                sample = f.read(4096)
                f.seek(0)
                try:
                    dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
                except Exception:
                    dialect = csv.excel
                reader = csv.reader(f, dialect)
                rows = list(reader)
            break
        except UnicodeDecodeError:
            continue

    if not rows:
        return {}

    def _norm_cell(s: str) -> str:
        return re.sub(r"\s+", "", (s or "").strip().lower())

    header = [_norm_cell(c) for c in rows[0]]
    sku_idx: int | None = None
    wt_idx: int | None = None

    def _pick_indices(fieldnames: list[str]) -> tuple[int | None, int | None]:
        si: int | None = None
        wi: int | None = None
        for i, cn in enumerate(fieldnames):
            if si is None and re.search(
                r"^(sku|item|part|vendor|buyer|style|product|mph|mfg|model)", cn, re.I
            ):
                si = i
            if wi is None and re.search(r"(weight|lb|lbs|pound|shipwt|ship\s*wt|gross)", cn, re.I):
                wi = i
        return si, wi

    sku_idx, wt_idx = _pick_indices(header)
    data_start = 1
    if sku_idx is None or wt_idx is None:
        # No header row detected — assume col0 = SKU, col1 = weight.
        sku_idx, wt_idx = 0, 1
        data_start = 0

    out: dict[str, float] = {}
    for row in rows[data_start:]:
        if len(row) <= max(sku_idx or 0, wt_idx or 0):
            continue
        sku_raw = (row[sku_idx] if sku_idx is not None else "").strip()
        wt_raw = (row[wt_idx] if wt_idx is not None else "").strip()
        if not sku_raw or not wt_raw:
            continue
        m = re.search(r"(\d+(?:\.\d+)?)", wt_raw.replace(",", ""))
        if not m:
            continue
        try:
            w = float(m.group(1))
        except ValueError:
            continue
        if w <= 0:
            continue
        key = sku_raw.upper()
        out[key] = w
    return out


def _grainger_pick_qty_near_sku(page: Page, body_text: str, sku: str) -> int:
    """Best-effort ordered qty: labels after SKU on page, then table row with SKU, else 1."""
    msku = re.search(rf"(?i){re.escape(sku)}", body_text)
    window = body_text[msku.start() : msku.start() + 1000] if msku else body_text[:2500]
    for pat in (
        r"(?i)ordered[^\d]{0,60}(\d{1,5})\b",
        r"(?i)order\s*qty[^\d]{0,40}(\d{1,5})\b",
        r"(?i)units?\s*ordered[^\d]{0,40}(\d{1,5})\b",
        r"(?i)qty\s*ordered[^\d]{0,40}(\d{1,5})\b",
    ):
        m = re.search(pat, window)
        if m:
            try:
                q = int(m.group(1))
                if 1 <= q <= 50_000:
                    return q
            except Exception:
                continue
    # Table row containing SKU: use last plausible integer in that row (often qty column).
    for ctx in _contexts(page):
        try:
            rows = ctx.locator("tr")
            for i in range(min(rows.count(), 120)):
                row = rows.nth(i)
                try:
                    rt = row.inner_text()
                except Exception:
                    continue
                if not re.search(rf"(?i)\b{re.escape(sku)}\b", rt):
                    continue
                nums: list[int] = []
                for g in re.findall(r"\b(\d{1,5})\b", rt):
                    try:
                        n = int(g)
                    except ValueError:
                        continue
                    if 1 <= n <= 50_000:
                        nums.append(n)
                if nums:
                    return nums[-1]
        except Exception:
            continue
    return 1


def _grainger_find_mapped_sku_qty(page: Page, weight_by_sku: dict[str, float]) -> tuple[str, int] | None:
    if not weight_by_sku:
        return None
    try:
        text = page.inner_text("body")
    except Exception:
        text = ""
    for sku_key in sorted(weight_by_sku.keys(), key=len, reverse=True):
        if not sku_key:
            continue
        if not re.search(rf"(?i)(?<![A-Z0-9]){re.escape(sku_key)}(?![A-Z0-9])", text):
            continue
        qty = _grainger_pick_qty_near_sku(page, text, sku_key)
        return sku_key, qty
    return None


def _grainger_gross_weight_lbs(page: Page, weights_csv: Path | None) -> str:
    """Gross ship weight (lb): SKU weights map × line qty when possible."""
    if not weights_csv:
        return _estimate_gross_weight(page)
    wb = load_sku_weight_map(weights_csv)
    if not wb:
        print(f"WARN: SKU weights file empty or unreadable: {weights_csv}")
        return _estimate_gross_weight(page)
    hit = _grainger_find_mapped_sku_qty(page, wb)
    if not hit:
        print("WARN: No mapped SKU found on ASN page; gross weight uses numeric fallback.")
        return _estimate_gross_weight(page)
    sku_key, qty = hit
    unit = wb.get(sku_key.upper()) or wb.get(sku_key)
    if unit is None or unit <= 0:
        return _estimate_gross_weight(page)
    total = max(1, int(round(float(unit) * max(1, qty))))
    print(f"Grainger ASN: gross weight {total} lb (SKU {sku_key!r} × {qty} @ {unit} lb/unit from weights map).")
    return str(total)


def _fill_grainger_asn_tracking(page: Page, tracking: str) -> None:
    """Fill carrier tracking on Grainger ASN (header, Order/pack lines, then BOL if needed)."""
    tracking_filled = False
    for ctx in _contexts(page):
        inputs = ctx.locator(
            "input[data-testid*='trackingNumber-input__input'], "
            "input[aria-label='Carrier Tracking #'], "
            "input[aria-label*='Tracking' i]"
        )
        for i in range(min(inputs.count(), 60)):
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
        for ctx in _contexts(page):
            try:
                if _fill_tracking_for_order_index(ctx, 0, tracking) > 0:
                    tracking_filled = True
                    break
            except Exception:
                continue

    if not tracking_filled:
        try:
            if _fill_pack_pages_for_order(page, 0, tracking) > 0:
                tracking_filled = True
        except Exception:
            pass

    if not tracking_filled:
        for ctx in _contexts(page):
            for sel in (
                "input[data-testid='asn.header.shipment.billOfLading-input__input']",
                "input[data-testid*='billOfLading'][data-testid$='__input']",
            ):
                try:
                    trk = ctx.locator(sel).first
                    if trk.count() > 0 and trk.is_visible() and _fill_tracking_input(trk, tracking):
                        tracking_filled = True
                        break
                except Exception:
                    continue
            if tracking_filled:
                break

    if not tracking_filled:
        raise RuntimeError(
            "Could not fill Grainger ASN carrier tracking (tracking # / pack / BOL fields)."
        )


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


def _sps_visible_modal_like(page: Page) -> bool:
    """True when a dialog/modal is open — avoid ESC / generic buttons that dismiss creation flows."""
    for sel in ("[role='dialog']", ".sps-modal", "[class*='ReactModal__Content']"):
        for ctx in _contexts(page):
            try:
                loc = ctx.locator(sel)
                n = min(loc.count(), 6)
                for i in range(n):
                    if loc.nth(i).is_visible():
                        return True
            except Exception:
                continue
    return False


def clear_click_blockers(page: Page) -> None:
    """Best-effort removal of modal/backdrop overlays that intercept clicks."""
    if _sps_visible_modal_like(page):
        # Do not press Escape or click Continue/OK while a modal is open (breaks Shipment Create New).
        pass
    else:
        # Try common close controls (avoid Continue/OK — they advance the wrong modal on SPS).
        click_first_visible(
            page,
            [
                "button[aria-label='Close']",
                "button[title='Close']",
                "button:has-text('Close')",
                "button:has-text('Dismiss')",
                "button:has-text('Got it')",
                "[data-testid='modalCancelBtn']",
                "xpath=//*[self::button or @role='button'][contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'close')]",
            ],
            timeout_ms=1200,
        )
        # ESC often closes SPS drawers — but it also closes Create New modals; skip when dialog visible.
        for _ in range(2):
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(200)
            except Exception:
                pass
    # Removing overlays while a real modal is open can tear down the Shipment Create New dialog.
    if not _sps_visible_modal_like(page):
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
        # SPS can briefly bounce through auth/login-looking routes during SPA/iframe transitions.
        # Re-check once after a short settle delay before declaring logged out.
        try:
            page.wait_for_timeout(900)
        except Exception:
            pass
        if _looks_authenticated_sps(page):
            return
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
    try:
        settings = load_sps_settings()
        start_url = (settings.sps_url or "").strip() or "https://commerce.spscommerce.com"
        return (
            start_url,
            (settings.sps_username or "").strip(),
            (settings.sps_password or "").strip(),
            int(settings.timeout_ms),
        )
    except ValueError as exc:
        print(f"WARN: {exc}")
        start_url = (os.environ.get("SPS_URL") or "").strip() or "https://commerce.spscommerce.com"
        return start_url, "", "", 30_000


def _session_ready_for_workflow(page: Page) -> bool:
    """True only when transactions UI is reachable (not just a stale cookie on the landing page)."""
    if not _looks_authenticated_sps(page) or _is_login_page_visible(page):
        return False
    try:
        page.goto(TRANSACTIONS_LIST_URL, wait_until="domcontentloaded", timeout=120_000)
        page.wait_for_timeout(600)
        wait_for_transactions_page_ready(page, timeout_ms=30_000)
        return True
    except Exception:
        return False


def _invalidate_stale_sps_session(context: BrowserContext, storage_path: Path) -> None:
    """Drop saved cookies/file so the next login attempt is not fooled by an expired session."""
    try:
        context.clear_cookies()
    except Exception:
        pass
    if storage_path.is_file():
        try:
            storage_path.unlink()
            print(f"Removed stale SPS session file: {storage_path}")
        except OSError as exc:
            print(f"Warning: could not remove stale session file ({storage_path}): {exc}")


def _save_sps_session_if_ready(page: Page, context: BrowserContext, storage_path: Path, *, label: str) -> bool:
    if not _session_ready_for_workflow(page):
        return False
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(storage_path))
    print(f"{label}: {storage_path}")
    return True


def ensure_sps_session(
    page: Page,
    context: BrowserContext,
    storage_path: Path,
    *,
    headless: bool,
    allow_manual: bool = True,
) -> None:
    """Confirm SPS is signed in; refresh storage_path via .env login and/or manual sign-in."""
    start_url, _, _, _timeout_ms = _load_sps_login_settings()
    for probe_url in (DASHBOARD_URL, TRANSACTIONS_LIST_URL, start_url):
        try:
            page.goto(probe_url, wait_until="domcontentloaded", timeout=120_000)
            page.wait_for_timeout(700)
        except Exception:
            continue
        if _session_ready_for_workflow(page):
            _save_sps_session_if_ready(page, context, storage_path, label="SPS session OK; refreshed storage")
            return

    print("SPS session is not valid for transactions — clearing stale cookies and re-authenticating.")
    _invalidate_stale_sps_session(context, storage_path)

    if not headless and allow_manual:
        if login_with_env_credentials_then_save(page, context, storage_path):
            return
        interactive_login_then_save(page, context, storage_path)
        return

    if login_with_env_credentials_then_save(page, context, storage_path):
        return

    raise RuntimeError(
        "SPS Commerce is not signed in and automatic login could not complete. "
        f"Set SPS_USERNAME and SPS_PASSWORD in {DEFAULT_STORAGE_STATE.parent / '.env'}, "
        "then re-run with a visible browser (HEADLESS=false) so you can complete MFA if needed. "
        "Or run once with --interactive-login to save a session file."
    )


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
            if _session_ready_for_workflow(page):
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

    if not _session_ready_for_workflow(page):
        print(
            ">>> Complete SPS sign-in in the browser (including MFA if prompted), then press Enter."
        )
        input(">>> Press Enter once SPS is fully logged in...\n")
        if not _session_ready_for_workflow(page):
            raise RuntimeError(
                "SPS login was not detected after manual sign-in (transactions page did not load)."
            )

    if not _save_sps_session_if_ready(page, context, storage_path, label=">>> Saved session file"):
        raise RuntimeError("SPS sign-in looked complete but transactions page is still not reachable.")
    print()


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

    if _save_sps_session_if_ready(page, context, storage_path, label="SPS session already valid; saved session"):
        return True

    if not _perform_sps_login(page, username, password, effective_timeout):
        print("SPS auto-login could not complete username/password submit from .env.")
        return False
    if _save_sps_session_if_ready(page, context, storage_path, label="SPS auto-login succeeded; saved session"):
        return True
    print("SPS auto-login did not reach the transactions page (MFA/SSO may still be required).")
    return False


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


def open_ready_for_shipment(page: Page, partner_name: str = "Tractor Supply Dropship") -> None:
    # Use only the Transactions + Advanced Search path.
    open_ready_for_shipment_via_advanced_search(page, partner_name=partner_name)


def open_ready_for_shipment_via_advanced_search(page: Page, *, partner_name: str = "Tractor Supply Dropship") -> None:
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

    print(f"STEP 1.3: Select Partner = {partner_name}...")
    ensure_partner_selected(page, partner_name)
    print("STEP 1.3 done.")

    print("STEP 1.4: Select Workflow = Shipment...")
    # Force focus to the exact Workflows Ready For box to avoid typing into Document Type.
    click_first_visible(
        page,
        [
            "input[data-testid='advancedSearchWorkflowsMultiselect__option-list-input']",
            "xpath=//*[contains(normalize-space(.), 'Workflows Ready For')]/following::input[1]",
        ],
        timeout_ms=3000,
    )
    ensure_workflow_shipment_selected(page, workflow_selector)
    print("STEP 1.4 done.")

    print("STEP 1.5: Click Search...")
    click_advanced_search_button(page)
    page.wait_for_load_state("domcontentloaded")
    print("STEP 1.5 done.")


def ensure_partner_selected(page: Page, partner_name: str) -> None:
    wanted = (partner_name or "").strip()
    if not wanted:
        return

    def _partner_selected() -> bool:
        low = wanted.lower()
        checks = [
            f"xpath=//*[contains(@id,'_tag-') and contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{low}')]",
            f"xpath=//*[contains(@class,'tag') and contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{low}')]",
            f"xpath=//*[contains(@class,'chip') and contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{low}')]",
        ]
        for sel in checks:
            for ctx in _contexts(page):
                try:
                    loc = ctx.locator(sel)
                    if loc.count() > 0 and loc.first.is_visible():
                        return True
                except Exception:
                    continue
        return False

    def _clear_partner_tags() -> None:
        for _ in range(6):
            removed = False
            for sel in (
                "button[aria-label*='Remove']",
                "button[title*='Remove']",
                "[data-testid*='remove']",
                "[class*='tag'] [class*='close']",
                "[class*='chip'] [class*='close']",
                "i.sps-icon-close",
                "i.sps-icon-x",
            ):
                for ctx in _contexts(page):
                    try:
                        loc = ctx.locator(sel)
                        n = min(loc.count(), 8)
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
                                page.wait_for_timeout(70)
                                break
                            except Exception:
                                continue
                        if removed:
                            break
                    except Exception:
                        continue
                if removed:
                    break
            if not removed:
                break

    def _partner_inputs():
        selectors = [
            "input[data-testid='advancedSearchPartnerMultiselect__option-list-input']",
            "input[data-testid='advancedSearchPartnersMultiselect__option-list-input']",
            "input[data-testid='advancedSearchTradingPartnersMultiselect__option-list-input']",
            "xpath=//*[contains(normalize-space(.), 'Partner')]/following::input[1]",
        ]
        out = []
        for sel in selectors:
            for ctx in _contexts(page):
                try:
                    loc = ctx.locator(sel)
                    if loc.count() == 0:
                        continue
                    out.append(loc.first)
                except Exception:
                    continue
        return out

    def _choose_partner_option(active_input=None) -> bool:
        low = wanted.lower()
        # Deterministic path: click option from the partner input's own option list.
        if active_input is not None:
            try:
                owns = (active_input.get_attribute("aria-owns") or "").strip()
            except Exception:
                owns = ""
            if owns:
                for sel in (
                    f"#{owns} [role='option']:has-text('{wanted}')",
                    f"#{owns} li[role='option']:has-text('{wanted}')",
                    f"#{owns} [role='option']",
                ):
                    try:
                        loc = page.locator(sel)
                        if loc.count() == 0:
                            continue
                        # Prefer exact-ish text match first.
                        chosen = None
                        for i in range(loc.count()):
                            node = loc.nth(i)
                            txt = (node.inner_text() or "").strip().lower()
                            if wanted.lower() in txt:
                                chosen = node
                                break
                        if chosen is None:
                            chosen = loc.first
                        chosen.click(timeout=1500)
                        page.wait_for_timeout(180)
                        if _partner_selected():
                            return True
                    except Exception:
                        continue
        # Fallback option selectors across contexts.
        if click_first_visible(
            page,
            [
                f"li[role='option']:has-text('{wanted}')",
                f"a[role='option']:has-text('{wanted}')",
                f"[role='option']:has-text('{wanted}')",
                f"xpath=//*[contains(@id,'option') and @role='option' and contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{low}')]",
                f"xpath=//*[@role='option' and contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{low}')]",
            ],
            timeout_ms=1_400,
        ):
            page.wait_for_timeout(180)
            return _partner_selected()
        return False

    if _partner_selected():
        return

    _clear_partner_tags()
    for attempt in range(1, 6):
        clear_click_blockers(page)
        typed = False
        active_input = None
        for inp in _partner_inputs():
            try:
                inp.wait_for(state="visible", timeout=1_800)
                try:
                    inp.click(timeout=900)
                except Exception:
                    inp.click(timeout=900, force=True)
                try:
                    inp.fill("", timeout=700)
                except Exception:
                    pass
                page.keyboard.press("Control+A")
                page.keyboard.press("Delete")
                inp.type(wanted, delay=18)
                typed = True
                active_input = inp
                break
            except Exception:
                continue
        if not typed:
            page.wait_for_timeout(180)
            continue

        picked = _choose_partner_option(active_input=active_input)
        if not picked:
            page.wait_for_timeout(220)
            continue

        # Commit selection without tabbing into the next field (Document Type).
        try:
            if active_input is not None:
                active_input.click(timeout=500)
        except Exception:
            pass
        page.wait_for_timeout(180)
        if _partner_selected():
            print(f"Partner filter confirmed as '{wanted}' on attempt {attempt}.")
            return
        _clear_partner_tags()
        page.wait_for_timeout(120)

    raise RuntimeError(f"Could not select Partner '{wanted}' in Advanced Search.")


def clear_document_type_filter(page: Page) -> None:
    """Ensure Document Type is empty so partner/workflow search is not over-filtered."""
    # Remove selected chips if present.
    for _ in range(6):
        removed = False
        for sel in (
            "xpath=//*[contains(normalize-space(.), 'Document Type')]/following::button[contains(@aria-label,'Remove') or contains(@title,'Remove')][1]",
            "xpath=//*[contains(normalize-space(.), 'Document Type')]/following::*[contains(@class,'close') or contains(@class,'sps-icon-close')][1]",
        ):
            for ctx in _contexts(page):
                try:
                    loc = ctx.locator(sel)
                    if loc.count() == 0:
                        continue
                    node = loc.first
                    if not node.is_visible():
                        continue
                    try:
                        node.click(timeout=700)
                    except Exception:
                        node.click(timeout=700, force=True)
                    removed = True
                    page.wait_for_timeout(80)
                    break
                except Exception:
                    continue
            if removed:
                break
        if not removed:
            break

    # Also clear typed value if input exists.
    for ctx in _contexts(page):
        for sel in (
            "input[data-testid='advancedSearchDocumentTypeMultiselect__option-list-input']",
            "xpath=//*[contains(normalize-space(.), 'Document Type')]/following::input[1]",
        ):
            try:
                loc = ctx.locator(sel)
                if loc.count() == 0:
                    continue
                inp = loc.first
                if not inp.is_visible():
                    continue
                try:
                    inp.click(timeout=600)
                except Exception:
                    inp.click(timeout=600, force=True)
                try:
                    inp.fill("", timeout=500)
                except Exception:
                    pass
            except Exception:
                continue


def ensure_document_type_order(page: Page) -> None:
    """Force Document Type filter to Order to neutralize accidental focus drift."""
    clear_document_type_filter(page)
    for attempt in range(1, 6):
        clear_click_blockers(page)
        field = None
        for ctx in _contexts(page):
            for sel in (
                "input[data-testid='advancedSearchDocumentTypeMultiselect__option-list-input']",
                "xpath=//*[contains(normalize-space(.), 'Document Type')]/following::input[1]",
            ):
                try:
                    loc = ctx.locator(sel)
                    if loc.count() == 0:
                        continue
                    cand = loc.first
                    cand.wait_for(state="visible", timeout=1_500)
                    field = cand
                    break
                except Exception:
                    continue
            if field is not None:
                break
        if field is None:
            page.wait_for_timeout(180)
            continue
        try:
            field.click(timeout=900)
        except Exception:
            field.click(timeout=900, force=True)
        try:
            field.fill("", timeout=700)
        except Exception:
            pass
        field.type("Order", delay=20)
        picked = click_first_visible(
            page,
            [
                "li[role='option']:has-text('Order')",
                "[role='option']:has-text('Order')",
                "xpath=//*[@role='option' and contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'order')]",
            ],
            timeout_ms=1_400,
        )
        if not picked:
            try:
                field.press("ArrowDown")
                field.press("Enter")
            except Exception:
                pass
        page.wait_for_timeout(150)
        for sel in (
            "xpath=//*[contains(@class,'tag') and contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'order')]",
            "xpath=//*[contains(@class,'chip') and contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'order')]",
            "xpath=//*[contains(normalize-space(.), 'Document Type')]/following::*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'order')][1]",
        ):
            for ctx in _contexts(page):
                try:
                    loc = ctx.locator(sel)
                    if loc.count() > 0 and loc.first.is_visible():
                        print(f"Document Type filter confirmed as 'Order' on attempt {attempt}.")
                        return
                except Exception:
                    continue
    raise RuntimeError("Could not set Document Type filter to 'Order'.")


def set_workflow_ready_for_shipment(page: Page, workflow_selector: str) -> None:
    def _clear_existing_workflow_tags() -> None:
        # Remove only chips inside "Workflows Ready For" control (do NOT clear Partner chips).
        workflow_close_selectors = [
            "xpath=//*[contains(normalize-space(.), 'Workflows Ready For')]/following::*[contains(@class,'tag') or contains(@class,'chip')][1]//*[contains(@class,'close') or self::button][1]",
            "xpath=//*[contains(normalize-space(.), 'Workflows Ready For')]/following::button[contains(@aria-label,'Remove') or contains(@title,'Remove')][1]",
            "xpath=//*[contains(normalize-space(.), 'Workflows Ready For')]/following::*[contains(@class,'sps-icon-close') or contains(@class,'sps-icon-x')][1]",
        ]
        for _ in range(8):
            removed = False
            for sel in workflow_close_selectors:
                for ctx in _contexts(page):
                    try:
                        loc = ctx.locator(sel)
                        if loc.count() == 0:
                            continue
                        node = loc.first
                        if not node.is_visible():
                            continue
                        try:
                            node.click(timeout=700)
                        except Exception:
                            try:
                                node.evaluate("el => el.click()")
                            except Exception:
                                node.click(timeout=700, force=True)
                        removed = True
                        page.wait_for_timeout(80)
                        break
                    except Exception:
                        continue
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
        # Strict primary path: exact workflow input testid.
        for ctx in _contexts(page):
            try:
                loc = ctx.locator("input[data-testid='advancedSearchWorkflowsMultiselect__option-list-input']")
                if loc.count() == 0:
                    continue
                fld = loc.first
                fld.wait_for(state="visible", timeout=1_500)
                try:
                    fld.click(timeout=900)
                except Exception:
                    fld.click(timeout=900, force=True)
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
        # Fallback: provided workflow selector.
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

    def _pick_shipment_option(active_field=None) -> bool:
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
        # Then keyboard selection as fallback, but only on the workflow field.
        try:
            active_testid = (
                page.evaluate(
                    "() => (document.activeElement && document.activeElement.getAttribute('data-testid')) || ''"
                )
                or ""
            )
            if active_field is None or "advancedsearchworkflowsmultiselect" not in active_testid.lower():
                return False
            active_field.press("ArrowDown")
            active_field.press("Enter")
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
        active_workflow_field = None
        for ctx in _contexts(page):
            try:
                loc = ctx.locator("input[data-testid='advancedSearchWorkflowsMultiselect__option-list-input']")
                if loc.count() == 0:
                    continue
                fld = loc.first
                if fld.is_visible():
                    active_workflow_field = fld
                    break
            except Exception:
                continue
        if not typed:
            typed = False
        if not typed:
            page.wait_for_timeout(200)
            continue

        _pick_shipment_option(active_field=active_workflow_field)
        page.wait_for_timeout(160)

        # Commit workflow selection without tabbing to neighboring filters.
        try:
            if active_workflow_field is not None:
                active_workflow_field.click(timeout=500)
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


def _open_next_order_from_results(page: Page, processed_order_ids: set[str]) -> str | None:
    """Open next visible open order from filtered results (top-down)."""
    max_pages = 80
    for _ in range(1, max_pages + 1):
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
                    order_id = normalize_po(link.inner_text().strip())
                except Exception:
                    order_id = ""
                if not order_id or order_id in processed_order_ids:
                    continue
                row = link.locator("xpath=ancestor::tr[1]")
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
                        link.evaluate("(el) => el.click()")
                    except Exception:
                        link.click(timeout=2500, force=True)
                page.wait_for_load_state("domcontentloaded")
                page.wait_for_timeout(500)
                return order_id
        if not _go_next_results_page(page):
            break
    return None


def _sps_url_is_document(url: str) -> bool:
    return "/fulfillment/transactions/document/" in (url or "").lower()


def _sps_url_is_transaction_hub_not_document(url: str) -> bool:
    """Transactions area (list/search/inbox) but not an order/document detail URL."""
    u = (url or "").lower()
    if "/fulfillment/transactions/document/" in u:
        return False
    return "/fulfillment/transactions/" in u


def _grainger_shipment_workflow_visible(page: Page) -> bool:
    """True when workflow rail shows a Shipment step (ASN is created from this order document)."""
    if not _sps_url_is_document(page.url or ""):
        return False
    _tr_from = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    _tr_to = "abcdefghijklmnopqrstuvwxyz"
    xpath = (
        f"xpath=//*[(contains(@class,'workflow') or @data-testid='workflow')]"
        f"//*[contains(translate(normalize-space(.),'{_tr_from}','{_tr_to}'),'shipment')]"
    )
    for ctx in _contexts(page):
        try:
            loc = ctx.locator(xpath)
            if loc.count() > 0 and loc.first.is_visible():
                return True
        except Exception:
            continue
    return False


def _wait_grainger_order_document_ready_for_asn(page: Page, *, timeout_ms: int = 90_000) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        if _grainger_shipment_workflow_visible(page):
            return True
        page.wait_for_timeout(300)
    return False


def _grainger_ack_indicator_span(page: Page):
    """First span[@title] under Order Status -> Acknowledgement in the workflow rail, if visible."""
    _tr_from = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    _tr_to = "abcdefghijklmnopqrstuvwxyz"
    xpath = (
        f"xpath=(//*[(contains(@class,'workflow') or @data-testid='workflow')])[1]"
        f"//*[contains(translate(normalize-space(.),'{_tr_from}','{_tr_to}'),'acknowledgement') "
        f"or contains(translate(normalize-space(.),'{_tr_from}','{_tr_to}'),'acknowledgment')][1]"
        f"/following::span[@title][1]"
    )
    for ctx in _contexts(page):
        try:
            loc = ctx.locator(xpath)
            if loc.count() == 0:
                continue
            node = loc.first
            if node.is_visible():
                return node
        except Exception:
            continue
    return None


def _grainger_span_looks_like_sent_ack(title: str, text: str) -> bool:
    if re.search(r"view\s+all", text or "", re.I):
        return True
    t = (title or "").strip()
    if not t:
        return False
    if "," in t and re.search(r"\d", t):
        return True
    if re.fullmatch(r"\d{6,12}", t):
        return True
    return False


def _grainger_wait_ack_transaction_list(page: Page, *, timeout_ms: int = 45_000) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        u = (page.url or "").lower()
        hub = "/fulfillment/transactions/" in u and "/document/" not in u
        body_snip = ""
        try:
            body_snip = (page.inner_text("body") or "")[:4000]
        except Exception:
            pass
        if hub or re.search(r"matching\s+results", body_snip, re.I):
            for ctx in _contexts(page):
                try:
                    if ctx.locator("tbody tr a[href*='/fulfillment/transactions/document/']").count() >= 1:
                        return True
                except Exception:
                    continue
        page.wait_for_timeout(320)
    return False


def _grainger_click_first_ack_row_document_link(page: Page) -> bool:
    """After View All: first results row that looks like an Acknowledgment document."""
    for ctx in _contexts(page):
        try:
            rows = ctx.locator("tbody tr")
            n = min(rows.count(), 80)
            for i in range(n):
                row = rows.nth(i)
                try:
                    if not row.is_visible():
                        continue
                except Exception:
                    continue
                try:
                    rt = row.inner_text()
                except Exception:
                    continue
                if re.search(r"Acknowledgement|Acknowledgment", rt, re.I) is None:
                    continue
                link = row.locator("a.text-truncate[href*='/fulfillment/transactions/document/']").first
                if link.count() == 0:
                    link = row.locator("a[href*='/fulfillment/transactions/document/']").first
                if link.count() == 0:
                    continue
                btn = link.first
                try:
                    if not btn.is_visible():
                        continue
                except Exception:
                    continue
                try:
                    btn.scroll_into_view_if_needed()
                except Exception:
                    pass
                try:
                    btn.click(timeout=5000)
                except Exception:
                    try:
                        btn.evaluate("el => el.click()")
                    except Exception:
                        btn.click(timeout=5000, force=True)
                return True
        except Exception:
            continue
    for ctx in _contexts(page):
        try:
            link = ctx.locator("tbody tr a.text-truncate[href*='/fulfillment/transactions/document/']").first
            if link.count() > 0 and link.is_visible():
                link.click(timeout=5000)
                return True
        except Exception:
            continue
    return False


def _wait_grainger_ack_document_ready_for_shipment(page: Page, *, timeout_ms: int = 90_000) -> bool:
    """Acknowledgment document (read-only or editable) before Shipment -> New."""
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        if not _sps_url_is_document(page.url or ""):
            page.wait_for_timeout(300)
            continue
        for sel in (
            "[data-testid='poAck2.header.ackType-select-value']",
            "[data-testid^='poAck2.']",
            "th:has-text('SKU')",
            "text=/\\bSKU\\b/",
            "text=/\\bUnits\\b/",
        ):
            for ctx in _contexts(page):
                try:
                    loc = ctx.locator(sel)
                    if loc.count() > 0 and loc.first.is_visible():
                        return True
                except Exception:
                    continue
        page.wait_for_timeout(320)
    return False


def _grainger_try_open_existing_acknowledgment_flow(page: Page) -> bool:
    """
    If acknowledgments already exist, open one: single-id span -> that document; View All ->
    transaction list -> first Acknowledgment row document link. Returns False to create a new ack.
    """
    sp = _grainger_ack_indicator_span(page)
    if sp is None:
        return False
    try:
        title = (sp.get_attribute("title") or "").strip()
        txt = (sp.inner_text() or "").strip()
    except Exception:
        return False
    if not _grainger_span_looks_like_sent_ack(title, txt):
        return False
    is_view_all = bool(re.search(r"view\s+all", txt, re.I)) or "," in title
    print(f"Grainger: existing acknowledgment control found (View All={is_view_all}); opening ack document.")
    try:
        sp.scroll_into_view_if_needed()
    except Exception:
        pass
    try:
        sp.click(timeout=4000)
    except Exception:
        try:
            sp.evaluate("el => el.click()")
        except Exception:
            sp.click(timeout=4000, force=True)
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(500)
    if is_view_all:
        if not _grainger_wait_ack_transaction_list(page, timeout_ms=45_000):
            raise RuntimeError("Acknowledgment list (after View All) did not load.")
        if not _grainger_click_first_ack_row_document_link(page):
            raise RuntimeError("Could not open first Acknowledgment document from the list.")
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(600)
    return True


def _grainger_modal_pick_advance_ship_notice(page: Page) -> None:
    """Shipment -> Create New modal: select Advance Ship Notice inside the dialog only."""
    deadline = time.monotonic() + 25.0
    asn_label = re.compile(r"advance\s*ship\s*notice", re.I)
    while time.monotonic() < deadline:
        for ctx in _contexts(page):
            for root in ("[role='dialog']", ".sps-modal", "[class*='modal__']"):
                try:
                    roots = ctx.locator(root)
                    for ri in range(min(roots.count(), 6)):
                        dlg = roots.nth(ri)
                        try:
                            if not dlg.is_visible():
                                continue
                        except Exception:
                            continue
                        try:
                            blob = dlg.inner_text(timeout=1200)[:2500]
                        except Exception:
                            continue
                        if not re.search(r"shipment|advance|ship|notice|asn|create\s*new", blob, re.I):
                            continue
                        for sel in (
                            "label.sps-checkable__label",
                            "label",
                            "span",
                            "div",
                        ):
                            try:
                                cand = dlg.locator(sel).filter(has_text=asn_label)
                                if cand.count() == 0:
                                    continue
                                node = cand.first
                                if node.is_visible():
                                    node.scroll_into_view_if_needed()
                                    node.click(timeout=3500)
                                    return
                            except Exception:
                                continue
                        try:
                            hit = dlg.locator("span, div, label, p").filter(has_text=asn_label)
                            if hit.count() > 0 and hit.first.is_visible():
                                hit.first.click(timeout=3500)
                                return
                        except Exception:
                            pass
                        try:
                            tline = dlg.locator("text=/Advance\\s+Ship\\s*Notice/i")
                            if tline.count() > 0 and tline.first.is_visible():
                                tline.first.click(timeout=3500)
                                return
                        except Exception:
                            pass
                except Exception:
                    continue
        page.wait_for_timeout(280)
    raise RuntimeError("Could not select Advance Ship Notice inside Shipment Create New modal.")


def _wait_for_grainger_invoice_from_po_modal_ready(page: Page, *, timeout_ms: int = 20_000) -> bool:
    """Billing -> New modal showing Invoice (From PO) option."""
    inv = re.compile(r"invoice\s*\(\s*from\s*po", re.I)
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        for ctx in _contexts(page):
            try:
                dlg = ctx.locator("[role='dialog'], .sps-modal").filter(has_text=inv)
                if dlg.count() > 0 and dlg.first.is_visible():
                    return True
            except Exception:
                continue
            try:
                lab = ctx.locator("label.sps-checkable__label").filter(has_text=inv)
                if lab.count() > 0 and lab.first.is_visible():
                    return True
            except Exception:
                continue
        page.wait_for_timeout(220)
    return False


def _grainger_modal_pick_invoice_from_po(page: Page) -> None:
    """Inside Billing Create New modal: select the Invoice (From PO) radio/label."""
    inv_label = re.compile(r"invoice\s*\(\s*from\s*po\s*\)", re.I)
    deadline = time.monotonic() + 25.0
    while time.monotonic() < deadline:
        for ctx in _contexts(page):
            for root in ("[role='dialog']", ".sps-modal", "[class*='modal__']"):
                try:
                    roots = ctx.locator(root)
                    for ri in range(min(roots.count(), 8)):
                        dlg = roots.nth(ri)
                        try:
                            if not dlg.is_visible():
                                continue
                        except Exception:
                            continue
                        try:
                            blob = dlg.inner_text(timeout=1200)[:3500]
                        except Exception:
                            continue
                        if not re.search(r"invoice|billing|from\s*po|create\s*new", blob, re.I):
                            continue
                        for sel in ("label.sps-checkable__label", "label", "span", "div"):
                            try:
                                cand = dlg.locator(sel).filter(has_text=inv_label)
                                if cand.count() == 0:
                                    continue
                                node = cand.first
                                if node.is_visible():
                                    node.scroll_into_view_if_needed()
                                    node.click(timeout=3500)
                                    page.wait_for_timeout(200)
                                    return
                            except Exception:
                                continue
                        try:
                            tline = dlg.locator("text=/Invoice\\s*\\(\\s*From\\s*PO\\s*\\)/i")
                            if tline.count() > 0 and tline.first.is_visible():
                                tline.first.click(timeout=3500)
                                page.wait_for_timeout(200)
                                return
                        except Exception:
                            pass
                        try:
                            radios = dlg.locator("input[type='radio']")
                            for rj in range(min(radios.count(), 24)):
                                r = radios.nth(rj)
                                try:
                                    if not r.is_visible():
                                        continue
                                except Exception:
                                    continue
                                rid = (r.get_attribute("id") or "").strip()
                                if rid:
                                    lab = dlg.locator(f"label[for='{rid}']")
                                    if lab.count() > 0:
                                        try:
                                            lt = lab.first.inner_text(timeout=500)
                                        except Exception:
                                            lt = ""
                                        if lt and re.search(inv_label, lt):
                                            lab.first.click(timeout=3000)
                                            page.wait_for_timeout(200)
                                            return
                                try:
                                    host = r.locator("xpath=ancestor::label[1]")
                                    if host.count() == 0:
                                        host = r.locator(
                                            "xpath=ancestor::div[contains(@class,'checkable')][1]"
                                        )
                                    if host.count() > 0:
                                        ht = host.first.inner_text(timeout=500)[:400]
                                        if re.search(inv_label, ht):
                                            host.first.click(timeout=3000)
                                            page.wait_for_timeout(200)
                                            return
                                except Exception:
                                    continue
                        except Exception:
                            pass
                except Exception:
                    continue
        page.wait_for_timeout(280)
    raise RuntimeError("Could not select Invoice (From PO) inside Billing Create New modal.")


def _grainger_modal_click_create_new_after_invoice_choice(page: Page) -> bool:
    """Click Create New on the same Billing modal after Invoice (From PO) is selected."""
    inv = re.compile(r"from\s*po|invoice", re.I)
    for ctx in _contexts(page):
        for root in ("[role='dialog']", ".sps-modal"):
            try:
                roots = ctx.locator(root)
                for ri in range(min(roots.count(), 8)):
                    dlg = roots.nth(ri)
                    try:
                        if not dlg.is_visible():
                            continue
                    except Exception:
                        continue
                    try:
                        b = dlg.inner_text(timeout=800)[:2500]
                    except Exception:
                        continue
                    if not re.search(inv, b):
                        continue
                    for bs in (
                        "button[data-testid='modalOkBtn'][title='Create New']",
                        "button[data-testid='modalOkBtn']:has-text('Create New')",
                        "div.sps-button.sps-button--confirm button[data-testid='modalOkBtn']",
                    ):
                        try:
                            btn = dlg.locator(bs).first
                            if btn.count() > 0 and btn.is_visible():
                                try:
                                    btn.scroll_into_view_if_needed()
                                except Exception:
                                    pass
                                try:
                                    btn.click(timeout=4000)
                                except Exception:
                                    try:
                                        btn.evaluate("el => el.click()")
                                    except Exception:
                                        btn.click(timeout=4000, force=True)
                                return True
                        except Exception:
                            continue
            except Exception:
                continue
    # Fallback: any visible modal with Create New (single Billing dialog expected).
    for ctx in _contexts(page):
        for root in ("[role='dialog']", ".sps-modal"):
            try:
                roots = ctx.locator(root)
                for ri in range(min(roots.count(), 8)):
                    dlg = roots.nth(ri)
                    try:
                        if not dlg.is_visible():
                            continue
                    except Exception:
                        continue
                    btn = dlg.locator(
                        "button[data-testid='modalOkBtn'][title='Create New'], "
                        "button[data-testid='modalOkBtn']:has-text('Create New')"
                    ).first
                    try:
                        if btn.count() > 0 and btn.is_visible():
                            btn.click(timeout=4000)
                            return True
                    except Exception:
                        continue
            except Exception:
                continue
    return False


def _wait_grainger_invoice_editor_ready(page: Page, *, timeout_ms: int = 90_000) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        for ctx in _contexts(page):
            try:
                d = ctx.locator("input[data-testid='invoice2.header.invoiceDate-input_date_input']").first
                if d.count() > 0 and d.is_visible():
                    return True
            except Exception:
                continue
        page.wait_for_timeout(300)
    return False


def _open_grainger_document_from_list_by_po(page: Page, po: str) -> bool:
    """On transactions results (any results page), click the document link matching this PO."""
    norm = normalize_po(po)
    if not norm:
        return False
    max_pages = 80
    for _ in range(1, max_pages + 1):
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
                    link_po = normalize_po(link.inner_text().strip())
                except Exception:
                    link_po = ""
                if link_po != norm:
                    continue
                try:
                    link.scroll_into_view_if_needed()
                except Exception:
                    pass
                try:
                    link.click(timeout=3000)
                except Exception:
                    try:
                        link.evaluate("(el) => el.click()")
                    except Exception:
                        link.click(timeout=3000, force=True)
                page.wait_for_load_state("domcontentloaded")
                page.wait_for_timeout(500)
                return True
        if not _go_next_results_page(page):
            break
    return False


def _ensure_grainger_on_order_view_after_ack_send(
    page: Page,
    anchor_document_url: str,
    po: str,
    *,
    partner_name: str,
    timeout_ms: int = 120_000,
) -> None:
    """
    Sending an acknowledgment can redirect to the transactions list. ASN must be started from the
    order document page; stay there until Shipment workflow is usable.
    """
    deadline = time.monotonic() + timeout_ms / 1000.0
    page.wait_for_timeout(700)
    # Give SPA time to settle on acknowledgment / order document without list flash.
    while time.monotonic() < deadline:
        if _grainger_shipment_workflow_visible(page):
            print("Grainger: on order document with Shipment workflow after acknowledgment.")
            return
        if _sps_url_is_transaction_hub_not_document(page.url or ""):
            break
        page.wait_for_timeout(350)
    if _grainger_shipment_workflow_visible(page):
        return

    reopened = False
    if anchor_document_url and _sps_url_is_document(anchor_document_url):
        print("Grainger: reopening saved order document after acknowledgment send.")
        try:
            page.goto(anchor_document_url, wait_until="domcontentloaded", timeout=120_000)
            page.wait_for_timeout(900)
            reopened = True
        except Exception as ex:
            print(f"Grainger: goto saved document URL failed ({ex}); trying list fallback.")
    if reopened and _wait_grainger_order_document_ready_for_asn(page, timeout_ms=75_000):
        return

    # Ack doc may load before the shipment step paints; avoid list-click while still on document URL.
    if _sps_url_is_document(page.url or "") and not _grainger_shipment_workflow_visible(page):
        if _wait_grainger_order_document_ready_for_asn(page, timeout_ms=45_000):
            return

    # Results list / hub: reopen by PO column link.
    if _sps_url_is_transaction_hub_not_document(page.url or ""):
        print(f"Grainger: locating PO {po} on results to reopen order document.")
        try:
            open_ready_for_shipment(page, partner_name=partner_name)
        except Exception as ex:
            print(f"Grainger: could not refresh Advanced Search ({ex}); trying current results page.")
        if _open_grainger_document_from_list_by_po(page, po):
            page.wait_for_timeout(600)
            if _wait_grainger_order_document_ready_for_asn(page, timeout_ms=75_000):
                return

    if _grainger_shipment_workflow_visible(page):
        return

    raise RuntimeError(
        "After acknowledgment send, could not reach the order document with Shipment workflow "
        "(avoid returning to transactions until ASN and invoice finish)."
    )


def _select_dropdown_value_by_testid(page: Page, value_testid: str, option_text: str) -> bool:
    for ctx in _contexts(page):
        try:
            value = ctx.locator(f"[data-testid='{value_testid}']").first
            if value.count() == 0:
                continue
            value.wait_for(state="visible", timeout=5000)
            try:
                value.click(timeout=1500)
            except Exception:
                value.click(timeout=1500, force=True)
            if click_first_visible(
                page,
                [
                    f"[role='option']:has-text('{option_text}')",
                    f"li[role='option']:has-text('{option_text}')",
                    f"text={option_text}",
                ],
                timeout_ms=2500,
            ):
                return True
        except Exception:
            continue
    return False


def _wait_for_ack_form_ready(page: Page, timeout_ms: int = 60_000) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        # Require acknowledgment-editor signals; do not treat base PO page as ready.
        for sel in (
            "[data-testid='poAck2.header.ackType-select-value']",
            "[data-testid='poAck2.ackRep.detail.0.additionalInfo.itemStatus-select-value']",
            "input[data-testid^='poAck2.']",
            "text=/acknowledge\\s*-\\s*with\\s*detail\\s*no\\s*change/i",
            "text=/item\\s*accepted/i",
        ):
            for ctx in _contexts(page):
                try:
                    loc = ctx.locator(sel)
                    if loc.count() > 0 and loc.first.is_visible():
                        return True
                except Exception:
                    continue
        page.wait_for_timeout(250)
    return False


def _grainger_csv_tracking_for_open_order(open_po: str, tracking_by_po: dict[str, str]) -> tuple[str, str] | None:
    """
    Grainger: we already opened this order from the list — use that PO for CSV lookup.
    Do not rely on page.inner_text after redirects (list may not expose PO as \\b\\d{10}\\b).
    """
    norm = normalize_po(open_po)
    if not norm:
        return None
    tracking = tracking_by_po.get(norm)
    if tracking:
        return norm, tracking
    return None


def _extract_tracking_match_from_page(page: Page, tracking_by_po: dict[str, str]) -> tuple[str, str] | None:
    """Find a PO on the current page that exists in CSV tracking map (prefer 10-digit tokens)."""
    try:
        text = page.inner_text("body")
    except Exception:
        text = ""
    seen: set[str] = set()
    for po in re.findall(r"\b\d{10}\b", text):
        norm = normalize_po(po)
        if norm in seen:
            continue
        seen.add(norm)
        tracking = tracking_by_po.get(norm)
        if tracking:
            return norm, tracking
    # Shorter POs / alternate formatting: any digit run that normalizes to a map key.
    for m in re.finditer(r"\b\d{6,12}\b", text):
        norm = normalize_po(m.group(0))
        if not norm or norm in seen:
            continue
        seen.add(norm)
        tracking = tracking_by_po.get(norm)
        if tracking:
            return norm, tracking
    return None


def _estimate_gross_weight(page: Page) -> str:
    """
    Placeholder gross-weight estimate.
    Uses shipped/ordered qty if visible; defaults to 1 lb.
    """
    qty = 1
    try:
        text = page.inner_text("body")
    except Exception:
        text = ""
    for m in re.finditer(r"\b(\d+)\b", text):
        try:
            n = int(m.group(1))
        except Exception:
            continue
        if 1 <= n <= 500:
            qty = n
            break
    return str(max(1, qty))


def _click_grainger_acknowledgment_new(page: Page) -> None:
    """
    Acknowledgment 'New' is often a plain sps-button (no createNewBtn data-testid).
    Shipments/Billing typically use button[data-testid='createNewBtn'] — do not confuse them.
    """
    clear_click_blockers(page)
    _tr_from = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    _tr_to = "abcdefghijklmnopqrstuvwxyz"
    scoped_selectors = [
        # Scoped to workflow rail so we do not match body text; plain New, not createNewBtn.
        f"xpath=(//*[(contains(@class,'workflow') or @data-testid='workflow')]//*[contains(translate(normalize-space(.),'{_tr_from}','{_tr_to}'),'order status')])[1]"
        f"//*[contains(translate(normalize-space(.),'{_tr_from}','{_tr_to}'),'acknowledgement')]/following::button[contains(@class,'sps-button__clickable-element')][normalize-space()='New'][1]",
        f"xpath=(//*[(contains(@class,'workflow') or @data-testid='workflow')]//*[contains(translate(normalize-space(.),'{_tr_from}','{_tr_to}'),'order status')])[1]"
        f"//*[contains(translate(normalize-space(.),'{_tr_from}','{_tr_to}'),'acknowledgment')]/following::button[contains(@class,'sps-button__clickable-element')][normalize-space()='New'][1]",
        f"xpath=(//*[(contains(@class,'workflow') or @data-testid='workflow')]//*[contains(translate(normalize-space(.),'{_tr_from}','{_tr_to}'),'order status (')])[1]/following::button[contains(@class,'sps-button__clickable-element') and not(@data-testid='createNewBtn')][normalize-space()='New'][1]",
        # If workflow container is not class-tagged, fall back to first Order Status + Ack text path.
        f"xpath=(//*[contains(translate(normalize-space(.),'{_tr_from}','{_tr_to}'),'order status')])[1]"
        f"//*[contains(translate(normalize-space(.),'{_tr_from}','{_tr_to}'),'acknowledgement')]/following::button[contains(@class,'sps-button__clickable-element')][normalize-space()='New'][1]",
        f"xpath=(//*[contains(translate(normalize-space(.),'{_tr_from}','{_tr_to}'),'order status')])[1]"
        f"//*[contains(translate(normalize-space(.),'{_tr_from}','{_tr_to}'),'acknowledgment')]/following::button[contains(@class,'sps-button__clickable-element')][normalize-space()='New'][1]",
    ]
    for sel in scoped_selectors:
        if click_first_visible(page, [sel], timeout_ms=5000):
            return
        for ctx in _contexts(page):
            try:
                loc = ctx.locator(sel)
                if loc.count() == 0:
                    continue
                btn = loc.first
                btn.wait_for(state="visible", timeout=4000)
                try:
                    btn.click(timeout=2000)
                except Exception:
                    try:
                        btn.evaluate("el => el.click()")
                    except Exception:
                        btn.click(timeout=2000, force=True)
                return
            except Exception:
                continue
    raise RuntimeError(
        "Could not click Acknowledgment 'New' (plain workflow button — not createNewBtn)."
    )


def _create_grainger_ack_for_open_order(page: Page, *, submit: bool) -> None:
    # Open the Order Status -> Acknowledgment -> Create New modal.
    modal_ready = False
    for attempt in range(1, 4):
        _click_grainger_acknowledgment_new(page)
        page.wait_for_timeout(350)
        for sel in (
            "button[data-testid='modalOkBtn'][title='Create New']",
            "button[data-testid='modalOkBtn']:has-text('Create New')",
            "text=/create\\s+new/i",
            "text=/certified\\s+vendor\\s+poa/i",
            "text=/acknowledg/i",
        ):
            found = False
            for ctx in _contexts(page):
                try:
                    loc = ctx.locator(sel)
                    if loc.count() > 0 and loc.first.is_visible():
                        found = True
                        break
                except Exception:
                    continue
            if found:
                modal_ready = True
                break
        if modal_ready:
            break
        print(f"Grainger ack modal not ready on attempt {attempt}; retrying Acknowledgment New.")
        page.wait_for_timeout(250)
    if not modal_ready:
        raise RuntimeError("Grainger acknowledgment Create New popup did not open.")

    # Popup step: explicitly choose acknowledgment document option (radio circle/label).
    picked_ack_option = click_first_visible(
        page,
        [
            "label.sps-checkable__label:has-text('Certified Vendor POA')",
            "label.sps-checkable__label:has-text('Acknowledgment')",
            "text=Certified Vendor POA",
            "text=Acknowledgment",
            "input[type='radio'][data-testid*='ack']",
            "input[type='radio'][value*='ack']",
        ],
        timeout_ms=10_000,
    )
    if not picked_ack_option:
        # Try direct radio click in case label click is blocked by the overlay wall.
        for ctx in _contexts(page):
            for sel in (
                "input[type='radio']",
                "label.sps-checkable__label",
            ):
                try:
                    loc = ctx.locator(sel)
                    if loc.count() == 0:
                        continue
                    node = loc.first
                    try:
                        node.click(timeout=1200)
                    except Exception:
                        try:
                            node.evaluate("el => el.click()")
                        except Exception:
                            node.click(timeout=1200, force=True)
                    picked_ack_option = True
                    break
                except Exception:
                    continue
            if picked_ack_option:
                break
    if not picked_ack_option:
        raise RuntimeError("Could not select acknowledgment option in Grainger Create New popup.")

    # Then click Create New with robust fallback paths.
    created = click_first_visible(
        page,
        [
            "button[data-testid='modalOkBtn'][title='Create New']",
            "button[data-testid='modalOkBtn']:has-text('Create New')",
            "div.sps-button.sps-button--confirm button[data-testid='modalOkBtn'][title='Create New']",
        ],
        timeout_ms=10_000,
    )
    if not created:
        for ctx in _contexts(page):
            try:
                loc = ctx.locator("button[data-testid='modalOkBtn'][title='Create New']")
                if loc.count() == 0:
                    continue
                btn = loc.first
                try:
                    btn.click(timeout=1500)
                except Exception:
                    try:
                        btn.evaluate("el => el.click()")
                    except Exception:
                        btn.click(timeout=1500, force=True)
                created = True
                break
            except Exception:
                continue
    if not created:
        raise RuntimeError("Could not click Create New for Grainger Acknowledgment.")

    if not _wait_for_ack_form_ready(page, timeout_ms=60_000):
        raise RuntimeError("Grainger Acknowledgment form did not load.")

    ack_ok = _select_dropdown_value_by_testid(
        page,
        "poAck2.header.ackType-select-value",
        "Acknowledge - With Detail No Change",
    )
    item_ok = _select_dropdown_value_by_testid(
        page,
        "poAck2.ackRep.detail.0.additionalInfo.itemStatus-select-value",
        "Item Accepted",
    )
    if not ack_ok or not item_ok:
        raise RuntimeError("Grainger acknowledgment dropdowns were not set correctly.")
    print("Grainger acknowledgment form ready and values set.")

    if submit:
        send_documents(page)
    else:
        print("Dry run: acknowledgment prepared but not sent.")


def _create_grainger_asn_for_open_order(page: Page, tracking: str, *, submit: bool) -> None:
    _click_workflow_new(page, "Shipment", allow_global_fallback=False)
    page.wait_for_timeout(450)
    try:
        for ctx in _contexts(page):
            loc = ctx.locator("[role='dialog'], .sps-modal").first
            if loc.count() > 0:
                loc.wait_for(state="visible", timeout=12_000)
                break
    except Exception:
        pass
    try:
        _grainger_modal_pick_advance_ship_notice(page)
    except Exception as ex:
        print(f"Grainger ASN: scoped modal pick failed ({ex}); falling back to generic Advance Ship Notice click.")
        if not click_first_visible(
            page,
            [
                "label.sps-checkable__label:has-text('Advance Ship Notice')",
                "text=Advance Ship Notice",
            ],
            timeout_ms=6000,
        ):
            raise RuntimeError("Could not select Advance Ship Notice for Grainger ASN.") from ex
    if not click_first_visible(
        page,
        [
            "button[data-testid='modalOkBtn'][title='Create New']",
            "button[data-testid='modalOkBtn']:has-text('Create New')",
        ],
        timeout_ms=10000,
    ):
        raise RuntimeError("Could not click Create New for Grainger ASN.")
    wait_for_asn_form_ready(page, timeout_ms=90_000)

    weights_csv = resolve_sku_weights_csv_path()
    if weights_csv:
        print(f"Grainger ASN: using SKU weights map {weights_csv}")
    gross_weight = _grainger_gross_weight_lbs(page, weights_csv)
    for ctx in _contexts(page):
        try:
            gw = ctx.locator("input[data-testid='asn.header.shipment.grossWeight-input__input']").first
            if gw.count() > 0 and gw.is_visible():
                try:
                    gw.fill("")
                    gw.fill(gross_weight)
                except Exception:
                    gw.click(timeout=1200, force=True)
                    gw.fill(gross_weight)
                break
        except Exception:
            continue
    fill_asn_date(page)
    _fill_grainger_asn_tracking(page, tracking)

    if submit:
        send_documents(page)
        if not _wait_for_asn_document_ready(page, timeout_ms=70_000):
            raise RuntimeError("Grainger ASN post-send page did not become ready.")
    else:
        print("Dry run: Grainger ASN prepared but not sent.")


def _create_grainger_invoice_from_po_for_open_order(page: Page, *, submit: bool) -> None:
    opened = False
    for attempt in range(1, 7):
        try:
            _click_workflow_new(page, "Billing", allow_global_fallback=False)
        except Exception:
            pass
        page.wait_for_timeout(450)
        try:
            for ctx in _contexts(page):
                loc = ctx.locator("[role='dialog'], .sps-modal").first
                if loc.count() > 0:
                    loc.wait_for(state="visible", timeout=10_000)
                    break
        except Exception:
            pass
        if _wait_for_grainger_invoice_from_po_modal_ready(page, timeout_ms=6_000):
            opened = True
            break
        print(f"Grainger invoice: Billing Create New modal not ready yet (attempt {attempt}).")
        page.wait_for_timeout(400)
    if not opened:
        raise RuntimeError("Could not open Billing Create New modal for Invoice (From PO).")

    try:
        _grainger_modal_pick_invoice_from_po(page)
    except Exception as ex:
        print(f"Grainger invoice: dialog-scoped Invoice (From PO) click failed ({ex}); trying generic selectors.")
        if not click_first_visible(
            page,
            [
                "label.sps-checkable__label:has-text('Invoice (From PO)')",
                "label.sps-checkable__label:has-text('Invoice From PO')",
                "text=Invoice (From PO)",
                "xpath=//label[contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'invoice')][contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'from po')]",
            ],
            timeout_ms=7000,
        ):
            raise RuntimeError("Could not select Invoice (From PO) in Billing modal.") from ex

    page.wait_for_timeout(300)
    if not _grainger_modal_click_create_new_after_invoice_choice(page):
        if not click_first_visible(
            page,
            [
                "button[data-testid='modalOkBtn'][title='Create New']",
                "button[data-testid='modalOkBtn']:has-text('Create New')",
            ],
            timeout_ms=12_000,
        ):
            raise RuntimeError("Could not click Create New for Grainger Invoice (From PO).")

    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(500)
    if not _wait_grainger_invoice_editor_ready(page, timeout_ms=90_000):
        print("WARN: Grainger invoice editor fields not detected in time; continuing best-effort.")
    page.wait_for_timeout(400)

    today = datetime.now().strftime("%m/%d/%Y")
    for ctx in _contexts(page):
        try:
            d = ctx.locator("input[data-testid='invoice2.header.invoiceDate-input_date_input']").first
            if d.count() > 0 and d.is_visible():
                d.fill(today)
                break
        except Exception:
            continue

    po_for_invoice = ""
    for ctx in _contexts(page):
        try:
            src = ctx.locator("a.text-truncate.d-block[href*='/fulfillment/transactions/document/']").first
            if src.count() > 0:
                po_for_invoice = normalize_po(src.inner_text().strip())
                if po_for_invoice:
                    break
        except Exception:
            continue
    if not po_for_invoice:
        try:
            body = page.inner_text("body")
        except Exception:
            body = ""
        m = re.search(r"\b(\d{10})\b", body)
        po_for_invoice = m.group(1) if m else ""
    if po_for_invoice:
        for ctx in _contexts(page):
            for sel in (
                "input[data-testid='invoice2.header.invoiceNumber-input__input']",
                "input[aria-label='Invoice Number']",
            ):
                try:
                    inv = ctx.locator(sel).first
                    if inv.count() > 0 and inv.is_visible():
                        inv.fill(po_for_invoice)
                        break
                except Exception:
                    continue

    if submit:
        send_documents(page)
    else:
        print("Dry run: Grainger invoice prepared but not sent.")


def _click_workflow_new(page: Page, workflow_name: str, *, allow_global_fallback: bool = True) -> None:
    clear_click_blockers(page)
    # Prefer workflow-rail-local "New" (e.g., Billing -> New) to avoid clicking wrong section.
    selectors = [
        f"xpath=//*[contains(@class,'workflow') or @data-testid='workflow']//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{workflow_name.lower()}')]/following::button[@data-testid='createNewBtn' and @title='New'][1]",
        f"xpath=//*[normalize-space()='{workflow_name}']/following::button[@data-testid='createNewBtn' and @title='New'][1]",
        f"xpath=//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{workflow_name.lower()}')]/following::button[@data-testid='createNewBtn' and @title='New'][1]",
    ]
    if allow_global_fallback:
        selectors.append("button[data-testid='createNewBtn'][title='New']")

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
    page.wait_for_timeout(300)

    # Single-SKU: tracking may be visible on Header. Multi-SKU: tracking is on Order tab only.
    filled_pages = 0
    fill_path = "header-visible"
    for ctx in _contexts(page):
        loc = ctx.locator(
            "input[data-testid^='asn.order.0.packInfo.'][data-testid$='trackingNumber-input__input']"
        )
        for i in range(loc.count()):
            inp = loc.nth(i)
            try:
                if inp.is_visible() and _fill_tracking_input(inp, tracking):
                    filled_pages = 1
                    print("ASN: single-page tracking filled on current tab (Header).")
                    break
            except Exception:
                continue
        if filled_pages > 0:
            break

    if filled_pages <= 0:
        fill_path = "order-tab-pack-pages"
        if not _ensure_asn_order_tab_selected(page, 0):
            raise RuntimeError(
                "ASN ship date was set but Order tab could not be selected for tracking entry."
            )
        filled_pages = _fill_tractor_asn_tracking_all_pack_pages(page, 0, tracking)
        if filled_pages <= 0:
            filled_pages = _fill_pack_pages_for_order(page, 0, tracking)
            if filled_pages > 0:
                fill_path = "order-tab-pack-pages-fallback"

    if filled_pages <= 0:
        raise RuntimeError("Could not fill ASN tracking input(s) for current order (no pack pages updated).")
    asn_shape = "single-page" if filled_pages == 1 else "multi-page"
    print(
        f"ASN tracking: {asn_shape}, filled {filled_pages} pack page(s) "
        f"for current order via {fill_path}."
    )

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


def process_orders_individually(
    page: Page,
    tracking_by_po: dict[str, str],
    *,
    submit: bool,
    partner_name: str = "Tractor Supply Dropship",
) -> tuple[int, int]:
    """
    Per-PO flow:
    Tractor: transactions list -> open PO -> ASN -> Invoice from ASN -> next PO (new list pass).
    Grainger: transactions list -> open PO -> ACK -> stay on/open order document -> ASN -> Invoice
    (From PO) -> next PO. Post-ACK redirects to the list are undone so ASN runs on the order page.
    """
    is_grainger = "grainger" in (partner_name or "").lower()
    attempted = 0
    completed = 0
    processed_po: set[str] = set()
    max_iterations = 200 if is_grainger else max(1, len(tracking_by_po))
    for _ in range(max_iterations):
        anchor_doc_url = ""
        open_ready_for_shipment(page, partner_name=partner_name)
        if is_grainger:
            po = _open_next_order_from_results(page, processed_po)
            if po is None:
                print("No additional open Grainger orders found in filtered transactions results.")
                break
            attempted += 1
            processed_po.add(po)
            print(f"\n=== Grainger order {po}: opened from top of filtered list ===")
            anchor_doc_url = (page.url or "").strip()
        else:
            picked = _open_next_tracked_order_from_results(page, tracking_by_po, processed_po)
            if picked is None:
                print("No additional Shipment/Open POs on transactions pages matched CSV tracking.")
                break
            po, tracking = picked
            attempted += 1
            processed_po.add(po)
            print(f"\n=== PO {po}: matched from transactions list -> CSV tracking; processing ===")
        try:
            if is_grainger:
                used_existing_ack = _grainger_try_open_existing_acknowledgment_flow(page)
                if used_existing_ack:
                    if not _wait_grainger_ack_document_ready_for_shipment(page, timeout_ms=90_000):
                        raise RuntimeError(
                            "Opened existing acknowledgment but page did not show ack content (SKU / poAck2)."
                        )
                    print("Grainger: on acknowledgment document; proceeding to shipment.")
                else:
                    _create_grainger_ack_for_open_order(page, submit=submit)
                    if submit:
                        _ensure_grainger_on_order_view_after_ack_send(
                            page, anchor_doc_url, po, partner_name=partner_name
                        )
                tracking_match = _grainger_csv_tracking_for_open_order(po, tracking_by_po)
                tracking_source = "opened order PO -> CSV map"
                if not tracking_match:
                    tracking_match = _extract_tracking_match_from_page(page, tracking_by_po)
                    tracking_source = "page text -> CSV map"
                if not tracking_match:
                    print(
                        f"Grainger order {po}: no CSV tracking for normalized PO "
                        f"{normalize_po(po)!r} (and no map match from page text); skipping shipment/invoice."
                    )
                    continue
                csv_po, tracking = tracking_match
                print(f"Grainger order {po}: using CSV PO {csv_po} with tracking ({tracking_source}).")
                _create_grainger_asn_for_open_order(page, tracking, submit=submit)
                _create_grainger_invoice_from_po_for_open_order(page, submit=submit)
            else:
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


def _asn_order_tab_locator(ctx, order_idx: int = 0):
    """SPS ASN Order tab: div[role=tab][data-key=order] wrapping data-testid=tab-asn_order."""
    tab = ctx.locator("div[role='tab'][data-key='order']").nth(order_idx)
    if tab.count() > 0:
        return tab
    return ctx.locator("[data-testid='tab-asn_order']").nth(order_idx).locator(
        "xpath=ancestor::*[@role='tab' and @data-key='order'][1]"
    )


def _asn_order_tab_is_selected(page: Page, order_idx: int = 0) -> bool:
    for ctx in _contexts(page):
        tab = _asn_order_tab_locator(ctx, order_idx)
        if tab.count() == 0:
            continue
        try:
            if (tab.get_attribute("aria-selected") or "").lower() == "true":
                return True
            if "active" in (tab.get_attribute("class") or "").lower():
                return True
        except Exception:
            continue
    return False


def _ensure_asn_order_tab_selected(page: Page, order_idx: int = 0, *, timeout_ms: int = 20_000) -> bool:
    """Click Order tab (Header -> Order) and wait for pack tracking inputs to appear."""
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        if _asn_order_tab_is_selected(page, order_idx):
            for ctx in _contexts(page):
                trk = ctx.locator(
                    f"input[data-testid^='asn.order.{order_idx}.packInfo.']"
                    "[data-testid$='trackingNumber-input__input']"
                ).first
                try:
                    if trk.count() > 0 and trk.is_visible():
                        return True
                except Exception:
                    pass
        clear_click_blockers(page)
        clicked = False
        for ctx in _contexts(page):
            tab = _asn_order_tab_locator(ctx, order_idx)
            if tab.count() == 0:
                continue
            try:
                tab.scroll_into_view_if_needed()
            except Exception:
                pass
            for click_fn in (
                lambda t: t.click(timeout=2500),
                lambda t: t.click(timeout=2500, force=True),
                lambda t: t.evaluate("el => el.click()"),
            ):
                try:
                    click_fn(tab)
                    clicked = True
                    break
                except Exception:
                    continue
            if clicked:
                break
        if not clicked:
            try:
                page.get_by_role("tab", name=re.compile(r"^Order$", re.I)).first.click(timeout=2500)
                clicked = True
            except Exception:
                pass
        page.wait_for_timeout(400)
        if _asn_order_tab_is_selected(page, order_idx):
            for ctx in _contexts(page):
                trk = ctx.locator(
                    f"input[data-testid^='asn.order.{order_idx}.packInfo.']"
                    "[data-testid$='trackingNumber-input__input']"
                ).first
                try:
                    if trk.count() > 0 and trk.is_visible():
                        print("ASN: Order tab selected; pack tracking field visible.")
                        return True
                except Exception:
                    pass
        page.wait_for_timeout(250)
    print("WARN: ASN Order tab or pack tracking field did not become ready in time.")
    return False


def _click_asn_order_tab(page: Page, order_idx: int) -> None:
    """For multi-SKU cards, switch from Header -> Order tab for the given ASN row."""
    if not _ensure_asn_order_tab_selected(page, order_idx):
        raise RuntimeError("Could not select ASN Order tab.")


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


def _pack_info_next_page_button(page: Page, order_idx: int = 0):
    """
    Next-page chevron inside PACK INFO (1, 2, >), not footer list pagination.
    Scoped from the visible Carrier Tracking # input for this order.
    """
    for ctx in _contexts(page):
        inputs = ctx.locator(
            f"input[data-testid^='asn.order.{order_idx}.packInfo.']"
            "[data-testid$='trackingNumber-input__input'], "
            "input[aria-label='Carrier Tracking #']"
        )
        for i in range(min(inputs.count(), 8)):
            inp = inputs.nth(i)
            try:
                if not inp.is_visible():
                    continue
            except Exception:
                continue
            section = inp.locator(
                "xpath=ancestor::*["
                ".//button[@aria-label='Go to Next Page' or @title='Go to Next Page']"
                " or .//i[contains(@class,'chevron-right')]"
                "][1]"
            )
            for sel in (
                "button[aria-label='Go to Next Page'][title='Go to Next Page']",
                "button.sps-button__clickable-element:has(i.sps-icon-chevron-right)",
                "button:has(i.sps-icon-chevron-right)",
            ):
                btn = section.locator(sel).first
                if btn.count() > 0:
                    return btn
    return page.locator("button.sps-button__clickable-element:has(i.sps-icon-chevron-right)").first


def _pack_info_next_page_enabled(btn) -> bool:
    try:
        if btn.count() == 0 or not btn.is_visible():
            return False
        if not btn.is_enabled():
            return False
        disabled = (btn.get_attribute("disabled") or "").strip().lower()
        if disabled in ("", "false"):
            pass
        else:
            return False
        cls = (btn.get_attribute("class") or "").lower()
        if "disabled" in cls:
            return False
        icon = btn.locator("i.sps-icon-chevron-right").first
        if icon.count() > 0:
            icls = (icon.get_attribute("class") or "").lower()
            if "disabled" in icls:
                return False
        return True
    except Exception:
        return False


def _fill_tractor_asn_tracking_all_pack_pages(page: Page, order_idx: int, tracking: str) -> int:
    """
    On the Order tab: fill Carrier Tracking # on page 1, click >, fill page 2, repeat until
    the pack-info next arrow is disabled (last page).
    """
    filled_pages = 0
    visited_pack: set[int] = set()
    max_steps = 30

    for step in range(max_steps):
        clear_click_blockers(page)
        filled_this_round = False

        for ctx in _contexts(page):
            loc = ctx.locator(
                f"input[data-testid^='asn.order.{order_idx}.packInfo.']"
                "[data-testid$='trackingNumber-input__input']"
            )
            for i in range(loc.count()):
                inp = loc.nth(i)
                try:
                    if not inp.is_visible():
                        continue
                except Exception:
                    continue
                testid = inp.get_attribute("data-testid") or ""
                m = re.search(rf"packInfo\.(\d+)\.", testid)
                pack_idx = int(m.group(1)) if m else i
                if pack_idx in visited_pack:
                    continue
                if _fill_tracking_input(inp, tracking):
                    visited_pack.add(pack_idx)
                    filled_pages += 1
                    filled_this_round = True
                    print(f"ASN pack page {pack_idx + 1}: filled tracking.")
                break
            if filled_this_round:
                break

        next_btn = _pack_info_next_page_button(page, order_idx)
        if not _pack_info_next_page_enabled(next_btn):
            break
        try:
            next_btn.scroll_into_view_if_needed()
        except Exception:
            pass
        try:
            next_btn.click(timeout=2000)
        except Exception:
            try:
                next_btn.evaluate("el => el.click()")
            except Exception:
                try:
                    next_btn.click(timeout=2000, force=True)
                except Exception:
                    break
        page.wait_for_timeout(350)

    return filled_pages


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
        next_btns = page.locator(
            "button[aria-label='Go to Next Page'][title='Go to Next Page'], "
            "button[aria-label='Go to Next Page'], "
            "button[title='Go to Next Page'], "
            "button:has(i.sps-icon-chevron-right), "
            "[role='button']:has(i.sps-icon-chevron-right), "
            "button.sps-btn-icon:has(i.sps-icon-chevron-right), "
            "i.sps-icon-chevron-right"
        )
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
    parser.add_argument(
        "--partner",
        default="Tractor Supply Dropship",
        help="SPS Partner filter for Advanced Search (e.g., 'Tractor Supply Dropship', 'Grainger').",
    )
    return parser.parse_args()


def run_sps_partner_tracking_on_page(
    page: Page,
    context: BrowserContext,
    *,
    csv_path: Path,
    partner_name: str,
    submit: bool,
    storage_path: Path,
    headless: bool = False,
    interactive_login: bool = False,
    ensure_session: bool = True,
) -> tuple[int, int]:
    """
    Run one partner's tracking/invoicing flow on an existing SPS browser session.
    Returns (completed_count, attempted_count).
    """
    tracking_by_po = load_tracking_map(csv_path)
    if not tracking_by_po:
        print(f"No tracking rows loaded from {csv_path}; skipping {partner_name}.")
        return 0, 0

    if interactive_login:
        interactive_login_then_save(page, context, storage_path)
    elif ensure_session:
        ensure_sps_session(
            page,
            context,
            storage_path,
            headless=headless,
            allow_manual=not headless,
        )

    open_ready_for_shipment(page, partner_name=partner_name)
    completed, attempted = process_orders_individually(
        page,
        tracking_by_po,
        submit=bool(submit),
        partner_name=partner_name,
    )
    print(f"{partner_name}: {completed}/{attempted} processed.")
    return completed, attempted


def main() -> int:
    args = parse_args()
    print("SPS tracking: session validation v2 (transactions page must load before continuing).")
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
                if state_path.is_file():
                    try:
                        state_path.unlink()
                        print(f"Removed old session file before interactive login: {state_path}")
                    except OSError as exc:
                        print(f"Warning: could not remove old session file: {exc}")
                context = browser.new_context()
            elif state_path.is_file():
                print(f"Using Playwright storage state: {state_path}")
                try:
                    context = browser.new_context(storage_state=str(state_path))
                except Exception as ex:
                    print(f"WARN: Could not load storage state ({ex}); starting a fresh browser profile.")
                    context = browser.new_context()
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
                if args.interactive_login:
                    interactive_login_then_save(page, context, state_path)
                    did_auto_interactive_login = True
                else:
                    ensure_sps_session(
                        page,
                        context,
                        state_path,
                        headless=bool(args.headless),
                        allow_manual=not bool(args.headless),
                    )
                open_ready_for_shipment(page, partner_name=args.partner)
                print(f"After Transactions/Advanced Search: {page.url}")
                print("STEP 2: Process each PO individually (Shipment then Invoice from ASN)...")
                completed, attempted = process_orders_individually(
                    page,
                    tracking_by_po,
                    submit=bool(args.submit),
                    partner_name=args.partner,
                )
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
                )
                if can_retry_interactive and not did_auto_interactive_login:
                    did_auto_interactive_login = True
                    print(
                        "Detected expired/missing SPS login session. "
                        "Clearing stale session and retrying sign-in..."
                    )
                    _invalidate_stale_sps_session(context, state_path)
                    if not login_with_env_credentials_then_save(page, context, state_path):
                        interactive_login_then_save(page, context, state_path)
                    open_ready_for_shipment(page, partner_name=args.partner)
                    print(f"After Transactions/Advanced Search: {page.url}")
                    print("STEP 2: Process each PO individually (Shipment then Invoice from ASN)...")
                    completed, attempted = process_orders_individually(
                        page,
                        tracking_by_po,
                        submit=bool(args.submit),
                        partner_name=args.partner,
                    )
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
