from datetime import datetime
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from automation.config import load_settings

# Same path as run_sps_tracking.py (reuse session after inventory).
_SPS_PLAYWRIGHT_STORAGE = Path(__file__).resolve().parent.parent / "sps_playwright_storage.json"


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


def _click_first_visible(page, selectors: list[str], detect_ms: int = 750) -> bool:
    """Click the first visible locator from the provided selector list."""
    for selector in selectors:
        ctx = _get_visible_context(page, selector, detect_ms=detect_ms)
        if ctx is None:
            continue
        try:
            ctx.locator(selector).first.click()
            return True
        except Exception:
            continue
    return False


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


def run_sps_inventory_update() -> None:
    settings = load_settings()
    today = datetime.now().strftime("%m/%d/%Y")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=settings.headless)
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(settings.timeout_ms)

        try:
            # ── Login ──────────────────────────────────────────────────────────
            page.goto(settings.sps_url, wait_until="load")
            _perform_sps_login(page, settings.sps_username, settings.sps_password, settings.timeout_ms)
            _save_screenshot(page, "after_login")
            _persist_sps_session(context, label="after login")

            # ── Navigate directly to Transactions list ─────────────────────────
            # Skip the dashboard tile entirely — go straight to the transactions URL.
            page.goto("https://commerce.spscommerce.com/fulfillment/transactions/list/", wait_until="domcontentloaded")
            _save_screenshot(page, "transactions_tab")

            # ── Click Create New (opens the new document dialog) ──────────────
            # Give the SPA a short, fixed window to finish rendering, then click immediately.
            page.wait_for_timeout(5000)
            clicked = _click_first_visible(
                page,
                [
                    "button.sps-button__clickable-element:has-text('Create New')",
                    "button[data-testid='create-new-document-button']",
                    "button[title='Create New']",
                    "role=button[name='Create New']",
                ],
                detect_ms=1000,
            )

            if not clicked:
                _save_screenshot(page, "create_new_not_found")
                raise RuntimeError("Could not find Create New button on transactions page.")

            # ── Open Partner dropdown and select Tractor Supply Dropship ───────
            # Use full timeout here — the modal takes a few seconds to appear after clicking Create New.
            f = _get_frame(page, "[data-testid='createNewDocPartnerSelector-value']", detect_ms=settings.timeout_ms)
            f.locator("[data-testid='createNewDocPartnerSelector-value']").click()
            option = f.locator("span", has_text="Tractor Supply Dropship").first
            option.wait_for(state="visible", timeout=settings.timeout_ms)
            option.click()
            _save_screenshot(page, "partner_selected")

            # ── Check "I don't have a source document" ─────────────────────────
            f = _get_frame(page, "label.sps-checkable__label", detect_ms=3000)
            checkbox = f.locator("label.sps-checkable__label", has_text="I don't have a source document.").first
            checkbox.wait_for(state="visible", timeout=3000)
            checkbox.click()
            _save_screenshot(page, "no_source_doc_checked")

            # ── Open template dropdown and select Inventory Main ───────────────
            f = _get_frame(page, "[data-testid='createNewDocTemplateSelector-value']", detect_ms=3000)
            f.locator("[data-testid='createNewDocTemplateSelector-value']").click()
            template = f.locator("span", has_text="Inventory Main").first
            template.wait_for(state="visible", timeout=3000)
            template.click()
            _save_screenshot(page, "template_selected")

            # ── Click Create New in the modal ──────────────────────────────────
            f = _get_frame(page, "button[data-testid='modalOkBtn'][title='Create New']", detect_ms=3000)
            f.locator("button[data-testid='modalOkBtn'][title='Create New']").click()
            page.wait_for_load_state("load")
            _save_screenshot(page, "form_loaded")

            # ── Expand SHORT section (optional — skipped if already expanded) ──
            try:
                f = _get_frame(page, "button[data-testid='dataEntryCard__expanding']", 5000)
                btn = f.locator("button[data-testid='dataEntryCard__expanding']").first
                btn.wait_for(state="visible", timeout=5000)
                btn.click()
                _save_screenshot(page, "short_expanded")
            except Exception:
                pass

            # ── Set Report Date to today ───────────────────────────────────────
            f = _get_frame(page, "input[data-testid='inventoryAdvice.header.reportDate2-input_date_input']", settings.timeout_ms)
            date_field = f.locator("input[data-testid='inventoryAdvice.header.reportDate2-input_date_input']")
            date_field.wait_for(state="visible", timeout=settings.timeout_ms)
            date_field.click(click_count=3)
            date_field.fill(today)
            f.locator("body").press("Tab")
            _save_screenshot(page, "date_set")

            # ── Click Send button ──────────────────────────────────────────────
            f = _get_frame(page, "button[data-testid='dataEntry_document-actions-send']", settings.timeout_ms)
            send_btn = f.locator("button[data-testid='dataEntry_document-actions-send']")
            send_btn.wait_for(state="visible", timeout=settings.timeout_ms)
            send_btn.click()
            _save_screenshot(page, "send_clicked")

            # ── Click Continue on confirmation dialog ──────────────────────────
            f = _get_frame(page, "button[data-testid='modalOkBtn'][title='Continue']", settings.timeout_ms)
            continue_btn = f.locator("button[data-testid='modalOkBtn'][title='Continue']")
            continue_btn.wait_for(state="visible", timeout=settings.timeout_ms)
            continue_btn.click()
            page.wait_for_load_state("load")
            _save_screenshot(page, "submitted")

            print(f"SPS Commerce (Tractor Supply) inventory update submitted successfully for {today}.")
            _persist_sps_session(context, label="after inventory submit")
        except PlaywrightTimeoutError as exc:
            _save_screenshot(page, "timeout_error")
            raise RuntimeError(f"SPS timed out during automation: {exc}") from exc
        except Exception as exc:
            _save_screenshot(page, "general_error")
            raise RuntimeError(f"SPS automation failed: {exc}") from exc
        finally:
            context.close()
            browser.close()
