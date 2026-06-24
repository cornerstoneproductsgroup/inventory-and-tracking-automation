"""Launch installed Google Chrome (only) with the normal profile for Amazon Seller Central."""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent


def _log(msg: str) -> None:
    print(f"[amazon-seller] {msg}", flush=True)


def _load_env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(_SCRIPT_DIR / ".env", override=False)
        inv_env = _SCRIPT_DIR.parent / "Inventory Submissions" / ".env"
        if inv_env.is_file():
            load_dotenv(inv_env, override=False)
    except ImportError:
        pass


def chrome_executable() -> Path | None:
    _load_env()
    override = (os.environ.get("AMAZON_CHROME_EXE") or "").strip()
    if override:
        path = Path(override)
        return path if path.is_file() else None
    roots = [
        os.environ.get("PROGRAMFILES", ""),
        os.environ.get("PROGRAMFILES(X86)", ""),
        os.environ.get("LOCALAPPDATA", ""),
    ]
    for root in roots:
        if not root:
            continue
        cand = Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"
        if cand.is_file():
            return cand
    return None


def chrome_user_data_dir() -> Path | None:
    """Installed Chrome User Data root (not the isolated Playwright profile)."""
    _load_env()
    override = (os.environ.get("AMAZON_SYSTEM_CHROME_USER_DATA_DIR") or "").strip()
    if override:
        path = Path(override)
        if path.is_dir():
            return path
    local = (os.environ.get("LOCALAPPDATA") or "").strip()
    if not local:
        return None
    path = Path(local) / "Google" / "Chrome" / "User Data"
    return path if path.is_dir() else None


def chrome_profile_directory() -> str:
    _load_env()
    return (
        (os.environ.get("AMAZON_CHROME_PROFILE") or "").strip()
        or (os.environ.get("AMAZON_BROWSER_PROFILE") or "").strip()
        or "Default"
    )


def kill_chrome_before_launch() -> bool:
    _load_env()
    raw = (os.environ.get("AMAZON_KILL_CHROME") or os.environ.get("UPS_KILL_CHROME") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


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
    remaining = chrome_process_count()
    if remaining == 0:
        return 0
    should_kill = kill_chrome_before_launch() if force is None else force
    if not should_kill:
        _log(
            f"WARN: {remaining} chrome.exe process(es) still running. "
            "Close Chrome manually or set AMAZON_KILL_CHROME=1 in .env."
        )
        return remaining
    _log(f"Closing {remaining} Chrome process(es)…")
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", "chrome.exe", "/T"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception as exc:
        _log(f"WARN: taskkill chrome.exe failed: {exc}")
    for _ in range(20):
        time.sleep(0.5)
        if chrome_process_count() == 0:
            _log("Chrome closed.")
            return 0
    remaining = chrome_process_count()
    _log(f"WARN: {remaining} chrome.exe process(es) still running after taskkill.")
    return remaining


def cdp_endpoint_ready(port: int, *, timeout_s: float = 2.0) -> bool:
    url = f"http://127.0.0.1:{port}/json/version"
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def cdp_browser_name(port: int) -> str:
    """Browser string from CDP /json/version (e.g. 'Chrome/131' or 'Microsoft Edge/131')."""
    url = f"http://127.0.0.1:{port}/json/version"
    try:
        with urllib.request.urlopen(url, timeout=2.0) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        return str(data.get("Browser") or data.get("browser") or "")
    except Exception:
        return ""


def _cdp_is_chrome(port: int) -> bool:
    name = cdp_browser_name(port).lower()
    if not name:
        return False
    if "edge" in name or "edg/" in name:
        return False
    return "chrome" in name


def wait_for_cdp_port(port: int, *, timeout_s: float = 90.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if cdp_endpoint_ready(port) and _cdp_is_chrome(port):
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
        "--disable-restore-session-state",
        "--disable-notifications",
        "--new-window",
        home_url,
    ]


def _start_chrome_process(cmd: list[str], *, log_dir: Path | None) -> None:
    _log("Chrome command: " + " ".join(cmd))
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "chrome_launch_cmd.txt").write_text(" ".join(cmd), encoding="utf-8")
    if os.name == "nt":
        subprocess.Popen(
            ["cmd", "/c", "start", "", cmd[0], *cmd[1:]],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        return
    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def launch_chrome_for_cdp(
    *,
    home_url: str,
    port: int,
    log_dir: Path | None = None,
) -> int:
    """Start Google Chrome with remote debugging — never Edge."""
    exe = chrome_executable()
    user_data = chrome_user_data_dir()
    if exe is None:
        raise RuntimeError(
            "Google Chrome not found. Install Chrome or set AMAZON_CHROME_EXE in .env."
        )
    if user_data is None:
        raise RuntimeError(
            "Chrome User Data folder not found. Set AMAZON_CHROME_USER_DATA_DIR in .env."
        )

    profile = chrome_profile_directory()
    _log(f"Chrome exe: {exe}")
    _log(f"Chrome profile: {user_data} \\ {profile}")

    if cdp_endpoint_ready(port) and _cdp_is_chrome(port):
        _log(f"Chrome debug port {port} already open ({cdp_browser_name(port)}).")
        return port

    if cdp_endpoint_ready(port) and not _cdp_is_chrome(port):
        name = cdp_browser_name(port) or "unknown browser"
        raise RuntimeError(
            f"Port {port} is in use by {name}, not Chrome. "
            f"Close that browser or set AMAZON_CHROME_CDP_PORT to a different port."
        )

    remaining = close_chrome_processes()
    if remaining > 0:
        raise RuntimeError(
            f"{remaining} Chrome process(es) still running. "
            "Close Chrome manually or set AMAZON_KILL_CHROME=1 in .env."
        )

    cmd = _build_chrome_cmd(
        exe=exe,
        user_data=user_data,
        profile=profile,
        port=port,
        home_url=home_url,
    )
    _log(f"Starting Google Chrome (profile {profile!r}, debug port {port})…")
    _start_chrome_process(cmd, log_dir=log_dir or _SCRIPT_DIR)

    for _ in range(20):
        if chrome_process_count() > 0:
            break
        time.sleep(0.5)

    if not wait_for_cdp_port(port, timeout_s=90.0):
        raise RuntimeError(
            f"Chrome did not open debug port {port}. "
            "Close all Chrome windows and retry, or run Run Amazon Chrome Debug.bat."
        )

    _log(f"Chrome ready on port {port} ({cdp_browser_name(port)}).")
    return port


def connect_playwright_cdp(playwright, port: int):
    return playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")


def pick_seller_central_page(context, *, home_url: str):
    for pg in context.pages:
        try:
            url = (pg.url or "").lower()
            if "sellercentral.amazon.com" in url:
                pg.bring_to_front()
                return pg
        except Exception:
            continue
    if context.pages:
        pg = context.pages[0]
        pg.bring_to_front()
        return pg
    return context.new_page()


def goto_seller_central_home(page, home_url: str) -> None:
    current = (page.url or "").strip()
    _log(f"Active tab: {current!r}")
    if "sellercentral.amazon.com" in current.lower() and "signin" not in current.lower():
        _log(f"Already on Seller Central: {current}")
        return

    last_err: Exception | None = None
    for attempt in range(1, 4):
        try:
            _log(f"Navigating to {home_url} (attempt {attempt}/3)…")
            page.goto(home_url, wait_until="domcontentloaded", timeout=120_000)
            url = (page.url or "").lower()
            if "sellercentral.amazon.com" in url:
                _log(f"Seller Central loaded: {page.url}")
                return
        except Exception as exc:
            last_err = exc
            _log(f"WARN: navigation attempt {attempt} failed: {exc}")
        time.sleep(1.0)

    raise RuntimeError(
        f"Browser stayed on {page.url!r}; could not open Seller Central. {last_err}"
    )


def assert_chrome_context(context) -> None:
    try:
        page = context.pages[0] if context.pages else context.new_page()
        ua = page.evaluate("() => navigator.userAgent") or ""
    except Exception:
        ua = ""
    if ua and ("Edg/" in ua or "Edge" in ua) and "Chrome" not in ua:
        raise RuntimeError(
            "Connected browser is Microsoft Edge, not Google Chrome. "
            "Set AMAZON_CHROME_CDP_PORT to an unused port and ensure only Chrome uses it."
        )
    if ua:
        _log(f"Browser user-agent: {ua[:80]}…")


def connect_system_chrome(
    playwright,
    *,
    home_url: str,
    port: int,
    log_dir: Path | None = None,
):
    """Attach Playwright to installed Google Chrome (normal profile)."""
    _load_env()

    if cdp_endpoint_ready(port) and _cdp_is_chrome(port):
        _log(f"Attaching to Chrome on port {port} ({cdp_browser_name(port)}).")
    else:
        launch_chrome_for_cdp(home_url=home_url, port=port, log_dir=log_dir)

    browser = connect_playwright_cdp(playwright, port)
    if not browser.contexts:
        raise RuntimeError(f"Chrome on port {port} has no browser contexts.")
    context = browser.contexts[0]
    assert_chrome_context(context)
    page = pick_seller_central_page(context, home_url=home_url)
    goto_seller_central_home(page, home_url)
    return browser, page
