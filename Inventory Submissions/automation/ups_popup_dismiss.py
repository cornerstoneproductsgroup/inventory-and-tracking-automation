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


def overlay_still_blocking(page) -> bool:
    """True when cookie/location text still covers the page."""
    try:
        return bool(
            page.evaluate(
                """() => {
                const blob = (document.body && document.body.innerText || '').toLowerCase();
                if (blob.includes('cookie settings') && blob.includes('analytics')) return true;
                if (blob.includes('know your location')) return true;
                const ot = document.querySelector('#onetrust-banner-sdk, #onetrust-pc-sdk');
                if (ot) {
                    const st = window.getComputedStyle(ot);
                    if (st.display !== 'none' && st.visibility !== 'hidden') return true;
                }
                return false;
            }"""
            )
        )
    except Exception:
        return False


def _try_click(page, selectors: list[str], *, label: str, log: Callable[[str], None]) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if not loc.is_visible(timeout=800):
                continue
            try:
                loc.scroll_into_view_if_needed(timeout=1500)
            except Exception:
                pass
            loc.click(timeout=4000, force=True)
            log(f"Dismissed {label} ({sel!r}).")
            return True
        except Exception:
            continue
    return False


def _dismiss_ups_cookie_overlay_js(page, log: Callable[[str], None]) -> bool:
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
            const keywords = ['cookie settings', 'analytics technologies', 'privacy notice'];
            const nodes = Array.from(document.querySelectorAll('div, section, aside, dialog'));
            for (const el of nodes) {
                const text = (el.innerText || '').toLowerCase();
                if (!keywords.some((k) => text.includes(k))) continue;
                const r = el.getBoundingClientRect();
                if (r.height < 60 || r.width < 180) continue;
                const buttons = Array.from(el.querySelectorAll('button, [role="button"]'));
                for (const btn of buttons) {
                    const aria = (btn.getAttribute('aria-label') || '').toLowerCase();
                    const t = (btn.textContent || '').trim();
                    if (aria.includes('close') || t === '×' || t === 'X' || t === 'x') {
                        btn.click();
                        return 'cookie-overlay-x';
                    }
                }
                const topRight = buttons.filter((btn) => {
                    const br = btn.getBoundingClientRect();
                    return br.width <= 72 && br.height <= 72
                        && br.top - r.top < 72 && r.right - br.right < 72;
                });
                if (topRight.length) {
                    topRight[0].click();
                    return 'cookie-top-right';
                }
            }
            return null;
        }"""
        )
        if clicked:
            log(f"Dismissed cookie overlay (JS: {clicked}).")
            return True
    except Exception:
        pass
    return False


def _accept_cookies_fallback(page, cfg: dict[str, Any], log: Callable[[str], None]) -> bool:
    selectors = _popup_selectors(
        cfg,
        "cookie_accept",
        (
            "#onetrust-accept-btn-handler",
            "button:has-text('Accept All Cookies')",
            "button:has-text('Accept all cookies')",
            "button:has-text('I Accept')",
        ),
    )
    return _try_click(page, selectors, label="cookie banner (accept fallback)", log=log)


def dismiss_ups_startup_popups(
    page,
    cfg: dict[str, Any],
    *,
    log: Callable[[str], None] | None = None,
    aggressive: bool = False,
) -> bool:
    """
    Best-effort dismiss of overlays that block UPS automation.
    Returns True if anything was dismissed.
    """
    emit = log or _log_default
    timing = cfg.get("timing") or {}
    wait_ms = int(timing.get("popup_wait_ms") or os.environ.get("UPS_POPUP_WAIT_MS") or 2000)
    rounds = int(timing.get("popup_dismiss_rounds") or os.environ.get("UPS_POPUP_DISMISS_ROUNDS") or 6)
    if aggressive:
        rounds = max(rounds, 8)

    try:
        page.wait_for_timeout(max(500, wait_ms))
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

    dismissed_any = False
    for attempt in range(1, rounds + 1):
        dismissed = False

        if _try_click(page, location_deny, label="location prompt", log=emit):
            dismissed = True

        if _try_click(page, cookie_close, label="cookie banner (close)", log=emit):
            dismissed = True
        elif _dismiss_ups_cookie_overlay_js(page, emit):
            dismissed = True

        if _try_click(page, extension_dismiss, label="extension / browser promo", log=emit):
            dismissed = True

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

        if dismissed:
            dismissed_any = True
        elif attempt >= 3 and overlay_still_blocking(page):
            if _accept_cookies_fallback(page, cfg, emit):
                dismissed_any = True
                dismissed = True

        if not dismissed:
            if not overlay_still_blocking(page):
                break
        try:
            page.wait_for_timeout(500)
        except Exception:
            pass

    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(250)
    except Exception:
        pass

    if overlay_still_blocking(page):
        emit("WARN: A cookie/location overlay may still be visible — page may be blocked.")
    return dismissed_any


def clear_blocking_overlays(page, cfg: dict[str, Any], *, log: Callable[[str], None] | None = None) -> None:
    """Dismiss popups until clear or attempts exhausted."""
    emit = log or _log_default
    for attempt in range(1, 5):
        dismiss_ups_startup_popups(page, cfg, log=emit, aggressive=attempt > 2)
        if not overlay_still_blocking(page):
            return
        emit(f"Overlay still blocking (attempt {attempt}/4) — retrying dismiss…")
    emit("WARN: Could not fully clear overlays — continuing anyway.")
