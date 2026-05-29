"""Fill native Windows Save As / Save dialogs (Win32)."""

from __future__ import annotations

import time
from pathlib import Path


def _set_clipboard(text: str) -> None:
    import win32clipboard
    import win32con

    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
    finally:
        win32clipboard.CloseClipboard()


def _send_vk(vk: int) -> None:
    import win32api
    import win32con

    win32api.keybd_event(vk, 0, 0, 0)
    win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)


def _send_ctrl_v() -> None:
    import win32api
    import win32con

    win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
    win32api.keybd_event(ord("V"), 0, 0, 0)
    win32api.keybd_event(ord("V"), 0, win32con.KEYEVENTF_KEYUP, 0)
    win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)


def find_save_as_dialog_hwnd() -> int:
    try:
        import win32gui
    except ImportError:
        return 0

    found: list[int] = []

    def _cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        cls = win32gui.GetClassName(hwnd)
        title = (win32gui.GetWindowText(hwnd) or "").lower()
        if cls != "#32770":
            return True
        if any(
            hint in title
            for hint in (
                "save as",
                "save print output as",
                "save shipment",
                "save label",
            )
        ) or title == "save":
            found.append(hwnd)
        return True

    win32gui.EnumWindows(_cb, None)
    return found[0] if found else 0


def _click_dialog_button(hwnd: int, labels: tuple[str, ...]) -> bool:
    import win32con
    import win32gui

    for dlg_id in (1, 2, 6, 7):
        try:
            btn = win32gui.GetDlgItem(hwnd, dlg_id)
            if not btn:
                continue
            text = (win32gui.GetWindowText(btn) or "").strip().lower()
            if text in labels or text.replace("&", "") in labels:
                win32gui.PostMessage(btn, win32con.BM_CLICK, 0, 0)
                return True
        except Exception:
            continue
    return False


def dismiss_overwrite_prompt(*, timeout_s: float = 4.0) -> None:
    try:
        import win32gui
    except ImportError:
        return

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        hwnd = find_save_as_dialog_hwnd()
        if not hwnd:
            time.sleep(0.15)
            continue
        title = (win32gui.GetWindowText(hwnd) or "").lower()
        if "confirm" in title or "replace" in title or "already exists" in title:
            if _click_dialog_button(
                hwnd,
                ("yes", "ok", "&yes", "&ok", "replace", "&replace"),
            ):
                return
        time.sleep(0.15)


def fill_save_as_dialog(
    dest: Path,
    *,
    timeout_s: float = 20.0,
    min_bytes: int = 100,
) -> bool:
    """Paste full destination path into Save As and confirm."""
    try:
        import win32con
        import win32gui
    except ImportError:
        return False

    dest.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        hwnd = find_save_as_dialog_hwnd()
        if hwnd:
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                pass
            time.sleep(0.35)
            edit = win32gui.FindWindowEx(hwnd, 0, "ComboBoxEx32", None)
            if edit:
                edit = win32gui.FindWindowEx(edit, 0, "ComboBox", None)
            if not edit:
                edit = win32gui.FindWindowEx(hwnd, 0, "Edit", None)
            if edit:
                try:
                    _set_clipboard(str(dest))
                    win32gui.SetForegroundWindow(hwnd)
                    time.sleep(0.15)
                    _send_ctrl_v()
                    time.sleep(0.2)
                    if not _click_dialog_button(hwnd, ("save", "&save")):
                        save_btn = win32gui.GetDlgItem(hwnd, 1)
                        if save_btn:
                            win32gui.PostMessage(save_btn, win32con.BM_CLICK, 0, 0)
                        else:
                            _send_vk(win32con.VK_RETURN)
                    dismiss_overwrite_prompt()
                    for _ in range(60):
                        if dest.is_file() and dest.stat().st_size >= min_bytes:
                            return True
                        time.sleep(0.25)
                except Exception:
                    pass
        time.sleep(0.25)
    dismiss_overwrite_prompt()
    return dest.is_file() and dest.stat().st_size >= min_bytes


def wait_for_save_as_dialog(*, timeout_s: float) -> int:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        hwnd = find_save_as_dialog_hwnd()
        if hwnd:
            return hwnd
        time.sleep(0.25)
    return 0
