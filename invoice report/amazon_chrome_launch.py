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


def resolve_chrome_persistent_dir() -> tuple[Path, list[str]]:
    """
    Playwright user_data_dir for Chrome.

    Use the profile subfolder (e.g. User Data\\Default) when it exists — the parent
    User Data path often leaves the first tab stuck on about:blank.
    """
    user_data = chrome_user_data_dir()
    if user_data is None:
        raise RuntimeError("Chrome User Data folder not found.")
    profile = chrome_profile_directory()
    profile_path = user_data / profile
    if profile_path.is_dir():
        return profile_path, []
    return user_data, [f"--profile-directory={profile}"]


def kill_chrome_before_launch() -> bool:
    _load_env()
    raw = (os.environ.get("AMAZON_KILL_CHROME") or os.environ.get("UPS_KILL_CHROME") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


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


def _read_devtools_port(user_data: Path, profile: str) -> int | None:
    for rel in (Path(profile) / "DevToolsActivePort", Path("DevToolsActivePort")):
        path = user_data / rel
        if not path.is_file():
            continue
        try:
            first = path.read_text(encoding="utf-8", errors="ignore").splitlines()[0].strip()
            port = int(first)
            return port if port > 0 else None
        except (OSError, ValueError, IndexError):
            continue
    return None


def discover_chrome_cdp_port(
    *,
    preferred: int,
    user_data: Path,
    profile: str,
    timeout_s: float = 120.0,
) -> int | None:
    deadline = time.monotonic() + timeout_s
    last_log = 0.0
    while time.monotonic() < deadline:
        candidates: list[int] = []
        for port in (preferred, _read_devtools_port(user_data, profile), 9222, 9348):
            if port and port not in candidates:
                candidates.append(port)
        for port in candidates:
            if cdp_endpoint_ready(port, timeout_s=1.0) and _cdp_is_chrome(port):
                return port
        elapsed = timeout_s - (deadline - time.monotonic())
        if elapsed - last_log >= 15.0:
            last_log = elapsed
            dt = _read_devtools_port(user_data, profile)
            _log(
                f"Waiting for Chrome debug port… {int(elapsed)}s "
                f"(chrome.exe={chrome_process_count()}, DevToolsActivePort={dt})"
            )
        time.sleep(0.5)
    return None


def wait_for_cdp_port(
    port: int,
    *,
    user_data: Path | None = None,
    profile: str = "Default",
    timeout_s: float = 120.0,
) -> int | None:
    if user_data is not None:
        found = discover_chrome_cdp_port(
            preferred=port,
            user_data=user_data,
            profile=profile,
            timeout_s=timeout_s,
        )
        return found
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if cdp_endpoint_ready(port) and _cdp_is_chrome(port):
            return port
        time.sleep(0.5)
    return None


def _build_chrome_cmd(
    *,
    exe: Path,
    user_data: Path,
    profile: str,
    port: int,
    home_url: str,
    bind_localhost: bool,
) -> list[str]:
    cmd = [
        str(exe),
        f"--user-data-dir={user_data}",
        f"--profile-directory={profile}",
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        "--disable-restore-session-state",
        "--disable-notifications",
        "--new-window",
    ]
    if bind_localhost:
        cmd.append("--remote-debugging-address=127.0.0.1")
    cmd.append(home_url)
    return cmd


def _start_chrome_process(cmd: list[str], *, log_dir: Path | None) -> subprocess.Popen | None:
    _log("Chrome command: " + " ".join(cmd))
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "chrome_launch_cmd.txt").write_text(" ".join(cmd), encoding="utf-8")
    try:
        return subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except Exception as exc:
        _log(f"WARN: could not start Chrome process: {exc}")
        return None


def ensure_chrome_closed_for_launch() -> None:
    remaining = close_chrome_processes()
    if remaining > 0:
        raise RuntimeError(
            f"{remaining} Chrome process(es) still running. "
            "Close all Chrome windows or set AMAZON_KILL_CHROME=1 in .env."
        )


def launch_persistent_chrome(
    playwright,
    *,
    user_data_dir: Path,
    home_url: str,
    extra_args: list[str] | None = None,
):
    """Playwright direct control — no remote debugging."""
    exe = chrome_executable()
    profile = chrome_profile_directory()
    _log(f"Chrome profile dir: {user_data_dir}")
    if exe:
        _log(f"Chrome exe: {exe}")

    launch_args = [
        "--disable-session-crashed-bubble",
        "--disable-restore-session-state",
        "--no-first-run",
        "--no-default-browser-check",
        home_url,
    ]
    if extra_args:
        launch_args = extra_args + launch_args

    launch_kwargs: dict[str, Any] = {
        "headless": False,
        "accept_downloads": True,
        "ignore_default_args": ["--enable-automation", "--no-sandbox"],
        "args": launch_args,
        "channel": "chrome",
    }
    if exe is not None:
        launch_kwargs["executable_path"] = str(exe)

    context = playwright.chromium.launch_persistent_context(
        str(user_data_dir),
        **launch_kwargs,
    )

    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if context.pages:
            break
        time.sleep(0.2)

    for pg in context.pages:
        if _is_seller_central_url(_page_url(pg)) and not _is_blank_tab_url(_page_url(pg)):
            _log(f"Chrome opened Seller Central tab: {_page_url(pg)!r}")
            pg.bring_to_front()
            page = bootstrap_seller_central_page(context, home_url)
            assert_chrome_context(context)
            return context, page

    for pg in list(context.pages):
        try:
            pg.close()
        except Exception:
            pass

    page = context.new_page()
    page.bring_to_front()
    page = bootstrap_seller_central_page(context, home_url)
    assert_chrome_context(context)
    return context, page


def launch_persistent_system_chrome(playwright, *, home_url: str):
    """
    Open installed Chrome with the normal User Data profile.
    Playwright controls the browser directly (no debug port required).
    """
    if chrome_executable() is None:
        raise RuntimeError("Google Chrome not found. Set AMAZON_CHROME_EXE in .env.")

    user_data, extra_args = resolve_chrome_persistent_dir()
    profile = chrome_profile_directory()
    _log(f"Chrome profile name: {profile}")
    ensure_chrome_closed_for_launch()
    time.sleep(2.0)

    _log("Opening Chrome with your normal profile (direct automation control)…")
    return launch_persistent_chrome(
        playwright,
        user_data_dir=user_data,
        home_url=home_url,
        extra_args=extra_args,
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

    discovered: int | None = None
    for attempt_label, bind_localhost in (("localhost bind", True), ("no localhost bind", False)):
        cmd = _build_chrome_cmd(
            exe=exe,
            user_data=user_data,
            profile=profile,
            port=port,
            home_url=home_url,
            bind_localhost=bind_localhost,
        )
        _log(f"Starting Google Chrome ({attempt_label}, port {port})…")
        _start_chrome_process(cmd, log_dir=log_dir or _SCRIPT_DIR)

        for _ in range(20):
            if chrome_process_count() > 0:
                break
            time.sleep(0.5)

        discovered = wait_for_cdp_port(
            port,
            user_data=user_data,
            profile=profile,
            timeout_s=60.0,
        )
        if discovered is not None:
            break
        _log(f"WARN: Chrome debug port not ready ({attempt_label}) — retrying…")
        close_chrome_processes(force=True)
        time.sleep(2.0)

    if discovered is None:
        raise RuntimeError(
            f"Chrome did not open debug port {port}. "
            "Close all Chrome windows and retry, or run Run Amazon Chrome Debug.bat."
        )

    _log(f"Chrome ready on port {discovered} ({cdp_browser_name(discovered)}).")
    return discovered


def connect_playwright_cdp(playwright, port: int):
    return playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")


_BLANK_TAB_URLS = frozenset({"about:blank", "chrome://newtab/", "edge://newtab/", ""})


def _page_url(page) -> str:
    try:
        return (page.url or "").strip()
    except Exception:
        return ""


def _is_blank_tab_url(url: str | None) -> bool:
    text = (url or "").strip().lower()
    return text in _BLANK_TAB_URLS or text.startswith("chrome://newtab") or text.startswith("edge://newtab")


def _is_seller_central_url(url: str | None) -> bool:
    return "sellercentral.amazon.com" in (url or "").lower()


def _log_browser_tabs(context) -> None:
    try:
        urls = [_page_url(pg) for pg in context.pages]
        _log(f"Browser tabs ({len(urls)}): {urls!r}")
    except Exception:
        pass


def _close_extra_blank_tabs(context, *, keep) -> None:
    for pg in list(context.pages):
        if pg is keep:
            continue
        if _is_blank_tab_url(_page_url(pg)):
            try:
                _log(f"Closing blank tab: {_page_url(pg)!r}")
                pg.close()
            except Exception:
                pass


def _wait_until_tab_navigated(page, *, timeout_ms: int = 20_000) -> bool:
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        if not _is_blank_tab_url(_page_url(page)):
            return True
        page.wait_for_timeout(200)
    return False


def _goto_tab_url(page, url: str) -> bool:
    for wait_until in ("commit", "domcontentloaded"):
        try:
            page.goto(url, wait_until=wait_until, timeout=45_000)
        except Exception as exc:
            _log(f"WARN: page.goto ({wait_until}) raised: {exc}")
        if _wait_until_tab_navigated(page, timeout_ms=15_000):
            return True
    return False


def _js_navigate_tab(page, url: str) -> bool:
    try:
        page.evaluate("(target) => { window.location.assign(target); }", url)
        page.wait_for_load_state("domcontentloaded", timeout=45_000)
    except Exception as exc:
        _log(f"WARN: JS navigate failed: {exc}")
        return False
    return not _is_blank_tab_url(_page_url(page))


def _paste_url_in_address_bar(page, url: str) -> bool:
    page.bring_to_front()
    page.wait_for_timeout(400)
    try:
        page.mouse.click(500, 400)
        page.wait_for_timeout(200)
    except Exception:
        try:
            page.locator("body").click(timeout=2000)
        except Exception:
            pass

    for focus_key in ("Control+l", "Alt+d", "F6"):
        try:
            page.keyboard.press(focus_key)
            page.wait_for_timeout(300)
            page.keyboard.press("Control+a")
            page.wait_for_timeout(80)
            page.keyboard.insert_text(url)
            page.wait_for_timeout(80)
            page.keyboard.press("Enter")
            try:
                page.wait_for_load_state("domcontentloaded", timeout=30_000)
            except Exception:
                page.wait_for_timeout(2000)
            if not _is_blank_tab_url(_page_url(page)):
                return True
        except Exception:
            continue
    return False


def _open_seller_central_in_fresh_tab(page, url: str):
    """First tabs in a real Chrome profile often stay on about:blank — open a new tab."""
    context = page.context
    fresh = context.new_page()
    fresh.bring_to_front()
    try:
        for name, load in (
            ("new tab page.goto", lambda: _goto_tab_url(fresh, url)),
            ("new tab JS navigate", lambda: _js_navigate_tab(fresh, url)),
            ("new tab address bar paste", lambda: _paste_url_in_address_bar(fresh, url)),
        ):
            try:
                if load():
                    _log(f"Loaded via {name}: {_page_url(fresh)!r}")
                    _close_extra_blank_tabs(context, keep=fresh)
                    return fresh
            except Exception as exc:
                _log(f"WARN: {name} error: {exc}")
    except Exception as exc:
        _log(f"WARN: fresh tab open failed: {exc}")
    try:
        fresh.close()
    except Exception:
        pass
    return None


def _load_seller_central_in_tab(page, home_url: str):
    current = _page_url(page)
    if _is_seller_central_url(current) and "signin" not in current.lower():
        return page

    _log(f"Tab URL is {current!r} — loading {home_url!r}")
    page.bring_to_front()
    page.wait_for_timeout(500)

    loaders = (
        ("page.goto", lambda: _goto_tab_url(page, home_url)),
        ("JS navigate", lambda: _js_navigate_tab(page, home_url)),
        ("address bar paste", lambda: _paste_url_in_address_bar(page, home_url)),
    )
    for attempt in range(1, 3):
        _log(f"Seller Central load attempt {attempt}/2…")
        for name, load in loaders:
            try:
                if load():
                    _log(f"Loaded via {name}: {_page_url(page)!r}")
                    return page
            except Exception as exc:
                _log(f"WARN: {name} error: {exc}")
            page.wait_for_timeout(300)

    fresh = _open_seller_central_in_fresh_tab(page, home_url)
    if fresh is not None:
        return fresh

    raise RuntimeError(
        f"Still on about:blank after loading Seller Central ({home_url!r}). "
        "Close all Chrome windows and run again."
    )


def pick_seller_central_page(context, *, home_url: str):
    if not context.pages:
        return context.new_page()
    for pg in context.pages:
        try:
            url = _page_url(pg)
            if _is_seller_central_url(url) and not _is_blank_tab_url(url):
                pg.bring_to_front()
                return pg
        except Exception:
            continue
    pg = context.pages[0]
    pg.bring_to_front()
    return pg


def bootstrap_seller_central_page(context, home_url: str):
    """Fix about:blank startup tab — call right after opening the browser."""
    _log_browser_tabs(context)

    if not context.pages or all(_is_blank_tab_url(_page_url(pg)) for pg in context.pages):
        _log("Startup tab is blank — opening a fresh tab for Seller Central…")
        driver = context.new_page()
        driver.bring_to_front()
        for pg in list(context.pages):
            if pg is driver:
                continue
            try:
                pg.close()
            except Exception:
                pass
    else:
        driver = pick_seller_central_page(context, home_url=home_url)
        driver.bring_to_front()

    current = _page_url(driver).lower()
    if _is_blank_tab_url(current) or not _is_seller_central_url(current):
        driver = _load_seller_central_in_tab(driver, home_url)
    _close_extra_blank_tabs(context, keep=driver)
    driver.bring_to_front()
    url = _page_url(driver).lower()
    if "sellercentral.amazon.com" not in url:
        raise RuntimeError(
            f"Browser stayed on {_page_url(driver)!r}; could not open Seller Central."
        )
    _log(f"Seller Central loaded: {_page_url(driver)}")
    return driver


def goto_seller_central_home(page, home_url: str):
    """Navigate to Seller Central; returns the active page (may be a new tab)."""
    return bootstrap_seller_central_page(page.context, home_url)


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


def connect_system_chrome_cdp(
    playwright,
    *,
    home_url: str,
    port: int,
    log_dir: Path | None = None,
):
    """Attach Playwright over CDP (used when AMAZON_CHROME_CDP_URL is set)."""
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
    page = bootstrap_seller_central_page(context, home_url)
    return browser, page


def connect_system_chrome(
    playwright,
    *,
    home_url: str,
    port: int,
    log_dir: Path | None = None,
):
    """Open Chrome with the normal profile. Returns (page, cleanup_fn). No CDP by default."""
    _load_env()
    try:
        from amazon_seller_config import allow_unsafe_cdp, use_cdp_launch
    except ImportError:
        allow_unsafe_cdp = lambda: False  # type: ignore[misc, assignment]
        use_cdp_launch = lambda: False  # type: ignore[misc, assignment]

    if use_cdp_launch():
        _log("WARN: Using CDP / remote debugging (AMAZON_ALLOW_UNSAFE_CDP=1).")
        browser, page = connect_system_chrome_cdp(
            playwright, home_url=home_url, port=port, log_dir=log_dir
        )
        return page, lambda: None

    _log("Opening Chrome with Playwright direct control (no remote debugging).")
    context, page = launch_persistent_system_chrome(playwright, home_url=home_url)
    return page, context.close
