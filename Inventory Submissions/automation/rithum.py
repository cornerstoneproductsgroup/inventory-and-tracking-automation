from datetime import datetime
from pathlib import Path

from automation.commercehub_timeouts import (
    chain_fast,
    navigation_timeout_ms,
    rithum_ibl_timeout_ms,
    rithum_profile_timeout_ms,
)
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from automation.config import load_settings


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _save_screenshot(page, name: str) -> None:
    shots_dir = Path("screenshots")
    shots_dir.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(shots_dir / f"{_timestamp()}_{name}.png"), full_page=True)


def _click_first_available_profile(page, timeout_ms: int) -> None:
    profile_candidates = [
        "button:has-text('Select')",
        "input[type='submit'][value*='Select']",
        "a:has-text('Select')",
        "button:has-text('Continue')",
        "input[type='submit'][value*='Continue']",
        "a:has-text('Continue')",
    ]

    for selector in profile_candidates:
        try:
            locator = page.locator(selector).first
            if locator.is_visible(timeout=1500):
                locator.click(timeout=timeout_ms)
                return
        except Exception:
            continue


def _perform_login(page, username: str, password: str, timeout_ms: int) -> None:
    # CommerceHub can present either a two-step identifier/password flow or legacy single-page login.
    if "account.commercehub.com/u/login/identifier" in page.url:
        page.locator("input[name='username']").fill(username)
        page.locator("button._button-login-id").click()
        page.locator("input[name='password']").wait_for(state="visible", timeout=timeout_ms)
        page.locator("input[name='password']").fill(password)
        page.locator("button._button-login-password").click()
        page.wait_for_load_state("domcontentloaded")
        return

    username_selectors = ["#j_username", "input[name='j_username']", "#username", "input[type='email']"]
    password_selectors = ["#j_password", "input[name='j_password']", "#password", "input[type='password']"]

    username_filled = False
    for selector in username_selectors:
        locator = page.locator(selector).first
        if locator.count() > 0:
            locator.fill(username)
            username_filled = True
            break

    password_filled = False
    for selector in password_selectors:
        locator = page.locator(selector).first
        if locator.count() > 0:
            locator.fill(password)
            password_filled = True
            break

    if not username_filled or not password_filled:
        raise RuntimeError("Could not find login fields on Rithum page.")

    submit_candidates = [
        "#loginButton",
        "input[type='submit'][name='submit']",
        "input[type='submit'][value*='Log In']",
        "input[type='submit'][value*='Login']",
        "button[type='submit']",
        "button:has-text('Log In')",
        "button:has-text('Login')",
        "button:has-text('Continue')",
        "input[type='submit']",
    ]

    submitted = False
    for selector in submit_candidates:
        locator = page.locator(selector).first
        if locator.count() > 0:
            locator.click()
            submitted = True
            break

    if not submitted:
        page.keyboard.press("Enter")

    page.wait_for_load_state("domcontentloaded")


def run_rithum_inventory_on_authenticated_page(page, settings) -> None:
    """
    Submit inventory update on DSM after the user is already logged in and has a session.
    If the profile chooser is visible (fresh login / hub landing), clicks Cornerstone (or first profile).
    If it is not shown — e.g. chained run after Lowe's login already opened a session — skips straight to IBL.
    """
    chain_fast_mode = chain_fast()
    nav_ms = navigation_timeout_ms()
    try:
        if chain_fast_mode:
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(350)
        else:
            try:
                page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:
                page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(900)

        profile_ms = rithum_profile_timeout_ms()
        try:
            page.locator("a.application-identity-item").first.wait_for(state="visible", timeout=profile_ms)
            profile_link = page.locator("a.application-identity-item").filter(
                has_text="Cornerstone Products Group"
            ).first
            if profile_link.count() > 0 and profile_link.is_visible(timeout=min(5000, profile_ms)):
                profile_link.click(timeout=settings.timeout_ms)
            else:
                _click_first_available_profile(page, settings.timeout_ms)
            page.wait_for_load_state("domcontentloaded")
        except Exception:
            print(
                "Rithum inventory: profile chooser not visible (session already inside app). "
                "Opening inventory update directly."
            )

        page.goto(
            "https://dsm.commercehub.com/dsm/gotoUpdateInventory.do",
            wait_until="domcontentloaded",
            timeout=nav_ms,
        )
        ibl_wait = rithum_ibl_timeout_ms()
        page.locator("#selectAllIBL").wait_for(state="visible", timeout=ibl_wait)
        page.locator("#selectAllIBL").check()
        page.locator("#iblsubmit").click()
        page.wait_for_load_state("domcontentloaded")

        page.locator("input[name='skudates'][value='1']").wait_for(state="visible", timeout=ibl_wait)
        page.locator("input[name='skudates'][value='1']").check()
        _save_screenshot(page, "pre_submit")

        page.locator("#submitButton").wait_for(state="visible", timeout=ibl_wait)
        page.locator("#submitButton").click()
        page.wait_for_load_state("domcontentloaded")
        _save_screenshot(page, "submitted")

        print("Rithum inventory update submitted successfully.")
    except PlaywrightTimeoutError as exc:
        _save_screenshot(page, "timeout_error")
        raise RuntimeError(f"Timed out during automation: {exc}") from exc
    except Exception as exc:
        _save_screenshot(page, "general_error")
        raise RuntimeError(f"Automation failed: {exc}") from exc


def run_rithum_inventory_update() -> None:
    settings = load_settings()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=settings.headless)
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(settings.timeout_ms)

        try:
            page.goto(settings.rithum_url, wait_until="domcontentloaded")
            _perform_login(page, settings.rithum_username, settings.rithum_password, settings.timeout_ms)
            _save_screenshot(page, "after_login")

            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(3000)

            run_rithum_inventory_on_authenticated_page(page, settings)
        except PlaywrightTimeoutError as exc:
            _save_screenshot(page, "timeout_error")
            raise RuntimeError(f"Timed out during automation: {exc}") from exc
        except Exception as exc:
            _save_screenshot(page, "general_error")
            raise RuntimeError(f"Automation failed: {exc}") from exc
        finally:
            context.close()
            browser.close()
