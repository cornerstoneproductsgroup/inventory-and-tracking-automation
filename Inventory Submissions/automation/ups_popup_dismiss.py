"""Dismiss UPS.com startup popups (cookies, location, extension prompts) before automation."""

from __future__ import annotations

import os
from typing import Any, Callable


def _log_default(msg: str) -> None:
    print(f"[ups] {msg}", flush=True)


def _popup_selectors(cfg: dict[str, Any], key: str, defaults: tuple[str, ...]) -> list[str]:
    raw = (cfg.get("selectors", {}).get(key) or "").strip()
    out: list[str] = []
    if raw:
        out.extend(s.strip() for s in raw.split(",") if s.strip())
    out.extend(defaults)
    seen: set[str] = set()
    deduped: list[str] = []
    for sel in out:
        if sel not in seen:
            seen.add(sel)
            deduped.append(sel)
    return deduped


def _try_click(page, selectors: list[str], *, label: str, log: Callable[[str], None]) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if not loc.is_visible(timeout=600):
                continue
            try:
                loc.scroll_into_view_if_needed(timeout=1500)
            except Exception:
                pass
            loc.click(timeout=3000)
            log(f"Dismissed {label} ({sel!r}).")
            return True
        except Exception:
            continue
    return False


def _dismiss_cookie_banner_js(page, log: Callable[[str], None]) -> bool:
    try:
        clicked = page.evaluate(
            """() => {
            const closeSelectors = [
                '#onetrust-close-btn-container button',
                'button.onetrust-close-btn-handler',
                '.onetrust-close-btn-handler',
                '#close-pc-btn-handler',
                'button.ot-close-icon',
            ];
            for (const sel of closeSelectors) {
                const el = document.querySelector(sel);
                if (!el) continue;
                const r = el.getBoundingClientRect();
                if (r.width < 2 || r.height < 2) continue;
                const st = window.getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden') continue;
                el.click();
                return sel;
            }
            const banners = document.querySelectorAll(
                '#onetrust-banner-sdk, #onetrust-pc-sdk, [id*="cookie" i], [class*="cookie" i]'
            );
            for (const banner of banners) {
                const buttons = banner.querySelectorAll('button, [role="button"]');
                for (const btn of buttons) {
                    const t = (btn.textContent || '').trim().toLowerCase();
                    const aria = (btn.getAttribute('aria-label') || '').trim().toLowerCase();
                    if (aria === 'close' || t === '×' || t === 'x' || aria.includes('close')) {
                        btn.click();
                        return 'cookie-close-in-banner';
                    }
                }
            }
            return null;
        }"""
        )
        if clicked:
            log(f"Dismissed cookie banner (JS: {clicked}).")
            return True
    except Exception:
        pass
    return False


def dismiss_ups_startup_popups(
    page,
    cfg: dict[str, Any],
    *,
    log: Callable[[str], None] | None = None,
) -> None:
    """
    Best-effort dismiss of overlays that block UPS automation.

    Cookie banner: close (X) on the cookie/consent box (not Accept).
    Location prompt: Never allow / Block.
    Extension install promo: close or dismiss when visible in page DOM.
    """
    emit = log or _log_default
    timing = cfg.get("timing") or {}
    wait_ms = int(timing.get("popup_wait_ms") or os.environ.get("UPS_POPUP_WAIT_MS") or 1800)
    rounds = int(timing.get("popup_dismiss_rounds") or os.environ.get("UPS_POPUP_DISMISS_ROUNDS") or 4)

    try:
        page.wait_for_timeout(max(400, wait_ms))
    except Exception:
        pass

    cookie_close = _popup_selectors(
        cfg,
        "cookie_close",
        (
            "#onetrust-close-btn-container button",
            "button.onetrust-close-btn-handler",
            ".onetrust-close-btn-handler",
            "#close-pc-btn-handler",
            "button.ot-close-icon",
            "#onetrust-pc-sdk button[aria-label='Close']",
            "#onetrust-banner-sdk button[aria-label='Close']",
            "[id*='onetrust'] button[aria-label='Close']",
        ),
    )
    location_deny = _popup_selectors(
        cfg,
        "location_deny",
        (
            "button:has-text('Never allow')",
            "button:has-text('Block')",
            "button:has-text('Don\\'t allow')",
            "[role='button']:has-text('Never allow')",
        ),
    )
    extension_dismiss = _popup_selectors(
        cfg,
        "extension_dismiss",
        (
            "button[aria-label='Close']",
            "button[aria-label='Dismiss']",
            "[role='dialog'] button:has-text('×')",
            "[role='dialog'] button:has-text('Close')",
        ),
    )

    for attempt in range(1, rounds + 1):
        dismissed = False

        if _try_click(page, location_deny, label="location prompt", log=emit):
            dismissed = True

        if _try_click(page, cookie_close, label="cookie banner (close)", log=emit):
            dismissed = True
        elif _dismiss_cookie_banner_js(page, emit):
            dismissed = True

        if _try_click(page, extension_dismiss, label="extension / browser promo", log=emit):
            dismissed = True

        # Cookie Settings modal sometimes opens — close that too.
        if _try_click(
            page,
            _popup_selectors(
                cfg,
                "cookie_settings_close",
                (
                    "#onetrust-pc-sdk button.onetrust-close-btn-handler",
                    "#onetrust-pc-sdk .ot-close-icon",
                    "#onetrust-pc-sdk button[aria-label='Close']",
                ),
            ),
            label="cookie settings panel",
            log=emit,
        ):
            dismissed = True

        if not dismissed:
            break
        try:
            page.wait_for_timeout(450)
        except Exception:
            pass

    # Escape can close stray modals without accepting.
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
    except Exception:
        pass
