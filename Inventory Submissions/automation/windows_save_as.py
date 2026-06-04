"""Fill native Windows Save As / Save dialogs (Win32 — WorldShip Save Print Output)."""

from __future__ import annotations

import ctypes
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
        return bool(win32gui.FindWindowEx(hwnd, 0, "ComboBoxEx32", None))
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


def _enum_save_dialog_hwnds() -> list[tuple[int, int]]:
    try:
        import win32gui
    except ImportError:
        return []

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
    found.sort(key=lambda x: x[0], reverse=True)
    return found


def find_save_as_dialog_hwnd(*, log: bool = True) -> int:
    found = _enum_save_dialog_hwnds()
    if not found:
        return 0
    hwnd = found[0][1]
    if log:
        _log(f"Save dialog: {_dialog_title(hwnd)!r}")
    return hwnd


def _dialog_still_open(hwnd: int) -> bool:
    import win32gui

    try:
        return win32gui.IsWindow(hwnd) and win32gui.IsWindowVisible(hwnd)
    except Exception:
        return False


def _focus_dialog(hwnd: int) -> None:
    import win32gui

    try:
        win32gui.ShowWindow(hwnd, 5)
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass


def _safe_get_dlg_item(parent_hwnd: int, ctrl_id: int) -> int:
    """GetDlgItem raises error 1421 when the control id does not exist."""
    import win32gui

    try:
        child = win32gui.GetDlgItem(parent_hwnd, ctrl_id)
        return int(child) if child else 0
    except Exception:
        return 0


def _walk_descendants(parent_hwnd: int, visit) -> None:
    """Depth-first walk of all child windows (EnumChildWindows is one level only)."""
    import win32gui

    def _child_cb(child: int, _) -> bool:
        visit(child)
        _walk_descendants(child, visit)
        return True

    try:
        win32gui.EnumChildWindows(parent_hwnd, _child_cb, None)
    except Exception:
        pass


def _find_filename_edit_hwnd(parent_hwnd: int) -> int:
    """File name field in WorldShip Save Print Output (ComboBoxEx32 chain)."""
    import win32gui

    combo_boxes: list[int] = []

    def _visit(hwnd: int) -> None:
        if win32gui.GetClassName(hwnd) == "ComboBoxEx32":
            combo_boxes.append(hwnd)

    _walk_descendants(parent_hwnd, _visit)

    for combo_ex in combo_boxes:
        edit = win32gui.FindWindowEx(combo_ex, 0, "Edit", None)
        if edit:
            return edit
        combo = win32gui.FindWindowEx(combo_ex, 0, "ComboBox", None)
        if combo:
            edit = win32gui.FindWindowEx(combo, 0, "Edit", None)
            if edit:
                return edit

    edits: list[int] = []

    def _visit_edit(hwnd: int) -> None:
        if win32gui.GetClassName(hwnd) == "Edit" and win32gui.IsWindowVisible(hwnd):
            edits.append(hwnd)

    _walk_descendants(parent_hwnd, _visit_edit)
    if edits:
        return edits[-1]

    for ctrl_id in (1148, 1152, 1001):
        child = _safe_get_dlg_item(parent_hwnd, ctrl_id)
        if not child:
            continue
        edit = win32gui.FindWindowEx(child, 0, "Edit", None)
        if edit:
            return edit
    return 0


def _find_save_button_hwnd(parent_hwnd: int) -> int:
    import win32gui

    save_btn = 0

    def _visit(hwnd: int) -> None:
        nonlocal save_btn
        if save_btn:
            return
        if win32gui.GetClassName(hwnd) != "Button":
            return
        text = (win32gui.GetWindowText(hwnd) or "").replace("&", "").strip().lower()
        if text == "save":
            save_btn = hwnd

    _walk_descendants(parent_hwnd, _visit)
    if save_btn:
        return save_btn

    for ctrl_id in (1, 2):
        btn = _safe_get_dlg_item(parent_hwnd, ctrl_id)
        if btn:
            return btn
    return 0


def _read_edit_text(edit_hwnd: int) -> str:
    import win32con
    import win32gui

    try:
        n = win32gui.SendMessage(edit_hwnd, win32con.WM_GETTEXTLENGTH, 0, 0)
        if n <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(n + 2)
        win32gui.SendMessage(edit_hwnd, win32con.WM_GETTEXT, n + 1, buf)
        return buf.value.strip()
    except Exception:
        return ""


def _set_edit_text(edit_hwnd: int, text: str) -> bool:
    import win32con
    import win32gui

    try:
        win32gui.SendMessage(edit_hwnd, win32con.EM_SETSEL, 0, -1)
        win32gui.SendMessage(edit_hwnd, win32con.WM_SETTEXT, 0, text)
        return True
    except Exception:
        return False


def _filename_field_ok(edit_hwnd: int, dest: Path) -> bool:
    if not edit_hwnd:
        return False
    current = _read_edit_text(edit_hwnd)
    if not current:
        return False
    name = dest.name
    stem = dest.stem
    cur = current.strip().lower()
    return name.lower() in cur or stem.lower() in cur or cur.endswith(".pdf")


def _paste_into_edit(edit_hwnd: int, text: str, *, parent_hwnd: int) -> bool:
    import win32gui

    try:
        win32gui.SetForegroundWindow(parent_hwnd)
        win32gui.SetFocus(edit_hwnd)
    except Exception:
        pass
    time.sleep(0.15)
    if _set_edit_text(edit_hwnd, text):
        time.sleep(0.12)
        if _read_edit_text(edit_hwnd):
            return True
    _set_clipboard(text)
    _send_ctrl_a()
    _send_ctrl_v()
    time.sleep(0.25)
    return bool(_read_edit_text(edit_hwnd))


def _click_save_button(hwnd: int) -> bool:
    import win32con
    import win32gui

    btn = _find_save_button_hwnd(hwnd)
    if btn:
        try:
            win32gui.PostMessage(btn, win32con.BM_CLICK, 0, 0)
        except Exception:
            try:
                win32gui.SendMessage(btn, win32con.BM_CLICK, 0, 0)
            except Exception:
                pass
        _log("Clicked Save button.")
        return True
    _send_alt_key("s")
    _log("Sent Alt+S for Save.")
    return True


def _focus_filename_field_keyboard() -> None:
    """Common dialog accelerator: Alt+N focuses File name."""
    _send_alt_key("n")
    time.sleep(0.35)


def _try_keyboard_filename(hwnd: int, dest: Path) -> bool:
    """Alt+N → paste PO.pdf → verify → Save (no GetDlgItem)."""
    _focus_dialog(hwnd)
    time.sleep(0.45)
    _set_clipboard(dest.name)
    _focus_filename_field_keyboard()
    _send_ctrl_a()
    _send_ctrl_v()
    time.sleep(0.35)

    edit = _find_filename_edit_hwnd(hwnd)
    if edit:
        current = _read_edit_text(edit)
        _log(f"File name field: {current!r}")
        if not _filename_field_ok(edit, dest):
            _log("WARN: keyboard paste did not set filename — retrying focus.")
            _focus_dialog(hwnd)
            _focus_filename_field_keyboard()
            _send_ctrl_a()
            _send_ctrl_v()
            time.sleep(0.35)
            if not _filename_field_ok(edit, dest):
                return False
    else:
        _log("Filename edit not found; saving after keyboard paste anyway.")

    time.sleep(0.2)
    _click_save_button(hwnd)
    return True


def _try_wmset_filename(hwnd: int, dest: Path) -> bool:
    _focus_dialog(hwnd)
    time.sleep(0.4)
    edit = _find_filename_edit_hwnd(hwnd)
    if not edit:
        _log("WARN: no filename edit for WM_SETTEXT.")
        return False

    _log(f"WM_SETTEXT filename {dest.name!r}…")
    if not _paste_into_edit(edit, dest.name, parent_hwnd=hwnd):
        return False
    current = _read_edit_text(edit)
    _log(f"File name field: {current!r}")
    if not _filename_field_ok(edit, dest):
        return False
    time.sleep(0.2)
    _click_save_button(hwnd)
    return True


def _try_full_path_keyboard(hwnd: int, dest: Path) -> bool:
    """Paste full UNC path into File name."""
    _focus_dialog(hwnd)
    time.sleep(0.4)
    _set_clipboard(str(dest))
    _focus_filename_field_keyboard()
    _send_ctrl_a()
    _send_ctrl_v()
    time.sleep(0.35)
    edit = _find_filename_edit_hwnd(hwnd)
    if edit and dest.name.lower() not in _read_edit_text(edit).lower():
        return False
    time.sleep(0.2)
    _click_save_button(hwnd)
    return True


def _navigate_to_folder(hwnd: int, folder: Path) -> None:
    import win32con

    _focus_dialog(hwnd)
    time.sleep(0.35)
    _set_clipboard(str(folder))
    _send_alt_key("d")
    time.sleep(0.35)
    _send_ctrl_a()
    _send_ctrl_v()
    time.sleep(0.2)
    _send_vk(win32con.VK_RETURN)
    time.sleep(0.9)


def _try_folder_and_filename(hwnd: int, dest: Path) -> bool:
    _navigate_to_folder(hwnd, dest.parent)
    return _try_keyboard_filename(hwnd, dest)


def _try_pywinauto_filename(hwnd: int, dest: Path) -> bool:
    try:
        from pywinauto import Application
    except ImportError:
        return False

    try:
        app = Application(backend="win32").connect(handle=hwnd, visible_only=True)
        dlg = app.window(handle=hwnd)
        dlg.set_focus()
        time.sleep(0.4)
        combo = dlg.child_window(class_name="ComboBoxEx32")
        combo.set_focus()
        combo.type_keys("^a", pause=0.05)
        combo.type_keys(dest.name, with_spaces=True, pause=0.02)
        time.sleep(0.2)
        edit = _find_filename_edit_hwnd(hwnd)
        if edit and not _filename_field_ok(edit, dest):
            return False
        for btn in dlg.descendants(class_name="Button"):
            if (btn.window_text() or "").replace("&", "").strip().lower() == "save":
                btn.click_input()
                _log("Clicked Save (pywinauto).")
                return True
        dlg.type_keys("%s", pause=0.05)
        return True
    except Exception as exc:
        _log(f"WARN: pywinauto filename failed: {exc}")
        return False


def dismiss_overwrite_prompt(*, timeout_s: float = 4.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        hwnd = find_save_as_dialog_hwnd(log=False)
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
            for _ in range(40):
                if dest.is_file() and dest.stat().st_size >= min_bytes:
                    if dest.stat().st_mtime >= started - 10:
                        _log(f"Saved ({dest.stat().st_size:,} bytes).")
                        return True
                time.sleep(0.25)
            _log("Dialog closed but file not found at destination yet.")
            return False
        time.sleep(0.3)

    if _dialog_still_open(hwnd):
        _log("ERROR: Save dialog still open — filename may be empty or Save was blocked.")
    return dest.is_file() and dest.stat().st_size >= min_bytes


def fill_save_as_dialog(
    dest: Path,
    *,
    timeout_s: float = 45.0,
    min_bytes: int = 100,
) -> bool:
    """Save Print Output As: PO filename in vendor folder, then confirm."""
    dest = dest.resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    deadline = time.monotonic() + timeout_s

    hwnd = find_save_as_dialog_hwnd(log=True)
    if not hwnd:
        _log("ERROR: Save As dialog not found.")
        return False

    _log(f"Target folder: {dest.parent}")
    _log(f"Target file:   {dest.name}")

    steps = (
        ("keyboard Alt+N PO", _try_keyboard_filename),
        ("pywinauto ComboBoxEx32", _try_pywinauto_filename),
        ("WM_SETTEXT filename", _try_wmset_filename),
        ("full path keyboard", _try_full_path_keyboard),
        ("folder + PO keyboard", _try_folder_and_filename),
    )

    for step_name, fn in steps:
        if time.monotonic() >= deadline:
            break
        if not _dialog_still_open(hwnd):
            hwnd = find_save_as_dialog_hwnd(log=True)
            if not hwnd:
                break
        _focus_dialog(hwnd)
        time.sleep(0.3)
        try:
            attempted = fn(hwnd, dest)
        except Exception as exc:
            _log(f"WARN: {step_name} failed: {exc}")
            continue
        if not attempted:
            _log(f"WARN: {step_name} could not fill dialog.")
            continue
        time.sleep(0.5)
        dismiss_overwrite_prompt()
        if _wait_save_complete(hwnd, dest, started=started, deadline=deadline, min_bytes=min_bytes):
            return True
        _log(f"WARN: {step_name} — save not complete, trying next approach.")

    return False


def wait_for_save_as_dialog(*, timeout_s: float) -> int:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        found = _enum_save_dialog_hwnds()
        if found:
            hwnd = found[0][1]
            _log(f"Save dialog: {_dialog_title(hwnd)!r}")
            return hwnd
        time.sleep(0.35)
    return 0
