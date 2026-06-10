"""Launch installed Google Chrome for UPS automation (CDP + fallbacks)."""

from __future__ import annotations

import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from automation.ups_batch_config import (
    chrome_cdp_port,
    chrome_executable,
    chrome_profile_directory,
    kill_chrome_before_launch,
    system_chrome_user_data_dir,
)


def _log(msg: str) -> None:
    print(f"[ups] {msg}", flush=True)


def chrome_process_count() -> int:
    if os.name != "nt":
        return 0
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/NH"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        return result.stdout.lower().count("chrome.exe")
    except Exception:
        return 0


def close_chrome_processes(*, force: bool | None = None) -> int:
    """
    End chrome.exe so Playwright can open the profile with a debug session.
    Returns remaining chrome process count.
    """
    remaining = chrome_process_count()
    if remaining == 0:
        return 0

    should_kill = kill_chrome_before_launch() if force is None else force
    if not should_kill:
        _log(
            f"WARN: {remaining} chrome.exe process(es) still running. "
            "Set UPS_KILL_CHROME=1 or close Chrome manually."
        )
        return remaining

    _log(f"Ending {remaining} chrome.exe process(es) before UPS automation…")
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", "chrome.exe", "/T"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception as exc:
        _log(f"WARN: taskkill chrome failed: {exc}")

    for _ in range(20):
        time.sleep(0.5)
        remaining = chrome_process_count()
        if remaining == 0:
            _log("Chrome closed.")
            return 0
    _log(f"WARN: {remaining} chrome.exe process(es) still running after taskkill.")
    return remaining


def ensure_chrome_closed() -> None:
    remaining = close_chrome_processes()
    if remaining > 0:
        raise RuntimeError(
            f"{remaining} chrome.exe process(es) still running. "
            "Close Chrome manually or set UPS_KILL_CHROME=1 in .env."
        )


def cdp_endpoint_ready(port: int, *, timeout_s: float = 2.0) -> bool:
    url = f"http://127.0.0.1:{port}/json/version"
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def wait_for_cdp_endpoint(port: int, *, timeout_s: float = 90.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if cdp_endpoint_ready(port, timeout_s=2.0):
            return True
        time.sleep(0.5)
    return False


def _build_chrome_cmd(
    *,
    exe: Path,
    user_data: Path,
    profile: str,
    port: int,
    home_url: str,
) -> list[str]:
    return [
        str(exe),
        f"--user-data-dir={user_data}",
        f"--profile-directory={profile}",
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        "--disable-notifications",
        "--new-window",
        home_url,
    ]


def launch_chrome_for_cdp(
    *,
    home_url: str,
    port: int | None = None,
    wait_for_close_s: float = 8.0,
) -> int:
    """
    Start Chrome with remote debugging on a fixed port.
    Returns the debug port used.
    """
    exe = chrome_executable()
    user_data = system_chrome_user_data_dir()
    if exe is None or user_data is None:
        raise RuntimeError("Chrome executable or User Data folder not found.")

    profile = chrome_profile_directory()
    port = port or chrome_cdp_port()

    close_chrome_processes()
    if wait_for_close_s > 0 and chrome_process_count() > 0:
        _log(f"Waiting {wait_for_close_s:.0f}s for Chrome to close…")
        deadline = time.monotonic() + wait_for_close_s
        while time.monotonic() < deadline:
            if chrome_process_count() == 0:
                break
            time.sleep(1.0)
    ensure_chrome_closed()

    if cdp_endpoint_ready(port, timeout_s=1.0):
        _log(f"CDP port {port} already listening — connecting to existing Chrome.")
        return port

    cmd = _build_chrome_cmd(
        exe=exe,
        user_data=user_data,
        profile=profile,
        port=port,
        home_url=home_url,
    )
    _log(f"Starting Chrome (profile {profile!r}, debug port {port})…")
    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )

    if not wait_for_cdp_endpoint(port, timeout_s=90.0):
        raise RuntimeError(
            f"Chrome did not open debug port {port} within 90s. "
            "Close all Chrome windows and retry. If this persists, set "
            "UPS_USE_CHROME_CDP=0 in .env to use Playwright's Chrome launcher."
        )
    _log(f"Chrome debug port {port} is ready.")
    return port


def connect_playwright_cdp(playwright, port: int):
    return playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")


def _pick_active_page(context, *, home_url: str):
    for pg in context.pages:
        try:
            if "ups.com" in (pg.url or "").lower():
                pg.bring_to_front()
                return pg
        except Exception:
            continue
    if context.pages:
        return context.pages[0]
    return context.new_page()


def _goto_ups_home(page, home_url: str) -> None:
    current = (page.url or "").strip()
    _log(f"Chrome tab before navigation: {current!r}")
    if "ups.com" in current.lower():
        _log(f"Already on UPS: {current}")
        return

    last_err: Exception | None = None
    for attempt in range(1, 4):
        try:
            _log(f"Navigating to {home_url} (attempt {attempt}/3)…")
            page.goto(home_url, wait_until="domcontentloaded", timeout=120_000)
            try:
                page.wait_for_load_state("networkidle", timeout=20_000)
            except Exception:
                pass
            if "ups.com" in (page.url or "").lower():
                _log(f"UPS loaded: {page.url}")
                return
        except Exception as exc:
            last_err = exc
            _log(f"WARN: navigation attempt {attempt} failed: {exc}")
            try:
                page = page.context.new_page()
                page.bring_to_front()
            except Exception:
                pass
        time.sleep(1.0)

    raise RuntimeError(f"Chrome stayed on {page.url!r}; could not open UPS. {last_err}")


def _close_blank_tabs(context) -> None:
    for pg in list(context.pages):
        try:
            url = (pg.url or "").strip().lower()
        except Exception:
            continue
        if url in ("about:blank", "", "chrome://newtab/") and len(context.pages) > 1:
            try:
                pg.close()
            except Exception:
                pass


def launch_chrome_persistent_playwright(
    playwright,
    cfg: dict[str, Any],
    *,
    home_url: str,
    headless: bool,
    slow_mo: int,
    launch_args: list[str],
):
    """Playwright launches installed Chrome with the system User Data folder."""
    user_data = system_chrome_user_data_dir()
    if user_data is None:
        raise RuntimeError("Chrome User Data folder not found.")

    ensure_chrome_closed()

    profile = chrome_profile_directory()
    _log(
        f"Playwright launching Chrome — User Data: {user_data} "
        f"(profile {profile!r})…"
    )

    chrome_args = list(launch_args)
    for flag in ("--disable-restore-session-state",):
        if flag not in chrome_args:
            chrome_args.append(flag)

    # Playwright forbids a startup URL in args for launch_persistent_context;
    # navigate with page.goto() after launch instead.
    context = playwright.chromium.launch_persistent_context(
        str(user_data),
        channel="chrome",
        headless=headless,
        slow_mo=slow_mo,
        args=chrome_args,
        ignore_default_args=["--enable-automation", "--no-sandbox"],
        accept_downloads=True,
    )
    _log(f"Chrome launched — {len(context.pages)} tab(s) open.")
    _close_blank_tabs(context)

    page = _pick_active_page(context, home_url=home_url)
    page.bring_to_front()
    _goto_ups_home(page, home_url)
    return context
