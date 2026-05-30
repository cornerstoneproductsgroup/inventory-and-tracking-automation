"""Fill native Windows Save As / Save dialogs (Win32)."""

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


def _enum_child_edits(hwnd: int, *, max_depth: int = 12) -> list[int]:
    import win32gui

    edits: list[int] = []

    def _walk(parent: int, depth: int) -> None:
        if depth > max_depth:
            return
        child = 0
        while True:
            child = win32gui.FindWindowEx(parent, child, None, None)
            if not child:
                break
            try:
                if win32gui.GetClassName(child) == "Edit":
                    edits.append(child)
            except Exception:
                pass
            _walk(child, depth + 1)

    _walk(hwnd, 0)
    return edits


def _find_filename_edit(hwnd: int) -> int:
    import win32gui

    for ctrl_id in (1148, 1001, 1152):
        try:
            edit = win32gui.GetDlgItem(hwnd, ctrl_id)
            if edit:
                return edit
        except Exception:
            pass

    combo = win32gui.FindWindowEx(hwnd, 0, "ComboBoxEx32", None)
    if combo:
        inner = win32gui.FindWindowEx(combo, 0, "ComboBox", None)
        if inner:
            edit = win32gui.FindWindowEx(inner, 0, "Edit", None)
            if edit:
                return edit

    edits = _enum_child_edits(hwnd)
    if edits:
        return edits[-1]
    return 0


def _get_edit_text(edit: int) -> str:
    import win32con
    import win32gui

    try:
        n = win32gui.SendMessage(edit, win32con.WM_GETTEXTLENGTH, 0, 0)
        if n <= 0:
            return (win32gui.GetWindowText(edit) or "").strip()
        n += 1
        buf = win32gui.PyMakeBuffer(n * 2)
        win32gui.SendMessage(edit, win32con.WM_GETTEXT, n, buf)
        return buf.tobytes().decode("utf-16-le", errors="ignore").split("\0")[0].strip()
    except Exception:
        return ""


def _set_edit_text(edit: int, text: str) -> None:
    import win32con
    import win32gui

    win32gui.SendMessage(edit, win32con.WM_SETTEXT, 0, text)
    win32gui.SendMessage(edit, win32con.EM_SETSEL, 0, -1)


def _click_labeled_button(hwnd: int, labels: tuple[str, ...]) -> bool:
    import win32con
    import win32gui

    targets = {label.lower().replace("&", "") for label in labels}
    clicked = False

    def _cb(child, _):
        nonlocal clicked
        try:
            if win32gui.GetClassName(child) != "Button":
                return True
            label = (win32gui.GetWindowText(child) or "").strip().lower().replace("&", "")
            if label in targets:
                win32gui.SendMessage(child, win32con.BM_CLICK, 0, 0)
                clicked = True
                return False
        except Exception:
            pass
        return True

    try:
        win32gui.EnumChildWindows(hwnd, _cb, None)
    except Exception:
        pass
    return clicked


def _click_save_button(hwnd: int) -> bool:
    import win32con
    import win32gui

    if _click_labeled_button(hwnd, ("save",)):
        return True
    for dlg_id in (1, 2):
        try:
            btn = win32gui.GetDlgItem(hwnd, dlg_id)
            if btn:
                win32gui.SendMessage(btn, win32con.BM_CLICK, 0, 0)
                return True
        except Exception:
            continue
    return False


def _dialog_still_open(hwnd: int) -> bool:
    import win32gui

    try:
        return win32gui.IsWindow(hwnd) and win32gui.IsWindowVisible(hwnd)
    except Exception:
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
            if _click_labeled_button(hwnd, ("yes", "ok", "replace")):
                return
        time.sleep(0.15)


def fill_save_as_dialog(
    dest: Path,
    *,
    timeout_s: float = 30.0,
    min_bytes: int = 100,
) -> bool:
    """Paste full destination path into Save As, click Save, verify dialog closes."""
    try:
        import win32con
        import win32gui
    except ImportError:
        _log("ERROR: pywin32 required for Save As automation.")
        return False

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest_str = str(dest)
    started = time.monotonic()
    deadline = time.monotonic() + timeout_s

    hwnd = find_save_as_dialog_hwnd()
    if not hwnd:
        _log("ERROR: Save As dialog not found.")
        return False

    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass
    time.sleep(0.5)

    edit = _find_filename_edit(hwnd)
    if not edit:
        _log("ERROR: Could not find filename field in Save As dialog.")
        return False

    filled = False
    for attempt in ("settext", "clipboard"):
        try:
            win32gui.SetForegroundWindow(hwnd)
            win32gui.SendMessage(edit, win32con.WM_SETFOCUS, 0, 0)
            time.sleep(0.15)
            if attempt == "settext":
                _set_edit_text(edit, dest_str)
            else:
                _set_clipboard(dest_str)
                _send_ctrl_a()
                _send_ctrl_v()
            time.sleep(0.25)
            current = _get_edit_text(edit)
            if dest.name.lower() in current.lower() or dest_str.lower() in current.lower():
                filled = True
                break
            _log(f"WARN: filename field shows {current!r} after {attempt}.")
        except Exception as exc:
            _log(f"WARN: fill attempt {attempt} failed: {exc}")

    if not filled:
        _log("ERROR: Could not fill filename field in Save As dialog.")
        return False

    _log(f"Filled Save As with: {dest_str}")
    _click_save_button(hwnd)
    time.sleep(0.3)
    dismiss_overwrite_prompt()

    while time.monotonic() < deadline:
        if not _dialog_still_open(hwnd):
            for _ in range(20):
                if dest.is_file() and dest.stat().st_size >= min_bytes:
                    if dest.stat().st_mtime >= started - 5:
                        _log(f"Saved ({dest.stat().st_size:,} bytes).")
                        return True
                time.sleep(0.25)
            _log("Dialog closed but file not found at destination.")
            return False
        time.sleep(0.25)

    if _dialog_still_open(hwnd):
        _log("ERROR: Save As dialog still open after Save click.")
        return False
    return dest.is_file() and dest.stat().st_size >= min_bytes


def wait_for_save_as_dialog(*, timeout_s: float) -> int:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        hwnd = find_save_as_dialog_hwnd()
        if hwnd:
            return hwnd
        time.sleep(0.25)
    return 0
