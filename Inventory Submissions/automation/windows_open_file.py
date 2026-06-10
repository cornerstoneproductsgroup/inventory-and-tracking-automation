"""Native Windows Open file dialog helper (UPS Browse for File)."""

from __future__ import annotations

import time
from pathlib import Path


def _log(msg: str) -> None:
    print(f"[open-file] {msg}", flush=True)


def _enum_open_dialog_hwnds() -> list[tuple[int, str]]:
    import win32gui

    found: list[tuple[int, str]] = []

    def _cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            if win32gui.GetClassName(hwnd) != "#32770":
                return True
            title = (win32gui.GetWindowText(hwnd) or "").strip()
            if title.lower() in ("open", "choose file to upload", "file upload"):
                found.append((hwnd, title))
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:
        pass
    return found


def _find_filename_edit(parent_hwnd: int) -> int:
    import win32gui

    edits: list[int] = []

    def _cb(child, _):
        try:
            cls = win32gui.GetClassName(child) or ""
            if cls in ("Edit", "ComboBox"):
                edits.append(child)
        except Exception:
            pass
        return True

    try:
        win32gui.EnumChildWindows(parent_hwnd, _cb, None)
    except Exception:
        pass
    return edits[-1] if edits else 0


def _click_open_button(parent_hwnd: int) -> bool:
    import win32con
    import win32gui

    target = "open"

    def _cb(child, _):
        try:
            if win32gui.GetClassName(child) != "Button":
                return True
            text = (win32gui.GetWindowText(child) or "").strip().lower().replace("&", "")
            if text == target:
                win32gui.PostMessage(child, win32con.BM_CLICK, 0, 0)
                return False
        except Exception:
            pass
        return True

    try:
        win32gui.EnumChildWindows(parent_hwnd, _cb, None)
        return True
    except Exception:
        return False


def fill_open_file_dialog(file_path: Path, *, timeout_s: float = 45.0) -> bool:
    """Set path in the Open dialog and click Open."""
    import win32con
    import win32gui

    path = file_path.resolve()
    if not path.is_file():
        _log(f"ERROR: file not found: {path}")
        return False

    deadline = time.monotonic() + timeout_s
    dlg_hwnd = 0
    while time.monotonic() < deadline:
        dialogs = _enum_open_dialog_hwnds()
        if dialogs:
            dlg_hwnd = dialogs[0][0]
            break
        time.sleep(0.2)

    if not dlg_hwnd:
        _log("ERROR: Open dialog not found.")
        return False

    try:
        win32gui.SetForegroundWindow(dlg_hwnd)
    except Exception:
        pass

    edit = _find_filename_edit(dlg_hwnd)
    if not edit:
        _log("ERROR: filename field not found in Open dialog.")
        return False

    try:
        win32gui.SendMessage(edit, win32con.EM_SETSEL, 0, -1)
        win32gui.SendMessage(edit, win32con.WM_SETTEXT, 0, str(path))
        time.sleep(0.3)
    except Exception as exc:
        _log(f"ERROR: could not set file path: {exc}")
        return False

    if not _click_open_button(dlg_hwnd):
        _log("ERROR: could not click Open button.")
        return False

    _log(f"Selected file: {path.name}")
    time.sleep(0.5)
    return True


def wait_for_open_dialog(*, timeout_s: float = 20.0) -> int | None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        dialogs = _enum_open_dialog_hwnds()
        if dialogs:
            return dialogs[0][0]
        time.sleep(0.2)
    return None
