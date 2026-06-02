import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from automation.config import load_sps_settings

# Same path as run_sps_tracking.py (reuse session after inventory).
_SPS_PLAYWRIGHT_STORAGE = Path(__file__).resolve().parent.parent / "sps_playwright_storage.json"
_TRANSACTIONS_LIST_URL = "https://commerce.spscommerce.com/fulfillment/transactions/list/"

_CREATE_NEW_SELECTORS: tuple[str, ...] = (
    "button[data-testid='create-new-document-button']",
    "button.sps-button__clickable-element:has-text('Create New')",
    "button.sps-button:has-text('Create New')",
    "button:has-text('Create New')",
    "button[title='Create New']",
    "role=button[name='Create New']",
    "[role='button']:has-text('Create New')",
)


def _persist_sps_session(context, label: str = "") -> None:
    """Write cookies/local storage so run_sps_tracking can reuse this login."""
    try:
        _SPS_PLAYWRIGHT_STORAGE.parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(_SPS_PLAYWRIGHT_STORAGE))
        suffix = f" ({label})" if label else ""
        print(f"Saved SPS browser session for tracking reuse{suffix}: {_SPS_PLAYWRIGHT_STORAGE}")
    except OSError as exc:
        print(f"Warning: could not save SPS storage state ({_SPS_PLAYWRIGHT_STORAGE}): {exc}")


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _save_screenshot(page, name: str) -> None:
    shots_dir = Path("screenshots")
    shots_dir.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(shots_dir / f"{_timestamp()}_sps_{name}.png"), full_page=True)


def _get_frame(page, selector: str, timeout_ms: int = 5000, detect_ms: int = 3000):
    """Return the first frame (main page or iframe) where selector is attached."""
    # Try main page first
    try:
        page.locator(selector).first.wait_for(state="attached", timeout=detect_ms)
        return page
    except Exception:
        pass
    # Search all iframes
    for frame in page.frames:
        try:
            frame.locator(selector).first.wait_for(state="attached", timeout=detect_ms)
            return frame
        except Exception:
            continue
    raise RuntimeError(f"Could not find '{selector}' on page or in any iframe.")


def _get_visible_context(page, selector: str, detect_ms: int = 750):
    """Return the first page/frame context where selector becomes visible quickly."""
    for ctx in [page, *page.frames]:
        try:
            ctx.locator(selector).first.wait_for(state="visible", timeout=detect_ms)
            return ctx
        except Exception:
            continue
    return None


def _contexts(page: Page) -> list[Page]:
    live: list[Page] = [page]
    for frame in page.frames:
        try:
            if frame.is_detached():
                continue
        except Exception:
            continue
        live.append(frame)
    return live


def _click_first_visible(page, selectors: list[str], detect_ms: int = 750) -> bool:
    """Click the first visible locator from the provided selector list."""
    for selector in selectors:
        for ctx in _contexts(page):
            try:
                loc = ctx.locator(selector).first
                loc.wait_for(state="visible", timeout=detect_ms)
                try:
                    loc.scroll_into_view_if_needed()
                except Exception:
                    pass
                try:
                    loc.click(timeout=detect_ms)
                except Exception:
                    loc.click(timeout=detect_ms, force=True)
                return True
            except Exception:
                continue
    return False


def _is_login_page_visible(page: Page) -> bool:
    for sel in ("input[name='username']", "input[name='password']", "button._button-login-id"):
        try:
            loc = page.locator(sel).first
            if loc.is_visible():
                return True
        except Exception:
            continue
    return False


def _wait_for_transactions_page_ready(page: Page, *, timeout_ms: int) -> None:
    """Wait until the transactions SPA is interactive (same signals as tracking automation)."""
    deadline = time.monotonic() + timeout_ms / 1000.0
    ready_selectors = (
        "button[data-testid='create-new-document-button']",
        "button:has-text('Create New')",
        "button:has-text('Advanced Search')",
        "input[placeholder*='Search here for a document']",
        "button[data-testid='advSearchBottomSearchButton']",
        "table",
        "tbody",
    )
    while time.monotonic() < deadline:
        if _is_login_page_visible(page):
            raise RuntimeError(
                "Not logged into SPS Commerce on the transactions page. "
                "Check SPS_USERNAME/SPS_PASSWORD in Inventory Submissions/.env and complete MFA if prompted."
            )
        for sel in ready_selectors:
            for ctx in _contexts(page):
                try:
                    loc = ctx.locator(sel)
                    if loc.count() > 0 and loc.first.is_visible():
                        return
                except Exception:
                    continue
        page.wait_for_timeout(250)
    raise RuntimeError("Transactions page did not become ready in time (Create New / search UI never appeared).")


def _clear_lightweight_blockers(page: Page) -> None:
    """Close tour/announcement overlays that hide the toolbar (not real Create New modals)."""
    _click_first_visible(
        page,
        [
            "button[aria-label='Close']",
            "button[title='Close']",
            "button:has-text('Close')",
            "button:has-text('Dismiss')",
            "button:has-text('Got it')",
            "[data-testid='modalCancelBtn']",
        ],
        detect_ms=800,
    )


def _click_create_new_on_transactions(page: Page, *, timeout_ms: int) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        _clear_lightweight_blockers(page)
        if _click_first_visible(page, list(_CREATE_NEW_SELECTORS), detect_ms=2500):
            return True
        page.wait_for_timeout(500)
    return False


def _open_transactions_list(page: Page, *, timeout_ms: int) -> None:
    last_err: Exception | None = None
    for attempt in (1, 2):
        try:
            page.goto(_TRANSACTIONS_LIST_URL, wait_until="domcontentloaded", timeout=120_000)
            page.wait_for_timeout(800)
            if _is_login_page_visible(page):
                raise RuntimeError("Redirected to SPS sign-in while opening transactions list.")
            _wait_for_transactions_page_ready(page, timeout_ms=timeout_ms)
            return
        except Exception as exc:
            last_err = exc
            print(f"SPS inventory: transactions list attempt {attempt} failed: {exc}")
            _clear_lightweight_blockers(page)
            page.wait_for_timeout(1000)
    raise RuntimeError(f"Could not open SPS transactions list: {last_err}")


def _perform_sps_login(page, username: str, password: str, timeout_ms: int) -> None:
    # Step 1: Enter username and click Next.
    page.locator("input[name='username']").wait_for(state="visible", timeout=timeout_ms)
    page.locator("input[name='username']").fill(username)
    page.locator("button._button-login-id").click()

    # Step 2: Enter password and click Next.
    page.locator("input[name='password']").wait_for(state="visible", timeout=timeout_ms)
    page.locator("input[name='password']").fill(password)
    page.locator("button._button-login-password").click()
    page.wait_for_load_state("domcontentloaded")


def run_sps_inventory_on_authenticated_page(page: Page, context) -> None:
    """Submit Tractor Supply inventory on an already-signed-in SPS transactions page."""
    settings = load_sps_settings()
    today = datetime.now().strftime("%m/%d/%Y")
    page.set_default_timeout(settings.timeout_ms)

    tx_ready_ms = max(60_000, int(settings.timeout_ms) * 3)
    _open_transactions_list(page, timeout_ms=tx_ready_ms)
    _save_screenshot(page, "transactions_tab")

    create_new_ms = max(45_000, int(settings.timeout_ms) * 2)
    clicked = _click_create_new_on_transactions(page, timeout_ms=create_new_ms)
    if not clicked:
        _save_screenshot(page, "create_new_not_found")
        raise RuntimeError(
            "Could not find Create New button on transactions page "
            f"(waited {create_new_ms // 1000}s). "
            f"See screenshots/sps_*_create_new_not_found.png and confirm the toolbar is visible at "
            f"{_TRANSACTIONS_LIST_URL}"
        )

    f = _get_frame(page, "[data-testid='createNewDocPartnerSelector-value']", detect_ms=settings.timeout_ms)
    f.locator("[data-testid='createNewDocPartnerSelector-value']").click()
    option = f.locator("span", has_text="Tractor Supply Dropship").first
    option.wait_for(state="visible", timeout=settings.timeout_ms)
    option.click()
    _save_screenshot(page, "partner_selected")

    f = _get_frame(page, "label.sps-checkable__label", detect_ms=3000)
    checkbox = f.locator("label.sps-checkable__label", has_text="I don't have a source document.").first
    checkbox.wait_for(state="visible", timeout=3000)
    checkbox.click()
    _save_screenshot(page, "no_source_doc_checked")

    f = _get_frame(page, "[data-testid='createNewDocTemplateSelector-value']", detect_ms=3000)
    f.locator("[data-testid='createNewDocTemplateSelector-value']").click()
    template = f.locator("span", has_text="Inventory Main").first
    template.wait_for(state="visible", timeout=3000)
    template.click()
    _save_screenshot(page, "template_selected")

    f = _get_frame(page, "button[data-testid='modalOkBtn'][title='Create New']", detect_ms=3000)
    f.locator("button[data-testid='modalOkBtn'][title='Create New']").click()
    page.wait_for_load_state("load")
    _save_screenshot(page, "form_loaded")

    try:
        f = _get_frame(page, "button[data-testid='dataEntryCard__expanding']", 5000)
        btn = f.locator("button[data-testid='dataEntryCard__expanding']").first
        btn.wait_for(state="visible", timeout=5000)
        btn.click()
        _save_screenshot(page, "short_expanded")
    except Exception:
        pass

    f = _get_frame(
        page, "input[data-testid='inventoryAdvice.header.reportDate2-input_date_input']", settings.timeout_ms
    )
    date_field = f.locator("input[data-testid='inventoryAdvice.header.reportDate2-input_date_input']")
    date_field.wait_for(state="visible", timeout=settings.timeout_ms)
    date_field.click(click_count=3)
    date_field.fill(today)
    f.locator("body").press("Tab")
    _save_screenshot(page, "date_set")

    f = _get_frame(page, "button[data-testid='dataEntry_document-actions-send']", settings.timeout_ms)
    send_btn = f.locator("button[data-testid='dataEntry_document-actions-send']")
    send_btn.wait_for(state="visible", timeout=settings.timeout_ms)
    send_btn.click()
    _save_screenshot(page, "send_clicked")

    f = _get_frame(page, "button[data-testid='modalOkBtn'][title='Continue']", settings.timeout_ms)
    continue_btn = f.locator("button[data-testid='modalOkBtn'][title='Continue']")
    continue_btn.wait_for(state="visible", timeout=settings.timeout_ms)
    continue_btn.click()
    page.wait_for_load_state("load")
    _save_screenshot(page, "submitted")

    print(f"SPS Commerce (Tractor Supply) inventory update submitted successfully for {today}.")
    _persist_sps_session(context, label="after inventory submit")


def run_sps_inventory_update() -> None:
    settings = load_sps_settings()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=settings.headless,
            args=[
                "--disable-features=BlockThirdPartyCookies,TrackingProtection3pcd",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto(settings.sps_url, wait_until="load")
            _perform_sps_login(page, settings.sps_username, settings.sps_password, settings.timeout_ms)
            _save_screenshot(page, "after_login")
            _persist_sps_session(context, label="after login")
            run_sps_inventory_on_authenticated_page(page, context)
        except PlaywrightTimeoutError as exc:
            _save_screenshot(page, "timeout_error")
            raise RuntimeError(f"SPS timed out during automation: {exc}") from exc
        except Exception as exc:
            _save_screenshot(page, "general_error")
            raise RuntimeError(f"SPS automation failed: {exc}") from exc
        finally:
            context.close()
            browser.close()
