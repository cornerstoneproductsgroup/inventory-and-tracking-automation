"""Launch Edge/Chrome for Pull Orders (CommerceHub + SPS), same pattern as FedEx batch."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, BrowserContext, Page, Playwright

_INVENTORY_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_COMMERCEHUB_PROFILE_DIR = _INVENTORY_ROOT / "pull_orders_commercehub_profile"
DEFAULT_SPS_PROFILE_DIR = _INVENTORY_ROOT / "pull_orders_sps_profile"

_STEALTH_INIT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
"""


def _log(msg: str) -> None:
    print(f"[pull-orders/browser] {msg}", flush=True)


def _resolve_channel() -> str | None:
    explicit = (os.environ.get("PULL_ORDERS_BROWSER_CHANNEL") or "").strip()
    lowered = explicit.lower()
    if lowered in ("chromium", "bundled", "playwright"):
        return None
    if lowered == "auto" or not explicit:
        return "msedge" if os.name == "nt" else "chrome"
    return explicit


def _use_persistent_profile(*, for_sps: bool) -> bool:
    """
    SPS defaults off — reuse sps_playwright_storage.json (same as tracking/inventory).
    CommerceHub defaults off — log in with RITHUM_* from .env (same as commercehub_chain).
    Set PULL_ORDERS_*_USE_PERSISTENT_PROFILE=1 to keep a separate Edge profile instead.
    """
    if for_sps:
        env_key = "PULL_ORDERS_SPS_USE_PERSISTENT_PROFILE"
    else:
        env_key = "PULL_ORDERS_COMMERCEHUB_USE_PERSISTENT_PROFILE"
    raw = (os.environ.get(env_key) or os.environ.get("PULL_ORDERS_USE_PERSISTENT_PROFILE") or "0").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _profile_dir(*, for_sps: bool) -> Path | None:
    if not _use_persistent_profile(for_sps=for_sps):
        return None
    env_key = "PULL_ORDERS_SPS_USER_DATA_DIR" if for_sps else "PULL_ORDERS_COMMERCEHUB_USER_DATA_DIR"
    raw = (os.environ.get(env_key) or "").strip()
    if raw.lower() in ("0", "false", "no", "off", "disable", "disabled"):
        return None
    if raw:
        path = Path(raw)
    else:
        path = DEFAULT_SPS_PROFILE_DIR if for_sps else DEFAULT_COMMERCEHUB_PROFILE_DIR
    if not path.is_absolute():
        path = _INVENTORY_ROOT / path
    return path


def _launch_args() -> list[str]:
    base = [
        "--disable-blink-features=AutomationControlled",
        "--disable-features=BlockThirdPartyCookies,TrackingProtection3pcd",
    ]
    out: list[str] = []
    for arg in base:
        if arg not in out:
            out.append(arg)
    return out


def _apply_stealth(context: BrowserContext) -> None:
    try:
        context.add_init_script(_STEALTH_INIT)
    except Exception:
        pass


def _headless(*, for_sps: bool) -> bool:
    if for_sps:
        raw = (os.environ.get("HEADLESS") or os.environ.get("COMMERCEHUB_HEADLESS") or "false").strip()
    else:
        raw = (os.environ.get("COMMERCEHUB_HEADLESS") or "false").strip()
    return raw.lower() in ("1", "true", "yes")


def _slow_mo() -> int:
    raw = (os.environ.get("COMMERCEHUB_SLOW_MO_MS") or "0").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def open_pull_orders_browser(
    p: Playwright,
    *,
    for_sps: bool = False,
    storage_state: Path | None = None,
) -> tuple[Browser | None, BrowserContext, Page, bool]:
    """
    Launch Pull Orders browser (Edge by default on Windows).

    Returns (browser, context, page, uses_persistent_profile).
    browser is None when using launch_persistent_context.
    """
    headless = _headless(for_sps=for_sps)
    slow_mo = _slow_mo()
    user_data_dir = _profile_dir(for_sps=for_sps)
    if for_sps and storage_state and storage_state.is_file() and user_data_dir is not None:
        _log(
            f"SPS: using session file {storage_state} (same as tracking); "
            "ignoring persistent profile."
        )
        user_data_dir = None
    args = _launch_args()
    channels: list[str | None] = []
    primary = _resolve_channel()
    if primary:
        channels.append(primary)
    for alt in ("msedge", "chrome"):
        if alt not in channels:
            channels.append(alt)
    channels.append(None)

    seen: set[str | None] = set()
    last_err: Exception | None = None
    leg = "SPS" if for_sps else "CommerceHub"

    for channel in channels:
        if channel in seen:
            continue
        seen.add(channel)
        label = channel or "playwright chromium"
        try:
            launch_kwargs: dict[str, Any] = {
                "headless": headless,
                "slow_mo": slow_mo,
                "args": args,
                "ignore_default_args": ["--enable-automation"],
            }
            if channel:
                launch_kwargs["channel"] = channel

            if user_data_dir is not None:
                user_data_dir.mkdir(parents=True, exist_ok=True)
                context = p.chromium.launch_persistent_context(
                    str(user_data_dir),
                    accept_downloads=True,
                    **launch_kwargs,
                )
                _apply_stealth(context)
                page = context.pages[0] if context.pages else context.new_page()
                _log(f"{leg}: {label} with persistent profile ({user_data_dir})")
                return None, context, page, True

            browser = p.chromium.launch(**launch_kwargs)
            state = storage_state if storage_state and storage_state.is_file() else None
            context = browser.new_context(
                accept_downloads=True,
                storage_state=str(state) if state else None,
                locale="en-US",
                viewport={"width": 1440, "height": 900},
            )
            _apply_stealth(context)
            page = context.new_page()
            if state:
                _log(f"{leg}: {label} (session from {state})")
            else:
                _log(f"{leg}: {label} (ephemeral context)")
            return browser, context, page, False
        except Exception as exc:
            last_err = exc
            _log(f"WARN: could not launch {leg} browser ({label}): {exc}")

    raise RuntimeError(
        f"Could not launch Pull Orders browser for {leg}. "
        "Install Microsoft Edge or Google Chrome, or set "
        "PULL_ORDERS_BROWSER_CHANNEL=msedge in .env. "
        f"Last error: {last_err}"
    )


def persist_sps_session(context: BrowserContext, state_path: Path, *, uses_persistent_profile: bool) -> None:
    if uses_persistent_profile:
        profile = _profile_dir(for_sps=True)
        _log(f"SPS session kept in browser profile ({profile}).")
        return
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(state_path))
        _log(f"Saved SPS session for tracking reuse: {state_path}")
    except OSError as exc:
        _log(f"WARN: could not save SPS session ({state_path}): {exc}")
