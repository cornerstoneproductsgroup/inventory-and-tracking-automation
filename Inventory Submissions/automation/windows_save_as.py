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


def _dialog_title(hwnd: int) -> str:
    import win32gui

    try:
        return (win32gui.GetWindowText(hwnd) or "").strip()
    except Exception:
        return ""


def _dialog_has_filename_combo(hwnd: int) -> bool:
    import win32gui

    try:
        combo_ex = win32gui.FindWindowEx(hwnd, 0, "ComboBoxEx32", None)
        return bool(combo_ex)
    except Exception:
        return False


def _score_save_dialog(hwnd: int) -> int:
    title = _dialog_title(hwnd).lower()
    score = 0
    if _dialog_has_filename_combo(hwnd):
        score += 50
    if "save print output" in title:
        score += 40
    if "save as" in title or title == "save":
        score += 20
    if "save label" in title or "save shipment" in title:
        score += 15
    return score


def find_save_as_dialog_hwnd() -> int:
    try:
        import win32gui
    except ImportError:
        return 0

    found: list[tuple[int, int]] = []

    def _cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        if win32gui.GetClassName(hwnd) != "#32770":
            return True
        score = _score_save_dialog(hwnd)
        if score > 0:
            found.append((score, hwnd))
        return True

    win32gui.EnumWindows(_cb, None)
    if not found:
        return 0
    found.sort(key=lambda x: x[0], reverse=True)
    best = found[0][1]
    _log(f"Save dialog: { _dialog_title(best)!r}")
    return best


def _dialog_still_open(hwnd: int) -> bool:
    import win32gui

    try:
        return win32gui.IsWindow(hwnd) and win32gui.IsWindowVisible(hwnd)
    except Exception:
        return False


def _focus_dialog(hwnd: int) -> None:
    import win32gui

    try:
        win32gui.ShowWindow(hwnd, 5)  # SW_SHOW
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass


def _find_filename_edit_hwnd(parent_hwnd: int) -> int:
    import win32gui

    combo_ex = win32gui.FindWindowEx(parent_hwnd, 0, "ComboBoxEx32", None)
    if combo_ex:
        combo = win32gui.FindWindowEx(combo_ex, 0, "ComboBox", None)
        if combo:
            edit = win32gui.FindWindowEx(combo, 0, "Edit", None)
            if edit:
                return edit
        return combo_ex
    return win32gui.FindWindowEx(parent_hwnd, 0, "Edit", None)


def _click_save_button(hwnd: int) -> bool:
    import win32con
    import win32gui

    for btn_id in (1, 2, 3):
        btn = win32gui.GetDlgItem(hwnd, btn_id)
        if not btn:
            continue
        text = (win32gui.GetWindowText(btn) or "").strip().replace("&", "").lower()
        if "save" in text or btn_id == 1:
            try:
                win32gui.SendMessage(btn, win32con.BM_CLICK, 0, 0)
            except Exception:
                pass
            _log(f"Clicked Save button (id={btn_id}, text={text!r}).")
            return True
    return False


def _fill_via_win32_split(hwnd: int, dest: Path) -> bool:
    """
    Standard common dialog: Alt+D folder bar, then filename = PO (dest.name).
    Matches WorldShip Save Print Output (folder + PO file name).
    """
    import win32con
    import win32gui

    _focus_dialog(hwnd)
    time.sleep(0.55)

    _set_clipboard(str(dest.parent))
    _send_alt_key("d")
    time.sleep(0.35)
    _send_ctrl_a()
    _send_ctrl_v()
    time.sleep(0.2)
    _send_vk(win32con.VK_RETURN)
    time.sleep(0.85)

    edit = _find_filename_edit_hwnd(hwnd)
    if edit:
        try:
            win32gui.SetForegroundWindow(hwnd)
            win32gui.SetFocus(edit)
        except Exception:
            pass
        time.sleep(0.15)
        _set_clipboard(dest.name)
        _send_ctrl_a()
        _send_ctrl_v()
        _log(f"win32: folder {dest.parent} then filename {dest.name!r}.")
    else:
        _set_clipboard(dest.name)
        _send_alt_key("n")
        time.sleep(0.2)
        _send_ctrl_a()
        _send_ctrl_v()
        _log(f"win32: filename {dest.name!r} (Alt+N).")

    time.sleep(0.3)
    if not _click_save_button(hwnd):
        _send_alt_key("s")
    return True


def _fill_via_win32_full_path(hwnd: int, dest: Path) -> bool:
    """Paste full UNC path into the file name combo (SPS-style)."""
    import win32con
    import win32gui

    _focus_dialog(hwnd)
    time.sleep(0.45)
    edit = _find_filename_edit_hwnd(hwnd)
    if not edit:
        _log("win32 full: no filename edit/combo.")
        return False

    try:
        win32gui.SetForegroundWindow(hwnd)
        win32gui.SetFocus(edit)
    except Exception:
        pass
    time.sleep(0.15)
    _set_clipboard(str(dest))
    _send_ctrl_a()
    _send_ctrl_v()
    time.sleep(0.3)
    if not _click_save_button(hwnd):
        _send_vk(win32con.VK_RETURN)
    _log("win32: pasted full path.")
    return True


def _fill_via_pywinauto(hwnd: int, dest: Path, *, backend: str, split_path: bool) -> bool:
    from pywinauto import Application

    dest_str = str(dest)
    app = Application(backend=backend).connect(handle=hwnd, visible_only=True)
    dlg = app.window(handle=hwnd)
    dlg.set_focus()
    time.sleep(0.45)

    if split_path:
        try:
            dlg.type_keys("%d", pause=0.08)
            dlg.type_keys("^a", pause=0.05)
            dlg.type_keys(str(dest.parent), with_spaces=True, pause=0.02)
            dlg.type_keys("{ENTER}", pause=0.08)
            time.sleep(0.6)
        except Exception:
            pass

    filename_set = False
    for combo in dlg.descendants(control_type="ComboBox"):
        try:
            label = (combo.element_info.name or combo.window_text() or "").lower()
            if "file name" not in label and "filename" not in label:
                continue
            combo.set_focus()
            combo.type_keys("^a", pause=0.05)
            text = dest.name if split_path else dest_str
            combo.type_keys(text, with_spaces=True, pause=0.02)
            filename_set = True
            _log(f"Set path via {backend} File name ({'PO only' if split_path else 'full'}).")
            break
        except Exception:
            continue

    if not filename_set:
        for edit in dlg.descendants(control_type="Edit"):
            try:
                if not edit.is_visible():
                    continue
                edit.set_focus()
                edit.set_edit_text(dest.name if split_path else dest_str)
                filename_set = True
                _log(f"Set path via {backend} Edit.")
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
                _log(f"Clicked Save ({backend}).")
                return True
        except Exception:
            continue

    dlg.type_keys("%s", pause=0.05)
    _log(f"Sent Alt+S ({backend}).")
    return True


def _fill_via_keyboard(hwnd: int, dest: Path, *, split_path: bool) -> bool:
    """Accelerators: Alt+D folder, Alt+N file name, Alt+S save."""
    import win32con

    _focus_dialog(hwnd)
    time.sleep(0.5)

    if split_path:
        _set_clipboard(str(dest.parent))
        _send_alt_key("d")
        time.sleep(0.3)
        _send_ctrl_a()
        _send_ctrl_v()
        time.sleep(0.15)
        _send_vk(win32con.VK_RETURN)
        time.sleep(0.75)
        _set_clipboard(dest.name)
        _send_alt_key("n")
        time.sleep(0.2)
        _send_ctrl_a()
        _send_ctrl_v()
        _log(f"Keyboard: folder then PO filename {dest.name!r}.")
    else:
        _set_clipboard(str(dest))
        _send_alt_key("n")
        time.sleep(0.2)
        _send_ctrl_a()
        _send_ctrl_v()
        _log("Keyboard: full path in File name.")

    time.sleep(0.3)
    _send_alt_key("s")
    _log("Keyboard: Alt+S Save.")
    return True


def dismiss_overwrite_prompt(*, timeout_s: float = 4.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        hwnd = find_save_as_dialog_hwnd()
        if not hwnd:
            return
        title = _dialog_title(hwnd).lower()
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
            for _ in range(30):
                if dest.is_file() and dest.stat().st_size >= min_bytes:
                    if dest.stat().st_mtime >= started - 8:
                        _log(f"Saved ({dest.stat().st_size:,} bytes).")
                        return True
                time.sleep(0.25)
            _log("Dialog closed but new file not found at destination.")
            return False
        time.sleep(0.25)

    if _dialog_still_open(hwnd):
        _log("ERROR: Save As dialog still open after save attempts.")
        return False
    return dest.is_file() and dest.stat().st_size >= min_bytes


def fill_save_as_dialog(
    dest: Path,
    *,
    timeout_s: float = 45.0,
    min_bytes: int = 100,
) -> bool:
    """Fill Save Print Output As: vendor folder + PO filename, then confirm."""
    dest = dest.resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    deadline = time.monotonic() + timeout_s

    hwnd = find_save_as_dialog_hwnd()
    if not hwnd:
        _log("ERROR: Save As dialog not found.")
        return False

    _log(f"Saving to folder: {dest.parent}")
    _log(f"Filename (PO): {dest.name}")

    methods = (
        ("win32-split", lambda h: _fill_via_win32_split(h, dest)),
        ("keyboard-split", lambda h: _fill_via_keyboard(h, dest, split_path=True)),
        ("win32-full", lambda h: _fill_via_win32_full_path(h, dest)),
        ("pywinauto-win32-split", lambda h: _fill_via_pywinauto(h, dest, backend="win32", split_path=True)),
        ("pywinauto-uia-split", lambda h: _fill_via_pywinauto(h, dest, backend="uia", split_path=True)),
        ("keyboard-full", lambda h: _fill_via_keyboard(h, dest, split_path=False)),
    )

    for name, fn in methods:
        if time.monotonic() >= deadline:
            break
        hwnd = find_save_as_dialog_hwnd() or hwnd
        if not hwnd or not _dialog_still_open(hwnd):
            _log("Save dialog closed before all methods were tried.")
            break
        _focus_dialog(hwnd)
        time.sleep(0.25)
        try:
            fn(hwnd)
        except Exception as exc:
            _log(f"WARN: {name} failed: {exc}")
            continue
        time.sleep(0.4)
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
