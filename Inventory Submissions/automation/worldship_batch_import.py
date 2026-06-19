"""UPS WorldShip desktop automation — Batch Import wizard (start phase)."""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

RECORD_COUNT_RE = re.compile(
    r"There are\s+(\d+)\s+record\(s\)\s+to be imported",
    re.IGNORECASE,
)
IMPORT_PATH_RE = re.compile(
    r"Importing from\s+(.+?)(?:\s+There are|\Z)",
    re.IGNORECASE | re.DOTALL,
)

WORLDSHIP_TITLE_RE = r".*UPS WorldShip.*"
PREVIEW_DIALOG_TITLE = "Import/Export Preview"
AUTO_PROCESS_LABEL = "Process shipments automatically after import"


def _looks_like_worldship_label(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    if "worldship" in t:
        return True
    return "ups" in t and "ship" in t


def _pinned_taskbar_dirs() -> list[Path]:
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        return []
    base = Path(appdata) / r"Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar"
    dirs = [base, base / "removed"]
    return [d for d in dirs if d.is_dir()]


def _resolve_lnk(path: Path) -> tuple[str, str]:
    import win32com.client

    shell = win32com.client.Dispatch("WScript.Shell")
    sc = shell.CreateShortCut(str(path))
    target = (sc.Targetpath or "").strip()
    workdir = (sc.WorkingDirectory or "").strip()
    return target, workdir


def _find_exe_from_pinned_shortcuts() -> Path | None:
    for folder in _pinned_taskbar_dirs():
        for lnk in folder.glob("*.lnk"):
            try:
                target, _ = _resolve_lnk(lnk)
            except Exception:
                continue
            hay = f"{lnk.stem} {target}".lower()
            if not _looks_like_worldship_label(hay):
                continue
            p = Path(target)
            if p.is_file():
                _log(f"Found WorldShip from pinned shortcut: {lnk.name} → {p}")
                return p.resolve()
    return None


def _click_worldship_taskbar() -> bool:
    """Click the WorldShip icon on the Windows taskbar (pinned or running)."""
    from pywinauto import Desktop

    backends = [_uia_backend_only()]
    preferred = (os.environ.get("WORLDSHIP_UI_BACKEND") or "uia").strip() or "uia"
    if preferred not in backends:
        backends.insert(0, preferred)

    for backend in backends:
        try:
            tray = Desktop(backend=backend).window(class_name="Shell_TrayWnd")
            if not tray.exists(timeout=3):
                continue
        except Exception:
            continue

        candidates: list[tuple[str, object]] = []
        try:
            for el in tray.descendants():
                try:
                    text = (el.window_text() or "").strip()
                except Exception:
                    text = ""
                try:
                    name = (el.element_info.name or "").strip()
                except Exception:
                    name = ""
                label = text or name
                if not _looks_like_worldship_label(label):
                    continue
                candidates.append((label, el))
        except Exception as exc:
            _log(f"WARN: taskbar scan skipped ({type(exc).__name__}).")
            continue

        if not candidates:
            continue

        # Prefer the shortest exact-ish match (pinned label vs long window title).
        candidates.sort(key=lambda t: len(t[0]))
        label, btn = candidates[0]
        try:
            btn.click_input()
            _log(f"Clicked taskbar button: {label!r}")
            return True
        except Exception as exc:
            _log(f"WARN: could not click taskbar button {label!r}: {exc}")
    return False


@dataclass(frozen=True)
class WorldShipBatchImportResult:
    record_count: int
    import_source: str | None
    preview_text: str
    labels_saved: int


def _log(msg: str) -> None:
    print(f"[worldship] {msg}", flush=True)


def _startup_timeout_s() -> float:
    """Max time to attach to any WorldShip window after launch."""
    raw = (os.environ.get("WORLDSHIP_STARTUP_TIMEOUT_S") or "360").strip()
    try:
        return max(60.0, float(raw))
    except ValueError:
        return 360.0


def _ok_dialog_timeout_s() -> float:
    """Max wait for the 'database will now be checked' OK dialog after launch."""
    raw = (os.environ.get("WORLDSHIP_OK_DIALOG_TIMEOUT_S") or "90").strip()
    try:
        return max(15.0, float(raw))
    except ValueError:
        return 90.0


def _ready_timeout_s(*, cold_start: bool) -> float:
    """Max wait for Import-Export tab to become clickable."""
    key = "WORLDSHIP_READY_TIMEOUT_S" if cold_start else "WORLDSHIP_WARM_READY_TIMEOUT_S"
    default = "300" if cold_start else "12"
    raw = (os.environ.get(key) or default).strip()
    try:
        return max(3.0 if cold_start else 2.0, float(raw))
    except ValueError:
        return 300.0 if cold_start else 12.0


def _step_wait_s(env_key: str, default: float) -> float:
    raw = (os.environ.get(env_key) or str(default)).strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


STARTUP_OK_TEXT_HINTS = (
    "did not shut down normally",
    "database will now be checked",
)
BLOCKING_DIALOG_TITLE_HINTS = (
    "software update",
    "ups worldship",
)
BLOCKING_DIALOG_TEXT_HINTS = STARTUP_OK_TEXT_HINTS + ("software update",)


def _uia_backend_only() -> str:
    return "uia"


def _safe_enum_child_text(hwnd: int) -> list[str]:
    import win32gui

    parts: list[str] = []

    def _cb(child, _):
        try:
            t = (win32gui.GetWindowText(child) or "").strip()
            if t:
                parts.append(t)
        except Exception:
            pass
        return True

    try:
        win32gui.EnumChildWindows(hwnd, _cb, None)
    except Exception:
        pass
    return parts


def _enum_candidate_dialog_hwnds() -> list[tuple[int, str]]:
    import win32gui

    out: list[tuple[int, str]] = []

    def _cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            title = (win32gui.GetWindowText(hwnd) or "").strip()
            cls = win32gui.GetClassName(hwnd) or ""
            tlow = title.lower()
            if cls == "#32770":
                out.append((hwnd, title))
                return True
            if any(h in tlow for h in BLOCKING_DIALOG_TITLE_HINTS):
                out.append((hwnd, title))
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:
        pass
    return out


def _dialog_blob(hwnd: int, title: str) -> str:
    parts = [title, *_safe_enum_child_text(hwnd)]
    return " ".join(p for p in parts if p).lower()


def _blocking_dialog_kind(hwnd: int, title: str) -> str | None:
    blob = _dialog_blob(hwnd, title)
    tlow = title.lower()
    if "software update" in tlow or "software update" in blob:
        return "Software Update"
    if any(h in blob for h in STARTUP_OK_TEXT_HINTS):
        return "database check"
    if "worldship" in tlow and "remote workstation" not in tlow:
        import win32gui

        try:
            if win32gui.GetDlgItem(hwnd, 1):
                return title or "WorldShip dialog"
        except Exception:
            pass
    return None


def _click_ok_on_dialog_hwnd(hwnd: int, *, label: str) -> bool:
    from pywinauto import Application

    import win32con
    import win32gui

    try:
        app = Application(backend=_uia_backend_only()).connect(handle=hwnd, timeout=4)
        dlg = app.window(handle=hwnd)
        ok = dlg.child_window(title="OK", control_type="Button")
        if ok.exists(timeout=2):
            ok.click_input()
            _log(f"Clicked OK on {label!r}.")
            return True
    except Exception:
        pass

    for dlg_id in (1, 2):
        try:
            ok_btn = win32gui.GetDlgItem(hwnd, dlg_id)
            if not ok_btn:
                continue
            text = (win32gui.GetWindowText(ok_btn) or "").strip().lower()
            if text not in ("ok", "&ok", ""):
                continue
            win32gui.PostMessage(ok_btn, win32con.BM_CLICK, 0, 0)
            _log(f"Clicked OK (Win32) on {label!r}.")
            return True
        except Exception:
            continue
    return False


def _dismiss_blocking_dialogs_once() -> bool:
    clicked = False
    for hwnd, title in _enum_candidate_dialog_hwnds():
        kind = _blocking_dialog_kind(hwnd, title)
        if kind is None:
            continue
        if _click_ok_on_dialog_hwnd(hwnd, label=kind):
            clicked = True
            time.sleep(0.6)
    return clicked


def _blocking_dialog_visible() -> bool:
    for hwnd, title in _enum_candidate_dialog_hwnds():
        if _blocking_dialog_kind(hwnd, title) is not None:
            return True
    return False


def _wait_and_dismiss_startup_dialogs(*, timeout_s: float) -> bool:
    _log(f"Waiting up to {timeout_s:.0f}s for startup dialogs (OK to click)…")
    deadline = time.monotonic() + timeout_s
    clicked_any = False
    while time.monotonic() < deadline:
        if _dismiss_blocking_dialogs_once():
            clicked_any = True
        elif clicked_any and not _blocking_dialog_visible():
            return True
        time.sleep(1.5)
    return clicked_any


def _import_export_tab_ready(app, *, timeout_s: float = 2.0, fast: bool = False) -> bool:
    if _blocking_dialog_visible():
        return False
    win_timeout = 0.4 if fast else min(3.0, timeout_s)
    try:
        win = app.window(title_re=WORLDSHIP_TITLE_RE)
        if not win.exists(timeout=win_timeout):
            return False
        if _ribbon_action_available(
            win, "Batch Import", ("Button", "MenuItem", "SplitButton")
        ):
            return True
        for tab in _matching_controls(
            win, title="Import-Export", control_types=("TabItem", "Button")
        ):
            try:
                if tab.is_enabled() and tab.is_visible():
                    return True
                if _is_tab_selected(tab):
                    return True
            except Exception:
                continue
        return False
    except Exception:
        return False


def _focus_main_window(win) -> None:
    from automation.worldship_ribbon_click import focus_main_window

    focus_main_window(win, log=_log)


def _bring_worldship_to_front(app) -> bool:
    """Restore and foreground WorldShip as soon as we attach (warm start)."""
    try:
        win = app.window(title_re=WORLDSHIP_TITLE_RE)
        if not win.exists(timeout=0.5):
            return False
        _focus_main_window(win)
        _log("Brought WorldShip to the foreground.")
        return True
    except Exception:
        return False


def _wait_until_import_export_ready(app, *, timeout_s: float, poll_interval_s: float = 2.0):
    _log(f"Waiting up to {timeout_s:.0f}s for Import-Export tab to be ready…")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        _dismiss_blocking_dialogs_once()
        if not _blocking_dialog_visible() and _import_export_tab_ready(app, fast=False):
            win = app.window(title_re=WORLDSHIP_TITLE_RE)
            _focus_main_window(win)
            _log("Import-Export tab is ready (no blocking dialogs).")
            return win
        time.sleep(poll_interval_s)
    raise TimeoutError(
        f"WorldShip Import-Export tab did not become ready within {timeout_s:.0f}s. "
        "A dialog (Software Update, database check, etc.) may still be open, or the app "
        "is still loading — increase WORLDSHIP_READY_TIMEOUT_S."
    )


def _resolve_main_window(app, *, cold_start: bool):
    """Return the main WorldShip window; skip long waits when already loaded."""
    _dismiss_blocking_dialogs_once()
    _bring_worldship_to_front(app)
    if _import_export_tab_ready(app, fast=True):
        win = app.window(title_re=WORLDSHIP_TITLE_RE)
        _focus_main_window(win)
        if not cold_start:
            _log("WorldShip already open — proceeding immediately.")
        else:
            _log("Import-Export tab is ready (no blocking dialogs).")
        return win
    if not cold_start and not _blocking_dialog_visible():
        _log("WorldShip is open — proceeding (warm start, skipping UIA ready poll).")
        win = app.window(title_re=WORLDSHIP_TITLE_RE)
        _focus_main_window(win)
        return win
    if not cold_start:
        _log("WorldShip is open but Import-Export is not ready yet — brief wait…")
    return _wait_until_import_export_ready(
        app,
        timeout_s=_ready_timeout_s(cold_start=cold_start),
        poll_interval_s=2.0 if cold_start else 0.15,
    )


def _default_exe_candidates() -> list[Path]:
    env = (os.environ.get("WORLDSHIP_EXE") or "").strip()
    out: list[Path] = []
    if env:
        out.append(Path(env))
    pinned = _find_exe_from_pinned_shortcuts()
    if pinned is not None:
        out.append(pinned)
    for p in (
        Path(r"C:\Program Files (x86)\UPS\WorldShip\WorldShip.exe"),
        Path(r"C:\Program Files\UPS\WorldShip\WorldShip.exe"),
        Path(r"C:\UPS\WorldShip\WorldShip.exe"),
    ):
        out.append(p)
    # De-dupe while preserving order.
    seen: set[str] = set()
    unique: list[Path] = []
    for p in out:
        key = str(p).lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(p)
    return unique


def _resolve_worldship_exe() -> Path | None:
    for cand in _default_exe_candidates():
        try:
            if cand.is_file():
                return cand.resolve()
        except OSError:
            continue
    return None


def _launch_worldship_exe(exe: Path) -> None:
    _log(f"Launching WorldShip from {exe}")
    subprocess.Popen([str(exe)], cwd=str(exe.parent) if exe.parent.is_dir() else None)


def _require_pywinauto():
    try:
        from pywinauto import Application
        from pywinauto.findwindows import ElementNotFoundError

        return Application, ElementNotFoundError
    except ImportError as exc:
        raise RuntimeError(
            "pywinauto is required for WorldShip automation. "
            'Install: pip install "pywinauto>=0.6.8"'
        ) from exc


def _connect_or_start(app_factory, *, startup_timeout_s: float) -> tuple[object, bool]:
    """
    Return (app, cold_start).

    cold_start is True when WorldShip was launched this run and we must wait for
    startup OK + full load before using the ribbon.
    """
    Application, ElementNotFoundError = _require_pywinauto()
    backend = (os.environ.get("WORLDSHIP_UI_BACKEND") or "uia").strip() or "uia"

    def _connect():
        return app_factory(backend=backend).connect(title_re=WORLDSHIP_TITLE_RE, timeout=8)

    try:
        _log("Connecting to running WorldShip window…")
        app = _connect()
        _bring_worldship_to_front(app)
        if _import_export_tab_ready(app, fast=True):
            _log("Connected — WorldShip is already loaded.")
            return app, False
        _log("Connected — WorldShip is open (brief wait for Import-Export ribbon)…")
        return app, False
    except Exception:
        app = None
        cold = True

    if app is None:
        _log("WorldShip not running — clicking taskbar icon…")
        launched = _click_worldship_taskbar()
        if not launched:
            exe = _resolve_worldship_exe()
            if exe is not None:
                _launch_worldship_exe(exe)
                launched = True
            else:
                raise FileNotFoundError(
                    "Could not start WorldShip.\n"
                    "  Pin UPS WorldShip to the taskbar (recommended — works on both PCs), or\n"
                    "  set WORLDSHIP_EXE in Inventory Submissions\\.env (see .env.example)."
                )
        if not launched:
            raise FileNotFoundError("Could not launch WorldShip.")

        deadline = time.monotonic() + startup_timeout_s
        last_err: Exception | None = None
        while time.monotonic() < deadline:
            _dismiss_blocking_dialogs_once()
            try:
                app = _connect()
                _log("Attached to WorldShip window (loading may still be in progress).")
                break
            except Exception as exc:
                last_err = exc
                time.sleep(2.0)
        else:
            raise TimeoutError(
                f"WorldShip did not appear within {startup_timeout_s:.0f}s. {last_err}"
            )
        cold = True

    if cold:
        _wait_and_dismiss_startup_dialogs(timeout_s=_ok_dialog_timeout_s())

    return app, cold


def _matching_controls(
    win,
    *,
    title: str | None = None,
    title_re: str | None = None,
    control_types: tuple[str, ...],
    max_index: int = 3,
):
    """Yield controls when WorldShip exposes duplicate UIA nodes for one ribbon item."""
    exist_ms = 30
    for ctrl in control_types:
        for i in range(max_index):
            try:
                kwargs: dict = {"control_type": ctrl, "found_index": i}
                if title is not None:
                    kwargs["title"] = title
                if title_re is not None:
                    kwargs["title_re"] = title_re
                target = win.child_window(**kwargs)
                if not target.exists(timeout=exist_ms / 1000.0):
                    break
                yield target
            except Exception:
                break


def _is_tab_selected(target) -> bool:
    try:
        if target.is_selected():
            return True
    except Exception:
        pass
    try:
        return bool(target.get_toggle_state())
    except Exception:
        pass
    return False


def _ribbon_action_available(
    win,
    title: str,
    control_types: tuple[str, ...],
) -> bool:
    for target in _matching_controls(win, title=title, control_types=control_types):
        try:
            if not target.is_visible():
                continue
            if target.is_enabled():
                return True
        except Exception:
            continue
    return False


def _ensure_import_export_tab(main) -> None:
    from automation.worldship_ribbon_click import ensure_import_export_tab

    ensure_import_export_tab(main, log=_log)


_RIBBON_POLL_S = 0.03


def _click_when_ready(
    win,
    *,
    title: str,
    control_types: tuple[str, ...] = ("Button", "TabItem"),
    timeout_s: float = 3.0,
) -> None:
    """Poll quickly and click the first visible, enabled match."""
    deadline = time.monotonic() + timeout_s
    last_err: Exception | None = None
    saw_any = False
    while time.monotonic() < deadline:
        clicked = False
        for target in _matching_controls(win, title=title, control_types=control_types):
            saw_any = True
            try:
                if not target.is_visible():
                    continue
                if not target.is_enabled():
                    if title == "Import-Export" and _is_tab_selected(target):
                        return
                    continue
                target.click_input()
                clicked = True
                break
            except Exception as exc:
                last_err = exc
        if clicked:
            return
        time.sleep(_RIBBON_POLL_S)
    if title == "Import-Export" and _ribbon_action_available(
        win, "Batch Import", ("Button", "MenuItem", "SplitButton")
    ):
        _log("Import-Export ribbon is active — continuing without tab click.")
        return
    hint = "no matching controls found" if not saw_any else "controls not clickable"
    raise RuntimeError(f"Could not click {title!r}: {last_err or hint}")


def _wait_for_batch_import_wizard(app, main, *, timeout_s: float = 8.0):
    """Return the wizard host as soon as the auto-process checkbox appears."""
    from automation.worldship_ribbon_click import (
        _fast_ribbon_clicks_enabled,
        batch_import_wizard_open,
    )

    fast = _fast_ribbon_clicks_enabled(main)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if batch_import_wizard_open(main, app=app):
            for title_re in (".*Batch Import.*", ".*Import.*Export.*"):
                try:
                    cand = app.window(title_re=title_re)
                    if cand.exists(timeout=0.05):
                        return cand
                except Exception:
                    continue
            return main

        for title_re in (".*Batch Import.*", ".*Import.*Export.*"):
            try:
                cand = app.window(title_re=title_re)
                if not cand.exists(timeout=0.03):
                    continue
                if fast:
                    return cand
                for box in _matching_controls(
                    cand, title=AUTO_PROCESS_LABEL, control_types=("CheckBox",)
                ):
                    if box.is_visible():
                        return cand
            except Exception:
                continue

        if not fast:
            for box in _matching_controls(
                main, title=AUTO_PROCESS_LABEL, control_types=("CheckBox",)
            ):
                try:
                    if box.is_visible():
                        return main
                except Exception:
                    continue
        time.sleep(_RIBBON_POLL_S)
    if fast:
        raise RuntimeError(
            f"Batch Import wizard did not appear within {timeout_s:.0f}s"
        )
    return main


def _ensure_checkbox_checked_win32(hwnd: int, label: str) -> bool:
    """Check a dialog checkbox via Win32 (fast over RDP; no UIA tree walk)."""
    import win32con
    import win32gui

    hint = label.lower().replace("&", "")
    bs_checkbox = 0x00000003
    target_hwnd = 0
    first_checkbox = 0

    def _cb(child, _):
        nonlocal target_hwnd, first_checkbox
        try:
            if win32gui.GetClassName(child) != "Button":
                return True
            style = win32gui.GetWindowLong(child, win32con.GWL_STYLE)
            if not (style & bs_checkbox):
                return True
            if not first_checkbox:
                first_checkbox = child
            text = (win32gui.GetWindowText(child) or "").strip().lower().replace("&", "")
            if hint and hint in text:
                target_hwnd = child
                return False
        except Exception:
            pass
        return True

    try:
        win32gui.EnumChildWindows(hwnd, _cb, None)
    except Exception:
        return False

    box = target_hwnd or first_checkbox
    if not box:
        return False
    try:
        checked = win32gui.SendMessage(box, win32con.BM_GETCHECK, 0, 0)
        if checked != win32con.BST_CHECKED:
            win32gui.PostMessage(box, win32con.BM_CLICK, 0, 0)
        return True
    except Exception:
        return False


def _ensure_checkbox_checked(dlg, label: str, *, timeout_s: float = 5.0) -> None:
    from automation.worldship_ribbon_click import _fast_ribbon_clicks_enabled

    dlg_hwnd = 0
    try:
        dlg_hwnd = int(dlg.handle)
    except Exception:
        pass

    if dlg_hwnd and _fast_ribbon_clicks_enabled():
        if _ensure_checkbox_checked_win32(dlg_hwnd, label):
            _log(f"Checked {label!r} (Win32).")
            return

    deadline = time.monotonic() + timeout_s
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        for target in _matching_controls(
            dlg, title=label, control_types=("CheckBox", "RadioButton")
        ):
            try:
                if not target.is_visible():
                    continue
                try:
                    state = target.get_toggle_state()
                    if state != 1:
                        target.click_input()
                except Exception:
                    target.click_input()
                return
            except Exception as exc:
                last_err = exc
        time.sleep(_RIBBON_POLL_S)
    raise RuntimeError(f"Could not set checkbox {label!r}: {last_err}")


@dataclass(frozen=True)
class ModalDialog:
    hwnd: int
    title: str

    @property
    def handle(self) -> int:
        return self.hwnd


def _enum_visible_modal_hwnds(*, title_hint: str = "") -> list[tuple[int, str]]:
    import win32gui

    hint = title_hint.lower()
    out: list[tuple[int, str]] = []

    def _cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            cls = win32gui.GetClassName(hwnd) or ""
            title = (win32gui.GetWindowText(hwnd) or "").strip()
            if not title and cls != "#32770":
                return True
            if cls == "#32770" or (hint and hint in title.lower()):
                out.append((hwnd, title))
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:
        pass
    return out


def _get_control_text(hwnd: int) -> str:
    import win32con
    import win32gui

    try:
        t = (win32gui.GetWindowText(hwnd) or "").strip()
        if t:
            return t
        n = win32gui.SendMessage(hwnd, win32con.WM_GETTEXTLENGTH, 0, 0)
        if n <= 0:
            return ""
        n += 1
        buf = win32gui.PyMakeBuffer(n * 2)
        win32gui.SendMessage(hwnd, win32con.WM_GETTEXT, n, buf)
        return buf.tobytes().decode("utf-16-le", errors="ignore").split("\0")[0].strip()
    except Exception:
        return ""


def _read_preview_text_from_hwnd(hwnd: int) -> str:
    import win32gui

    chunks: list[str] = []

    def _walk(child, depth: int) -> None:
        if depth > 8:
            return
        try:
            cls = win32gui.GetClassName(child) or ""
            t = _get_control_text(child)
            if t:
                low = t.lower().replace("&", "")
                if low not in {"next", "cancel", "help"}:
                    chunks.append(t)
            child_hwnd = child
            next_child = 0
            while True:
                next_child = win32gui.FindWindowEx(child_hwnd, next_child, None, None)
                if not next_child:
                    break
                _walk(next_child, depth + 1)
        except Exception:
            pass

    try:
        _walk(hwnd, 0)
    except Exception:
        pass
    return "\n".join(chunks)


def _send_vk(vk: int) -> None:
    import win32api
    import win32con

    win32api.keybd_event(vk, 0, 0, 0)
    win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)


def _click_preview_next(preview: ModalDialog) -> None:
    """Advance Import/Export Preview — Win32 Next, default button, then Enter."""
    import win32con
    import win32gui

    from automation.worldship_ribbon_click import _import_pacing_s

    hwnd = preview.hwnd
    settle_s = _import_pacing_s("WORLDSHIP_PREVIEW_BEFORE_NEXT_S", 2.0, 1.0)
    if settle_s > 0:
        _log(f"Waiting {settle_s:.1f}s for preview dialog to finish loading…")
        time.sleep(settle_s)

    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass

    for _ in range(5):
        if _click_button_win32(hwnd, "Next"):
            _log("Clicked Next on Import/Export Preview.")
            return
        time.sleep(0.2)

    try:
        default_btn = win32gui.GetDlgItem(hwnd, 1)
        if default_btn:
            win32gui.PostMessage(default_btn, win32con.BM_CLICK, 0, 0)
            _log("Clicked default button on Import/Export Preview.")
            return
    except Exception:
        pass

    try:
        win32gui.SetForegroundWindow(hwnd)
        time.sleep(0.15)
        _send_vk(win32con.VK_RETURN)
        _log("Sent Enter on Import/Export Preview (Next is default).")
        return
    except Exception as exc:
        raise RuntimeError(f"Could not click Next on Import/Export Preview: {exc}") from exc


def _parse_progress_stats(hwnd: int) -> dict[str, int]:
    import re

    stats: dict[str, int] = {}
    for part in _safe_enum_child_text(hwnd):
        m = re.match(r"(Remaining|Successful|Failed|Skipped|Total)\s*:\s*(\d+)", part, re.I)
        if m:
            stats[m.group(1).lower()] = int(m.group(2))
    if not stats:
        blob = " ".join(_safe_enum_child_text(hwnd))
        for key in ("remaining", "successful", "failed", "skipped", "total"):
            m = re.search(rf"{key}\s*:\s*(\d+)", blob, re.I)
            if m:
                stats[key] = int(m.group(1))
    return stats


def _try_click_smart_pickup_once() -> bool:
    """Click Yes if the optional Smart Pickup dialog is visible right now."""
    for hwnd, title in _enum_visible_modal_hwnds():
        blob = " ".join(_safe_enum_child_text(hwnd)).lower()
        title_low = title.lower()
        if "smart pickup" not in blob and "smart pickup" not in title_low:
            if title_low != "ups worldship" or "pickup" not in blob:
                continue
        if _click_button_win32(hwnd, "Yes"):
            _log("Clicked Yes on UPS Smart Pickup prompt.")
            return True
    return False


def _has_processing_progress_window() -> bool:
    for _hwnd, title in _enum_visible_modal_hwnds():
        if "automatic processing progress" in title.lower():
            return True
    return False


def _find_processing_progress() -> tuple[int, dict[str, int]] | None:
    for hwnd, title in _enum_visible_modal_hwnds():
        if "automatic processing progress" in title.lower():
            return hwnd, _parse_progress_stats(hwnd)
    return None


def _try_resume_batch_processing(progress_hwnd: int) -> bool:
    """If the batch was paused (e.g. after Stop), try Process/Continue when offered."""
    for label in ("Process", "Continue", "Resume"):
        if _click_button_win32(progress_hwnd, label):
            _log(f"Clicked {label!r} on Automatic Processing Progress to resume batch.")
            return True
    return False


def _wait_for_processing_after_save(
    *,
    saves_completed: int,
    saves_total: int,
    timeout_s: float,
) -> None:
    """
    WorldShip must keep processing shipments after each Save dialog.

    Do NOT click Stop on Automatic Processing Progress — that ends the batch and
    no further Save Print Output dialogs will appear.
    """
    from automation.windows_save_as import find_save_as_dialog_hwnd

    if saves_completed >= saves_total:
        return

    _log(
        "Let Automatic Processing continue — do NOT click Stop. "
        "Only respond to each Save Print Output As dialog."
    )
    prog = _find_processing_progress()
    prev_remaining = prog[1].get("remaining") if prog else None
    prev_success = prog[1].get("successful") if prog else None

    deadline = time.monotonic() + timeout_s
    stalled_at: float | None = None
    resume_attempted = False

    while time.monotonic() < deadline:
        if find_save_as_dialog_hwnd(log=False):
            _log("Next Save Print Output dialog is ready.")
            return

        prog = _find_processing_progress()
        if prog:
            hwnd, stats = prog
            remaining = stats.get("remaining")
            successful = stats.get("successful")
            total = stats.get("total")

            if (
                remaining == 0
                and total is not None
                and total > 0
                and saves_completed < saves_total
            ):
                raise RuntimeError(
                    f"Automatic Processing shows 0 remaining after only "
                    f"{saves_completed}/{saves_total} label(s) saved. "
                    "If you clicked Stop on Automatic Processing Progress, the batch "
                    "stopped early — close WorldShip, re-import the batch, and do not "
                    "click Stop; save each label when prompted."
                )

            if prev_remaining is not None and remaining is not None and remaining < prev_remaining:
                _log(f"Processing next shipment (remaining {prev_remaining} → {remaining}).")
                return
            if prev_success is not None and successful is not None and successful > prev_success:
                _log(f"Processing next shipment (successful {prev_success} → {successful}).")
                return

            if remaining is not None and remaining == prev_remaining:
                now = time.monotonic()
                if stalled_at is None:
                    stalled_at = now
                elif now - stalled_at >= 18.0:
                    if not resume_attempted and _try_resume_batch_processing(hwnd):
                        resume_attempted = True
                        stalled_at = None
                        time.sleep(1.0)
                        continue
                    raise RuntimeError(
                        "WorldShip batch processing stalled (remaining count not decreasing). "
                        "Do not click Stop — wait for the next Save Print Output As dialog. "
                        f"Remaining={remaining}, saved {saves_completed}/{saves_total}."
                    )
            else:
                stalled_at = None
        else:
            stalled_at = None

        time.sleep(0.4)

    raise TimeoutError(
        f"Timed out waiting for WorldShip to process the next shipment after save "
        f"{saves_completed}/{saves_total}. Do not click Stop on Automatic Processing Progress."
    )


def _wait_for_optional_smart_pickup() -> None:
    from automation.windows_save_as import find_save_as_dialog_hwnd

    pickup_wait_s = _step_wait_s("WORLDSHIP_SMART_PICKUP_WAIT_S", 8.0)
    _log(f"Checking for UPS Smart Pickup prompt (up to {pickup_wait_s:.0f}s, optional)…")
    deadline = time.monotonic() + pickup_wait_s
    while time.monotonic() < deadline:
        if find_save_as_dialog_hwnd():
            _log("Save dialog appeared — skipping remaining Smart Pickup wait.")
            return
        if _has_processing_progress_window():
            _log("Processing started — Smart Pickup was not shown.")
            return
        if _try_click_smart_pickup_once():
            return
        time.sleep(0.2)
    _log("No Smart Pickup prompt — continuing.")


def _wait_for_automatic_processing(*, timeout_s: float) -> None:
    """Wait until batch processing finishes and label save dialogs are ready."""
    from automation.windows_save_as import find_save_as_dialog_hwnd

    _log(f"Waiting up to {timeout_s:.0f}s for shipment processing to finish…")
    deadline = time.monotonic() + timeout_s
    seen_progress = False
    last_log = 0.0
    while time.monotonic() < deadline:
        if find_save_as_dialog_hwnd():
            _log("First Save Print Output dialog is ready.")
            return

        for hwnd, title in _enum_visible_modal_hwnds():
            if "automatic processing progress" not in title.lower():
                continue
            seen_progress = True
            stats = _parse_progress_stats(hwnd)
            remaining = stats.get("remaining")
            total = stats.get("total")
            if time.monotonic() - last_log > 8.0 and remaining is not None:
                _log(
                    f"Processing… remaining={remaining}"
                    + (f" of {total}" if total is not None else "")
                )
                last_log = time.monotonic()
            if remaining == 0 and total is not None and total > 0:
                _log("All shipments processed — waiting for first save dialog…")
                # Keep looping until save dialog or timeout
        time.sleep(0.4)

    if seen_progress:
        raise TimeoutError(
            f"Timed out after {timeout_s:.0f}s waiting for processing to finish "
            "and the first save dialog to appear."
        )
    raise TimeoutError(
        f"Automatic Processing Progress did not appear within {timeout_s:.0f}s."
    )


def _advance_after_preview_next(*, processing_timeout_s: float) -> None:
    """
    Optional Smart Pickup Yes, then wait for processing — unless save/processing
    is already underway (pickup prompt skipped on repeat runs).
    """
    from automation.windows_save_as import find_save_as_dialog_hwnd

    if find_save_as_dialog_hwnd():
        _log("Save Print Output dialog already open — skipping pickup and processing wait.")
        return

    if _has_processing_progress_window():
        _log("Automatic Processing Progress already open.")
    else:
        _wait_for_optional_smart_pickup()

    if find_save_as_dialog_hwnd():
        _log("Save Print Output dialog ready.")
        return

    _wait_for_automatic_processing(timeout_s=processing_timeout_s)


def _click_button_win32(hwnd: int, button_text: str) -> bool:
    import win32con
    import win32gui

    target = button_text.lower().replace("&", "")
    found_btn: int | None = None

    def _cb(child, _):
        nonlocal found_btn
        try:
            if win32gui.GetClassName(child) != "Button":
                return True
            text = (win32gui.GetWindowText(child) or "").strip().lower().replace("&", "")
            if text == target:
                found_btn = child
                return False
        except Exception:
            pass
        return True

    try:
        win32gui.EnumChildWindows(hwnd, _cb, None)
    except Exception:
        pass
    if not found_btn:
        return False
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass
    try:
        win32gui.PostMessage(found_btn, win32con.BM_CLICK, 0, 0)
        return True
    except Exception:
        return False


def _click_dialog_button(
    dlg,
    title: str,
    *,
    title_hint: str = "",
    timeout_s: float = 3.0,
) -> None:
    """Click a modal dialog button — Win32 first (fast), then UIA."""
    deadline = time.monotonic() + timeout_s
    hint = title_hint.lower()
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        modals = _enum_visible_modal_hwnds(title_hint=title_hint)
        if hint:
            modals = sorted(
                modals,
                key=lambda pair: hint not in pair[1].lower(),
            )
        for hwnd, _wtitle in modals:
            if _click_button_win32(hwnd, title):
                return
        try:
            handle = _dialog_hwnd(dlg)
            if handle and _click_button_win32(handle, title):
                return
        except Exception:
            pass
        if isinstance(dlg, ModalDialog):
            time.sleep(0.05)
            continue
        for target in _matching_controls(dlg, title=title, control_types=("Button",)):
            try:
                if target.is_visible() and target.is_enabled():
                    target.click_input()
                    return
            except Exception as exc:
                last_err = exc
        time.sleep(0.05)
    raise RuntimeError(
        f"Could not click button {title!r}"
        + (f" on {title_hint!r}" if title_hint else "")
        + f": {last_err or 'no matching button'}"
    )


def _find_modal_dialog(title_hint: str, *, timeout_s: float = 90) -> ModalDialog:
    """Wait for a visible modal by Win32 title (UIA often misses WorldShip dialogs)."""
    import win32gui

    hint = title_hint.lower()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for hwnd, title in _enum_visible_modal_hwnds(title_hint=title_hint):
            if hint not in title.lower():
                continue
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                pass
            _log(f"Found dialog: {title!r}")
            return ModalDialog(hwnd=hwnd, title=title)
        time.sleep(0.15)
    visible = [t for _, t in _enum_visible_modal_hwnds()]
    raise TimeoutError(
        f"Timed out waiting for dialog containing {title_hint!r} ({timeout_s:.0f}s). "
        f"Visible dialogs: {visible or '(none)'}"
    )


def _find_dialog(app, title: str, *, timeout_s: float = 90):
    """Find a modal dialog — Win32 first, UIA fallback."""
    try:
        return _find_modal_dialog(title, timeout_s=min(timeout_s, 15.0))
    except TimeoutError:
        pass
    dlg = app.window(title=title)
    dlg.wait("visible", timeout=int(timeout_s))
    try:
        dlg.set_focus()
    except Exception:
        pass
    return dlg


def _dialog_hwnd(dlg) -> int | None:
    if isinstance(dlg, ModalDialog):
        return dlg.hwnd
    try:
        return int(dlg.handle)
    except Exception:
        return None


def _read_preview_text(preview) -> str:
    hwnd = _dialog_hwnd(preview)
    if hwnd:
        return _read_preview_text_from_hwnd(hwnd)
    try:
        edit = preview.child_window(control_type="Edit", found_index=0)
        if edit.exists(timeout=0.15):
            t = (edit.window_text() or "").strip()
            if t:
                return t
    except Exception:
        pass
    try:
        t = (preview.window_text() or "").strip()
        if t:
            return t
    except Exception:
        pass
    return ""


def _parse_preview(preview_text: str) -> tuple[int, str | None]:
    m_count = RECORD_COUNT_RE.search(preview_text)
    if not m_count:
        raise RuntimeError(
            "Import/Export Preview did not show a record count. Text was:\n"
            f"{preview_text[:500]}"
        )
    m_path = IMPORT_PATH_RE.search(preview_text)
    source = m_path.group(1).strip() if m_path else None
    return int(m_count.group(1)), source


def _build_label_destination(order, vendor_maps: "VendorMapRegistry") -> Path:
    from automation.worldship_cornerstone_master import CornerstoneOrderRow
    from automation.worldship_label_config import LABEL_ROOTS, label_extension
    from automation.worldship_vendor_map import VendorMapRegistry

    if not isinstance(order, CornerstoneOrderRow):
        raise TypeError("order must be CornerstoneOrderRow")
    if not isinstance(vendor_maps, VendorMapRegistry):
        raise TypeError("vendor_maps must be VendorMapRegistry")
    vendor_folder = vendor_maps.lookup(order.sku, order.retailer_key)
    root = LABEL_ROOTS.get(order.retailer_key)
    if root is None:
        raise ValueError(f"No label root configured for retailer key {order.retailer_key!r}.")
    dest_dir = root / vendor_folder
    if not dest_dir.is_dir():
        raise FileNotFoundError(
            f"Vendor folder not found for row {order.row_number}: {dest_dir}\n"
            f"  SKU={order.sku!r} → vendor {vendor_folder!r}, retailer={order.retailer_raw!r}"
        )
    ext = label_extension()
    # Full PURCHASE_ORDER cell (e.g. '48690515 Coarse 10.pdf'), not digits-only.
    filename = f"{order.po}{ext}" if ext else order.po
    return dest_dir / filename


def _verify_saved_label(dest: Path, order) -> None:
    """Hard stop if the wrong file was written — prevents cascading mis-saves."""
    from automation.worldship_cornerstone_master import CornerstoneOrderRow
    from automation.worldship_label_config import label_extension

    if not isinstance(order, CornerstoneOrderRow):
        raise TypeError("order must be CornerstoneOrderRow")
    if not dest.is_file():
        raise RuntimeError(f"Label file missing after save: {dest}")
    ext = label_extension()
    expected_name = f"{order.po}{ext}" if ext else order.po
    if dest.name != expected_name:
        raise RuntimeError(
            f"Saved file name mismatch for row {order.row_number}: "
            f"expected {expected_name!r}, found {dest.name!r} at\n  {dest}"
        )
    try:
        size = dest.stat().st_size
    except OSError as exc:
        raise RuntimeError(f"Cannot read saved label: {dest}") from exc
    min_bytes = 800
    raw = (os.environ.get("WORLDSHIP_MIN_LABEL_BYTES") or "800").strip()
    try:
        min_bytes = max(100, int(raw))
    except ValueError:
        pass
    if size < min_bytes:
        raise RuntimeError(
            f"Saved label too small ({size} bytes) for row {order.row_number}, PO {order.po!r}: {dest}"
        )
    _log(f"Verified on disk: {dest.name} ({size:,} bytes)")


def _wait_until_save_dialog_gone(*, previous_hwnd: int = 0, max_s: float = 12.0) -> None:
    from automation.windows_save_as import wait_for_save_dialog_handoff

    if not previous_hwnd:
        return
    if not wait_for_save_dialog_handoff(previous_hwnd, timeout_s=max_s, saved_dest=None):
        raise RuntimeError(
            "The same Save Print Output As window is still open after save. "
            "Close it manually or fix the previous label."
        )


def _pause_between_labels(*, previous_hwnd: int, saved_dest: Path) -> None:
    from automation.windows_save_as import wait_for_save_dialog_handoff
    from automation.worldship_label_config import label_save_gap_s

    gap_s = label_save_gap_s()
    _log(f"Waiting {gap_s:.0f}s for next shipment…")
    time.sleep(gap_s)
    if not wait_for_save_dialog_handoff(
        previous_hwnd, timeout_s=8.0, saved_dest=saved_dest
    ):
        _log(
            "WARN: Save Print Output window may still be open — "
            "continuing to wait for the next label dialog."
        )


def _log_failed_label_summary(failed: list[dict[str, str]]) -> None:
    if not failed:
        return
    _log("=" * 62)
    _log(
        f"LABELS NOT SAVED ON DISK ({len(failed)}) — re-print these PO(s) manually:"
    )
    for entry in failed:
        _log(
            f"  PO {entry['po']!r} (row {entry['row']}, save {entry['index']}/{entry['total']}) "
            f"→ {entry['dest']}"
        )
    _log("=" * 62)


def _run_save_label_phase(plan) -> tuple[int, list[dict[str, str]]]:
    """Phase 1: consecutive Save dialogs — one per save_items entry, strict verify."""
    from automation.windows_save_as import (
        fill_save_as_dialog,
        wait_for_next_save_dialog,
        wait_for_save_as_dialog,
    )
    from automation.worldship_label_config import save_dialog_timeout_s

    items = plan.save_items
    if not items:
        return 0, []

    _log(f"=== Phase 1/2: SAVE {len(items)} label(s) to share ===")
    from automation.windows_save_as import reset_last_save_folder

    reset_last_save_folder()
    _log(
        "IMPORTANT: While Automatic Processing Progress is open, do NOT click Stop. "
        "WorldShip will pause the batch and later Save dialogs will not appear. "
        "Only click Save on each Save Print Output As window."
    )
    last_hwnd = 0
    saved = 0
    failed: list[dict[str, str]] = []

    for item in items:
        order = item.order
        dest = item.dest
        if item.index == 1:
            timeout_s = save_dialog_timeout_s(first=True)
            _log(
                f"Waiting for first Save dialog — save {item.index}/{len(items)}, "
                f"row {order.row_number}, PO {order.po!r}…"
            )
            dialog_hwnd = wait_for_save_as_dialog(timeout_s=timeout_s)
        else:
            from automation.windows_save_as import (
                _dialog_still_open,
                _find_filename_edit_hwnd,
                _filename_matches,
                find_save_as_dialog_hwnd,
            )

            timeout_s = save_dialog_timeout_s(first=False)
            prior_dest = items[saved - 1].dest
            dialog_hwnd = find_save_as_dialog_hwnd(log=False)
            next_ready = False
            if dialog_hwnd:
                if not _dialog_still_open(last_hwnd):
                    next_ready = True
                else:
                    edit = _find_filename_edit_hwnd(dialog_hwnd)
                    if edit and not _filename_matches(edit, prior_dest):
                        next_ready = True
            if next_ready and dialog_hwnd:
                _log(
                    f"Next Save dialog already open — save {item.index}/{len(items)}, "
                    f"row {order.row_number}, PO {order.po!r}"
                )
            else:
                _wait_for_processing_after_save(
                    saves_completed=saved,
                    saves_total=len(items),
                    timeout_s=timeout_s,
                )
                _log(
                    f"Waiting for next Save dialog — save {item.index}/{len(items)}, "
                    f"row {order.row_number}, PO {order.po!r}…"
                )
                dialog_hwnd = wait_for_next_save_dialog(
                    previous_hwnd=last_hwnd, timeout_s=timeout_s
                )

        if not dialog_hwnd:
            raise TimeoutError(
                f"Timed out waiting for Save dialog {item.index}/{len(items)} "
                f"(row {order.row_number}, PO {order.po!r}). "
                "Stop WorldShip, fix the previous label, and re-run."
            )
        last_hwnd = dialog_hwnd

        _log(
            f"--- Save {item.index}/{len(items)}: row {order.row_number}, "
            f"PO {order.po!r}, SKU {order.sku!r}, retailer {order.retailer_raw!r} ---"
        )
        _log(f"  → folder: {dest.parent}")
        _log(f"  → file:   {dest.name}")

        from automation.windows_save_as import recover_after_failed_worldship_save

        save_ok = False
        try:
            save_ok = fill_save_as_dialog(
                dest,
                timeout_s=90.0,
                dialog_hwnd=dialog_hwnd,
                po=order.po,
                sku=order.sku,
            )
            if save_ok:
                _verify_saved_label(dest, order)
        except Exception as exc:
            _log(f"WARN: save verification failed for PO {order.po!r}: {exc}")
            save_ok = False

        if not save_ok:
            failed.append(
                {
                    "po": order.po,
                    "row": str(order.row_number),
                    "index": str(item.index),
                    "total": str(len(items)),
                    "dest": str(dest),
                }
            )
            _log(
                f"WARN: skipping PO {order.po!r} (save {item.index}/{len(items)}) — "
                "file not on disk; continuing batch."
            )
            next_hwnd = recover_after_failed_worldship_save(
                previous_hwnd=last_hwnd,
                timeout_s=min(90.0, save_dialog_timeout_s(first=False)),
            )
            if next_hwnd:
                last_hwnd = next_hwnd
            continue

        saved += 1
        _log(f"Completed save {saved}/{len(items)}: {dest.name}")
        if item.index < len(items):
            _pause_between_labels(previous_hwnd=last_hwnd, saved_dest=dest)

    _log(f"Phase 1 complete: {saved}/{len(items)} label(s) saved and verified.")
    if failed:
        _log(
            f"WARN: {len(failed)} label(s) were not saved on disk — "
            "batch will continue; re-print failed POs listed below."
        )
        _log_failed_label_summary(failed)
    return saved, failed


def _run_warehouse_print_phase(plan) -> int:
    """Phase 2: warehouse-print rows — WorldShip prints; automation waits for Close."""
    orders = plan.print_orders
    if not orders:
        return 0

    _log(f"=== Phase 2/2: WAREHOUSE PRINT {len(orders)} label(s) ===")
    for order in orders:
        _log(
            f"  print row {order.row_number}: PO {order.po!r}, SKU {order.sku!r}"
        )
    _log(
        "WorldShip is printing warehouse labels — automation will wait for "
        "100% and click Close when ready (do not click Stop)."
    )
    return len(orders)


def _save_shipping_labels(app, main) -> int:
    from automation.warehouse_print_vendors import load_warehouse_print_vendors
    from automation.worldship_cornerstone_master import load_cornerstone_orders
    from automation.worldship_label_work_plan import (
        log_worldship_label_work_plan,
        partition_worldship_label_rows,
    )
    from automation.worldship_vendor_map import VendorMapRegistry

    _log("Loading CornerstoneMaster for label routing…")
    load_warehouse_print_vendors(reload=True)
    orders = load_cornerstone_orders()
    vendor_maps = VendorMapRegistry()
    plan = partition_worldship_label_rows(
        orders, vendor_maps, build_destination=_build_label_destination
    )
    log_worldship_label_work_plan(plan, vendor_maps)

    saved, failed_labels = _run_save_label_phase(plan)
    if failed_labels:
        _log(
            f"Continuing WorldShip batch with {len(failed_labels)} label(s) to re-print later."
        )

    if plan.save_items and plan.print_orders:
        from automation.worldship_label_config import label_save_gap_s

        gap_s = label_save_gap_s()
        _log(f"Save phase done; waiting {gap_s:.0f}s before warehouse print phase…")
        time.sleep(gap_s)
        from automation.windows_save_as import wait_until_save_dialog_closed

        if not wait_until_save_dialog_closed(timeout_s=30.0):
            _log("WARN: Save dialog still visible entering print phase — continuing.")

    printed = _run_warehouse_print_phase(plan)
    if printed != len(plan.print_orders):
        raise RuntimeError(
            f"Expected {len(plan.print_orders)} warehouse print(s), counted {printed}."
        )

    from automation.worldship_after_print import run_after_print_workflow

    run_after_print_workflow(app, main, print_label_count=printed)

    summary = (
        f"Label processing complete: {saved} saved to share, "
        f"{printed} warehouse print, End of Day + Batch Export done."
    )
    if failed_labels:
        summary += f" {len(failed_labels)} PO(s) need manual re-print (see list above)."
    _log(summary)
    return saved


def run_worldship_batch_import_start() -> WorldShipBatchImportResult:
    """
    WorldShip: Import-Export → Batch Import → auto-process → preview Next →
    Smart Pickup Yes → wait for processing → save each label from CornerstoneMaster.
    """
    Application, _ = _require_pywinauto()
    startup_timeout_s = _startup_timeout_s()

    app, cold_start = _connect_or_start(Application, startup_timeout_s=startup_timeout_s)
    main = _resolve_main_window(app, cold_start=cold_start)

    _focus_main_window(main)
    from automation.worldship_ribbon_click import (
        _fast_ribbon_clicks_enabled,
        _import_pacing_s,
    )

    if _fast_ribbon_clicks_enabled(main):
        _log("Fast import pacing enabled (calibrated ribbon / Remote Workstation).")

    after_fg_s = _import_pacing_s("WORLDSHIP_AFTER_FOREGROUND_S", 1.5, 0.4, main)
    if after_fg_s > 0:
        _log(f"Waiting {after_fg_s:.1f}s after foreground before Import-Export tab…")
        time.sleep(after_fg_s)

    from automation.worldship_ribbon_click import click_batch_import

    click_batch_import(main, log=_log, app=app)

    after_batch_open_s = _import_pacing_s("WORLDSHIP_AFTER_BATCH_IMPORT_OPEN_S", 2.5, 0.0, main)
    if after_batch_open_s > 0:
        _log(f"Waiting {after_batch_open_s:.1f}s for Batch Import wizard…")
        time.sleep(after_batch_open_s)
    else:
        _log("Waiting for Batch Import wizard…")
    wizard = _wait_for_batch_import_wizard(app, main, timeout_s=8.0)

    _log(f"Ensuring {AUTO_PROCESS_LABEL!r} is checked…")
    _ensure_checkbox_checked(wizard, AUTO_PROCESS_LABEL, timeout_s=5)

    before_next_s = _import_pacing_s("WORLDSHIP_BEFORE_NEXT_WAIT_S", 0.75, 0.35, main)
    if before_next_s > 0:
        _log(f"Waiting {before_next_s:.1f}s before Next…")
        time.sleep(before_next_s)

    _log("Clicking Next (wizard step 1)…")
    _click_dialog_button(wizard, "Next", title_hint="Batch Import", timeout_s=4)

    _log(f"Waiting for {PREVIEW_DIALOG_TITLE!r}…")
    preview = _find_modal_dialog(PREVIEW_DIALOG_TITLE, timeout_s=60)
    _click_preview_next(preview)

    from automation.worldship_label_config import processing_timeout_s

    from automation.worldship_cornerstone_master import load_cornerstone_orders
    from automation.worldship_label_work_plan import partition_worldship_label_rows
    from automation.worldship_vendor_map import VendorMapRegistry

    try:
        _orders = load_cornerstone_orders()
        _plan = partition_worldship_label_rows(
            _orders, VendorMapRegistry(), build_destination=_build_label_destination
        )
        _proc_count = len(_plan.save_items) + len(_plan.print_orders)
    except Exception:
        _proc_count = 4
    proc_timeout = processing_timeout_s(order_count=_proc_count or 4)
    _advance_after_preview_next(processing_timeout_s=proc_timeout)

    labels_saved = _save_shipping_labels(app, main)
    record_count = labels_saved
    import_source = None
    _log(f"Completed {labels_saved} label save(s).")

    return WorldShipBatchImportResult(
        record_count=record_count,
        import_source=import_source,
        preview_text="",
        labels_saved=labels_saved,
    )
