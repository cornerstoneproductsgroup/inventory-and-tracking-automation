"""Launch installed Google Chrome for UPS automation — CDP attach only."""

from __future__ import annotations

import json
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
    """End chrome.exe so automation can open the profile with a debug session."""
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


def _devtools_active_port_file(user_data: Path, profile: str) -> Path | None:
    for rel in (Path(profile) / "DevToolsActivePort", Path("DevToolsActivePort")):
        path = user_data / rel
        if path.is_file():
            return path
    return None


def read_devtools_port(user_data: Path, profile: str) -> int | None:
    """Chrome writes the real debug port here when --remote-debugging-port is used."""
    path = _devtools_active_port_file(user_data, profile)
    if path is None:
        return None
    try:
        first_line = path.read_text(encoding="utf-8", errors="ignore").splitlines()[0].strip()
        port = int(first_line)
        return port if port > 0 else None
    except (OSError, ValueError, IndexError):
        return None


def discover_cdp_port(
    *,
    preferred: int,
    user_data: Path,
    profile: str,
    timeout_s: float = 90.0,
) -> int | None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for port in (preferred, read_devtools_port(user_data, profile)):
            if port and cdp_endpoint_ready(port, timeout_s=1.0):
                return port
        time.sleep(0.5)
    return None


def list_cdp_tabs(port: int) -> list[dict[str, Any]]:
    url = f"http://127.0.0.1:{port}/json/list"
    try:
        with urllib.request.urlopen(url, timeout=3.0) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return []


def wait_for_ups_tab(
    port: int,
    *,
    home_url: str,
    timeout_s: float = 120.0,
) -> bool:
    """Wait until Chrome has a tab on ups.com (opened by real Chrome, not Playwright)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for tab in list_cdp_tabs(port):
            target_url = str(tab.get("url") or "").lower()
            if "ups.com" in target_url:
                _log(f"UPS tab visible in Chrome: {tab.get('url')}")
                return True
        time.sleep(0.75)
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
        "--disable-restore-session-state",
        "--disable-notifications",
        "--new-window",
        home_url,
    ]


def _launch_chrome_process(cmd: list[str], *, log_dir: Path | None = None) -> None:
    """Start real Chrome (not Playwright) — same as double-clicking Chrome with flags."""
    _log("Chrome command: " + " ".join(cmd))
    stderr_log: int | None = None
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        stderr_path = log_dir / "chrome_launch_stderr.log"
        stderr_log = os.open(str(stderr_path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC)

    popen_kwargs: dict[str, Any] = {
        "stdout": subprocess.DEVNULL,
        "close_fds": True,
    }
    if stderr_log is not None:
        popen_kwargs["stderr"] = stderr_log
    else:
        popen_kwargs["stderr"] = subprocess.DEVNULL

    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    subprocess.Popen(cmd, **popen_kwargs)
    if stderr_log is not None:
        os.close(stderr_log)


def launch_chrome_for_cdp(
    *,
    home_url: str,
    port: int | None = None,
    wait_for_close_s: float = 8.0,
    log_dir: Path | None = None,
) -> int:
    """
    Start installed Chrome with remote debugging, opening UPS in a normal window.
    Playwright only attaches afterward — it never launches the browser.
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
    _log(f"Starting real Chrome (profile {profile!r}, debug port {port})…")
    _launch_chrome_process(cmd, log_dir=log_dir)

    discovered = discover_cdp_port(
        preferred=port,
        user_data=user_data,
        profile=profile,
        timeout_s=90.0,
    )
    if discovered is None:
        hint = (
            f"Chrome did not open debug port {port} within 90s. "
            "Close all Chrome windows and retry. "
            "You can also run 'Run UPS Chrome Debug.bat' manually, then re-run the batch. "
            "Check logs/chrome_launch_stderr.log if present."
        )
        raise RuntimeError(hint)

    if discovered != port:
        _log(f"Chrome debug port is {discovered} (configured {port}).")

    if not wait_for_ups_tab(discovered, home_url=home_url, timeout_s=120.0):
        _log(
            "WARN: Chrome opened but no ups.com tab detected yet — "
            "Playwright will navigate after attach."
        )

    _log(f"Chrome debug port {discovered} is ready.")
    return discovered


def connect_playwright_cdp(playwright, port: int):
    return playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")


def pick_ups_page_from_context(context, *, home_url: str):
    for pg in context.pages:
        try:
            if "ups.com" in (pg.url or "").lower():
                pg.bring_to_front()
                return pg
        except Exception:
            continue
    if context.pages:
        pg = context.pages[0]
        pg.bring_to_front()
        return pg
    return context.new_page()


def goto_ups_home(page, home_url: str) -> None:
    current = (page.url or "").strip()
    _log(f"Active tab before navigation: {current!r}")
    if "ups.com" in current.lower():
        _log(f"Already on UPS: {current}")
        return

    last_err: Exception | None = None
    for attempt in range(1, 4):
        try:
            _log(f"Navigating to {home_url} (attempt {attempt}/3)…")
            page.goto(home_url, wait_until="domcontentloaded", timeout=120_000)
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
