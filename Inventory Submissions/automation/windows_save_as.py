"""Fill native Windows Save As / Save dialogs (Win32 — WorldShip Save Print Output)."""

from __future__ import annotations

import ctypes
import os
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


def _folder_nav_pause_s() -> float:
    raw = (os.environ.get("WORLDSHIP_SAVE_FOLDER_NAV_S") or "1.4").strip()
    try:
        return max(0.8, float(raw))
    except ValueError:
        return 1.4


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


def _walk_descendants(parent_hwnd: int, visit) -> None:
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
    return edits[-1] if edits else 0


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
    return save_btn


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


def _filename_matches(edit_hwnd: int, dest: Path) -> bool:
    if not edit_hwnd:
        return False
    current = _read_edit_text(edit_hwnd).strip().lower()
    if not current:
        return False
    want_stem = dest.stem.strip().lower()
    want_name = dest.name.strip().lower()
    return (
        current == want_name
        or current == want_stem
        or want_stem in current
        or current.startswith(want_stem)
    )


def _click_save_button(hwnd: int) -> bool:
    """
    Click the Save button on the Save Print Output As dialog only.

    Never send Alt+S or Enter — Alt+S activates Stop on Automatic Processing Progress
    if that window has focus, which halts the rest of the batch.
    """
    import win32con
    import win32gui

    _focus_dialog(hwnd)
    time.sleep(0.2)
    btn = _find_save_button_hwnd(hwnd)
    if not btn:
        _log("ERROR: Save button not found on Save Print Output As dialog.")
        return False
    try:
        win32gui.SendMessage(btn, win32con.BM_CLICK, 0, 0)
    except Exception:
        try:
            win32gui.PostMessage(btn, win32con.BM_CLICK, 0, 0)
        except Exception:
            _log("ERROR: could not click Save button.")
            return False
    time.sleep(0.25)
    _log("Clicked Save button (no keyboard shortcuts).")
    return True


def dismiss_save_as_dialog_esc() -> None:
    """Close a stray Save dialog without saving (warehouse-print rows)."""
    import win32con

    hwnd = find_save_as_dialog_hwnd(log=False)
    if not hwnd:
        return
    _focus_dialog(hwnd)
    time.sleep(0.15)
    _send_vk(win32con.VK_ESCAPE)
    time.sleep(0.4)
    _log("Dismissed Save dialog (Escape).")


def _navigate_to_folder(hwnd: int, folder: Path) -> None:
    """Always set folder via address bar (Alt+D) — do not assume WorldShip opened the right place."""
    import win32con

    folder_str = str(folder)
    _log(f"Setting folder: {folder_str}")
    _focus_dialog(hwnd)
    time.sleep(0.45)
    _set_clipboard(folder_str)
    _send_alt_key("d")
    time.sleep(0.45)
    _send_ctrl_a()
    _send_ctrl_v()
    time.sleep(0.25)
    _send_vk(win32con.VK_RETURN)
    time.sleep(_folder_nav_pause_s())


def _clear_filename_field(hwnd: int) -> None:
    """Clear stale path/PO text before entering the current label."""
    import win32con

    _focus_dialog(hwnd)
    time.sleep(0.2)
    edit = _find_filename_edit_hwnd(hwnd)
    if edit:
        _set_edit_text(edit, "")
    _send_alt_key("n")
    time.sleep(0.3)
    _send_ctrl_a()
    _send_vk(win32con.VK_DELETE)
    time.sleep(0.15)


def _set_filename_only(hwnd: int, dest: Path, *, attempts: int = 4) -> bool:
    """File name = full PURCHASE_ORDER.pdf (folder must already be set)."""
    want = dest.name
    for attempt in range(1, attempts + 1):
        _clear_filename_field(hwnd)
        edit = _find_filename_edit_hwnd(hwnd)
        if edit:
            _set_edit_text(edit, want)
            time.sleep(0.2)
        _set_clipboard(want)
        _focus_dialog(hwnd)
        _send_alt_key("n")
        time.sleep(0.35)
        _send_ctrl_a()
        _send_ctrl_v()
        time.sleep(0.4)
        edit = _find_filename_edit_hwnd(hwnd) or edit
        if edit:
            if not _filename_matches(edit, dest):
                _set_edit_text(edit, want)
                time.sleep(0.2)
            current = _read_edit_text(edit)
            _log(f"File name field (try {attempt}/{attempts}): {current!r}")
            if _filename_matches(edit, dest):
                return True
        if attempt < attempts:
            _log(f"Filename not set yet; retrying ({attempt}/{attempts})…")
            time.sleep(0.35)
    _log(f"ERROR: could not set filename to {want!r}.")
    return False


def _assert_ready_to_save(hwnd: int, dest: Path) -> bool:
    edit = _find_filename_edit_hwnd(hwnd)
    if not edit or not _filename_matches(edit, dest):
        current = _read_edit_text(edit) if edit else ""
        _log(f"ERROR: refusing Save — filename field is {current!r}, want {dest.name!r}.")
        return False
    return True


def _file_stat_snapshot(dest: Path) -> tuple[float, int] | None:
    if not dest.is_file():
        return None
    try:
        st = dest.stat()
        return (st.st_mtime, st.st_size)
    except OSError:
        return None


def _dest_file_saved(
    dest: Path,
    *,
    min_bytes: int,
    save_clicked_at: float,
    before: tuple[float, int] | None,
) -> bool:
    """
    True only when dest was written or updated by this Save click.

    Uses wall-clock mtimes (never time.monotonic() — that caused false positives
    when an older PDF with the same name already existed on the share).
    """
    if not dest.is_file():
        return False
    try:
        st = dest.stat()
    except OSError:
        return False
    if st.st_size < min_bytes:
        return False
    if st.st_mtime < save_clicked_at - 2.0:
        return False
    if before is None:
        return True
    prev_mtime, prev_size = before
    return st.st_mtime > prev_mtime + 0.05 or st.st_size != prev_size


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


def _wait_for_save_result(
    hwnd: int,
    dest: Path,
    *,
    save_clicked_at: float,
    before: tuple[float, int] | None,
    timeout_s: float,
    min_bytes: int,
) -> bool:
    """Dialog must close AND the PDF must be newly written at the exact target path."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        dialog_open = _dialog_still_open(hwnd)
        file_ok = _dest_file_saved(
            dest,
            min_bytes=min_bytes,
            save_clicked_at=save_clicked_at,
            before=before,
        )
        if file_ok and not dialog_open:
            _log(f"Confirmed on disk: {dest} ({dest.stat().st_size:,} bytes)")
            return True
        if not dialog_open:
            for _ in range(60):
                if _dest_file_saved(
                    dest,
                    min_bytes=min_bytes,
                    save_clicked_at=save_clicked_at,
                    before=before,
                ):
                    _log(f"Confirmed on disk: {dest} ({dest.stat().st_size:,} bytes)")
                    return True
                time.sleep(0.25)
            _log(
                "Dialog closed but the PDF was not updated at the target path "
                "(filename not saved or wrong folder)."
            )
            return False
        time.sleep(0.3)
    if _dialog_still_open(hwnd):
        _log("ERROR: Save dialog still open after Save click.")
    elif _dest_file_saved(
        dest, min_bytes=min_bytes, save_clicked_at=save_clicked_at, before=before
    ):
        _log("ERROR: PDF updated but Save dialog is still open (will block next label).")
    return False


def _worldship_save_once(
    hwnd: int,
    dest: Path,
    *,
    before: tuple[float, int] | None,
    min_bytes: int,
) -> bool:
    """
    One label, one dialog: navigate folder → clear → full PURCHASE_ORDER filename → Save.
    """
    _navigate_to_folder(hwnd, dest.parent)
    hwnd = find_save_as_dialog_hwnd(log=False) or hwnd
    _focus_dialog(hwnd)
    time.sleep(0.4)
    if not _set_filename_only(hwnd, dest):
        return False
    if not _assert_ready_to_save(hwnd, dest):
        return False
    time.sleep(0.25)
    save_clicked_at = time.time()
    if not _click_save_button(hwnd):
        return False
    time.sleep(0.6)
    dismiss_overwrite_prompt()
    if _wait_for_save_result(
        hwnd,
        dest,
        save_clicked_at=save_clicked_at,
        before=before,
        timeout_s=28.0,
        min_bytes=min_bytes,
    ):
        return True
    # Dialog still open — one more Save attempt with fresh focus.
    if _dialog_still_open(hwnd) and _set_filename_only(hwnd, dest):
        _log("Retrying Save click on same dialog…")
        save_clicked_at = time.time()
        if not _click_save_button(hwnd):
            return False
        time.sleep(0.6)
        dismiss_overwrite_prompt()
        return _wait_for_save_result(
            hwnd,
            dest,
            save_clicked_at=save_clicked_at,
            before=before,
            timeout_s=20.0,
            min_bytes=min_bytes,
        )
    return False


def wait_for_save_dialog_handoff(
    previous_hwnd: int,
    *,
    timeout_s: float = 15.0,
    saved_dest: Path | None = None,
) -> bool:
    """
    Wait until the Save dialog we just used (previous_hwnd) is done.

    WorldShip often opens the *next* Save dialog within 1–2s. That is success:
    - previous hwnd closed, or
    - a Save dialog is visible but the filename is no longer the file we saved
      (Windows may reuse the same hwnd for the new dialog).
    """
    if not previous_hwnd:
        return True

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _dialog_still_open(previous_hwnd):
            current = find_save_as_dialog_hwnd(log=False)
            if current and current != previous_hwnd:
                _log(
                    "Previous Save dialog closed; next Save Print Output dialog "
                    "is already open."
                )
            else:
                _log("Previous Save dialog closed.")
            return True

        if saved_dest is not None:
            edit = _find_filename_edit_hwnd(previous_hwnd)
            if edit and not _filename_matches(edit, saved_dest):
                _log(
                    "Save dialog shows the next shipment (filename changed) — continuing."
                )
                return True

        time.sleep(0.2)

    if _dialog_still_open(previous_hwnd):
        if saved_dest is not None:
            edit = _find_filename_edit_hwnd(previous_hwnd)
            if edit and not _filename_matches(edit, saved_dest):
                return True
        _log(
            "ERROR: Same Save Print Output dialog is still open after save "
            f"(hwnd={previous_hwnd})."
        )
        return False
    return True


def wait_until_save_dialog_closed(
    *, timeout_s: float = 20.0, previous_hwnd: int = 0
) -> bool:
    """Wait for a dialog to close. Prefer wait_for_save_dialog_handoff when hwnd is known."""
    if previous_hwnd:
        return wait_for_save_dialog_handoff(previous_hwnd, timeout_s=timeout_s)

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not find_save_as_dialog_hwnd(log=False):
            return True
        time.sleep(0.35)
    if find_save_as_dialog_hwnd(log=False):
        _log("ERROR: Save dialog still visible.")
        return False
    return True


def _min_label_bytes() -> int:
    raw = (os.environ.get("WORLDSHIP_MIN_LABEL_BYTES") or "800").strip()
    try:
        return max(100, int(raw))
    except ValueError:
        return 800


def wait_for_next_save_dialog(*, previous_hwnd: int, timeout_s: float) -> int:
    """
    Return hwnd for the next Save Print Output dialog.

    Accepts the next dialog if it is already open once previous_hwnd has closed.
    """
    if previous_hwnd and not _dialog_still_open(previous_hwnd):
        current = find_save_as_dialog_hwnd(log=False)
        if current:
            _log(f"Next Save dialog (already open): {_dialog_title(current)!r}")
            return current

    if not wait_for_save_dialog_handoff(previous_hwnd, timeout_s=min(timeout_s, 12.0)):
        return 0

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        found = _enum_save_dialog_hwnds()
        if not found:
            time.sleep(0.35)
            continue
        hwnd = found[0][1]
        if previous_hwnd and hwnd == previous_hwnd and _dialog_still_open(previous_hwnd):
            time.sleep(0.35)
            continue
        _log(f"Next Save dialog: {_dialog_title(hwnd)!r}")
        return hwnd
    return 0


def fill_save_as_dialog(
    dest: Path,
    *,
    timeout_s: float = 45.0,
    min_bytes: int | None = None,
    dialog_hwnd: int | None = None,
) -> bool:
    """Save Print Output As: vendor folder + PO.pdf, verify exact path on disk."""
    dest = dest.resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    before = _file_stat_snapshot(dest)
    if min_bytes is None:
        min_bytes = _min_label_bytes()

    hwnd = dialog_hwnd or find_save_as_dialog_hwnd(log=dialog_hwnd is None)
    if not hwnd:
        _log("ERROR: Save As dialog not found.")
        return False

    _log(f"Target folder: {dest.parent}")
    _log(f"Target file:   {dest.name}")
    if before:
        _log(f"Existing file on share (mtime {before[0]:.0f}, {before[1]:,} bytes) — will require update after Save.")

    if _worldship_save_once(hwnd, dest, before=before, min_bytes=min_bytes):
        return wait_for_save_dialog_handoff(hwnd, timeout_s=12.0, saved_dest=dest)

    # Dialog closed without success — do NOT touch the next Save dialog.
    if not _dialog_still_open(hwnd):
        _log(
            "ERROR: Save dialog closed but file was not written to the target path. "
            "Fix folder/filename manually before the next label."
        )
        return False

    if time.monotonic() - started < timeout_s - 5:
        _log("Retrying same Save dialog once (folder + PO)…")
        _focus_dialog(hwnd)
        time.sleep(0.5)
        if _worldship_save_once(hwnd, dest, before=before, min_bytes=min_bytes):
            return wait_for_save_dialog_handoff(hwnd, timeout_s=12.0, saved_dest=dest)

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
