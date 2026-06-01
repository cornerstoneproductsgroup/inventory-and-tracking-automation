"""Fill native Windows Save As / Save dialogs (Win32 + pywinauto UIA)."""

from __future__ import annotations

import time
from pathlib import Path


def _log(msg: str) -> None:
    print(f"[worldship/save] {msg}", flush=True)


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


def _send_alt_key(ch: str) -> None:
    import win32api
    import win32con

    vk = ord(ch.upper())
    win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
    win32api.keybd_event(vk, 0, 0, 0)
    win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)
    win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)


def _send_ctrl_v() -> None:
    import win32api
    import win32con

    win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
    win32api.keybd_event(ord("V"), 0, 0, 0)
    win32api.keybd_event(ord("V"), 0, win32con.KEYEVENTF_KEYUP, 0)
    win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)


def _send_ctrl_a() -> None:
    import win32api
    import win32con

    win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
    win32api.keybd_event(ord("A"), 0, 0, 0)
    win32api.keybd_event(ord("A"), 0, win32con.KEYEVENTF_KEYUP, 0)
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


def _dialog_still_open(hwnd: int) -> bool:
    import win32gui

    try:
        return win32gui.IsWindow(hwnd) and win32gui.IsWindowVisible(hwnd)
    except Exception:
        return False


def _focus_dialog(hwnd: int) -> None:
    import win32gui

    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass


def _fill_via_pywinauto(hwnd: int, dest: Path) -> bool:
    """Drive the visible File name field and Save button via UIA."""
    from pywinauto import Application

    dest_str = str(dest)
    app = Application(backend="uia").connect(handle=hwnd)
    dlg = app.window(handle=hwnd)
    dlg.set_focus()
    time.sleep(0.35)

    filename_set = False
    for combo in dlg.descendants(control_type="ComboBox"):
        try:
            label = (combo.element_info.name or combo.window_text() or "").lower()
            if "file name" not in label and "filename" not in label:
                continue
            combo.set_focus()
            combo.type_keys("^a", pause=0.05)
            combo.type_keys(dest_str, with_spaces=True, pause=0.02)
            filename_set = True
            _log("Set path via UIA File name combo.")
            break
        except Exception:
            continue

    if not filename_set:
        for edit in dlg.descendants(control_type="Edit"):
            try:
                if not edit.is_visible():
                    continue
                label = (edit.element_info.name or "").lower()
                if label and "file name" not in label and "search" in label:
                    continue
                edit.set_focus()
                edit.set_edit_text(dest_str)
                filename_set = True
                _log("Set path via UIA Edit control.")
                break
            except Exception:
                continue

    if not filename_set:
        return False

    time.sleep(0.25)
    for btn in dlg.descendants(control_type="Button"):
        try:
            text = (btn.window_text() or "").strip().replace("&", "")
            if text.lower() == "save":
                btn.click_input()
                _log("Clicked Save (UIA).")
                return True
        except Exception:
            continue

    dlg.type_keys("%s", pause=0.05)
    _log("Sent Alt+S (UIA).")
    return True


def _fill_via_keyboard(hwnd: int, dest: Path, *, split_path: bool) -> bool:
    """Use standard Save dialog accelerators: Alt+D (folder) + Alt+N (name) or Alt+N full path."""
    import win32con
    import win32gui

    _focus_dialog(hwnd)
    time.sleep(0.45)

    if split_path:
        _set_clipboard(str(dest.parent))
        _send_alt_key("d")
        time.sleep(0.25)
        _send_ctrl_a()
        _send_ctrl_v()
        time.sleep(0.15)
        _send_vk(win32con.VK_RETURN)
        time.sleep(0.65)
        _set_clipboard(dest.name)
        _send_alt_key("n")
        time.sleep(0.2)
        _send_ctrl_a()
        _send_ctrl_v()
        _log(f"Keyboard: folder then filename {dest.name!r}.")
    else:
        _set_clipboard(str(dest))
        _send_alt_key("n")
        time.sleep(0.2)
        _send_ctrl_a()
        _send_ctrl_v()
        _log("Keyboard: full path in File name (Alt+N).")

    time.sleep(0.25)
    _send_alt_key("s")
    _log("Keyboard: Alt+S Save.")
    return True


def dismiss_overwrite_prompt(*, timeout_s: float = 4.0) -> None:
    import win32con
    import win32gui

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        hwnd = find_save_as_dialog_hwnd()
        if not hwnd:
            return
        title = (win32gui.GetWindowText(hwnd) or "").lower()
        if "confirm" in title or "replace" in title or "already exists" in title:
            _send_alt_key("y")
            return
        time.sleep(0.15)


def _wait_save_complete(
    hwnd: int,
    dest: Path,
    *,
    started: float,
    deadline: float,
    min_bytes: int,
) -> bool:
    while time.monotonic() < deadline:
        if not _dialog_still_open(hwnd):
            for _ in range(24):
                if dest.is_file() and dest.stat().st_size >= min_bytes:
                    if dest.stat().st_mtime >= started - 5:
                        _log(f"Saved ({dest.stat().st_size:,} bytes).")
                        return True
                time.sleep(0.25)
            _log("Dialog closed but new file not found at destination.")
            return False
        time.sleep(0.25)

    if _dialog_still_open(hwnd):
        _log("ERROR: Save As dialog still open after Save click.")
        return False
    return dest.is_file() and dest.stat().st_size >= min_bytes


def fill_save_as_dialog(
    dest: Path,
    *,
    timeout_s: float = 45.0,
    min_bytes: int = 100,
) -> bool:
    """Fill Save Print Output As and confirm — success only when dialog closes."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    deadline = time.monotonic() + timeout_s

    hwnd = find_save_as_dialog_hwnd()
    if not hwnd:
        _log("ERROR: Save As dialog not found.")
        return False

    _log(f"Saving to: {dest}")
    methods = (
        ("pywinauto", lambda: _fill_via_pywinauto(hwnd, dest)),
        ("keyboard-full", lambda: _fill_via_keyboard(hwnd, dest, split_path=False)),
        ("keyboard-split", lambda: _fill_via_keyboard(hwnd, dest, split_path=True)),
    )

    for name, fn in methods:
        if not _dialog_still_open(hwnd):
            break
        _focus_dialog(hwnd)
        time.sleep(0.2)
        try:
            fn()
        except Exception as exc:
            _log(f"WARN: {name} failed: {exc}")
            continue
        time.sleep(0.35)
        dismiss_overwrite_prompt()
        if _wait_save_complete(hwnd, dest, started=started, deadline=deadline, min_bytes=min_bytes):
            return True
        _log(f"WARN: {name} did not complete save — trying next method.")

    return False


def wait_for_save_as_dialog(*, timeout_s: float) -> int:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        hwnd = find_save_as_dialog_hwnd()
        if hwnd:
            return hwnd
        time.sleep(0.25)
    return 0
