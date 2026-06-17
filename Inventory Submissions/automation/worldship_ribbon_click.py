"""RDP-friendly ribbon/tab clicks for UPS WorldShip (multi-strategy)."""

from __future__ import annotations

import os
import re
import time
from typing import Callable

_RIBBON_VERSION = "ribbon-click-v7"
_AUTO_PROCESS_LABEL_SNIPPET = "process shipments automatically"
_BATCH_IMPORT_TITLE_SNIPPET = "batch import"


def _log_default(msg: str) -> None:
    print(f"[worldship] {msg}", flush=True)


def _control_name(el) -> str:
    try:
        t = (el.window_text() or "").strip()
        if t:
            return t
    except Exception:
        pass
    try:
        return (el.element_info.name or "").strip()
    except Exception:
        return ""


def _name_matches(label: str, needle: str) -> bool:
    a = label.lower().replace("&", "").replace("-", " ")
    b = needle.lower().replace("&", "").replace("-", " ")
    if a == b:
        return True
    if b in a:
        return True
    # "batch import" vs "batch  import"
    return re.sub(r"\s+", " ", a) == re.sub(r"\s+", " ", b)


def foreground_window_title() -> str:
    import win32gui

    try:
        hwnd = win32gui.GetForegroundWindow()
        return (win32gui.GetWindowText(hwnd) or "").strip()
    except Exception:
        return ""


def force_foreground(hwnd: int, *, log: Callable[[str], None] | None = None) -> None:
    """Restore and foreground a window (AttachThreadInput for RDP / focus stealing)."""
    import ctypes
    import win32con
    import win32gui

    emit = log or _log_default
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        else:
            win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
    except Exception:
        pass

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    fg = user32.GetForegroundWindow()
    if fg == hwnd:
        return

    fg_thread = user32.GetWindowThreadProcessId(fg, None)
    target_thread = user32.GetWindowThreadProcessId(hwnd, None)
    cur_thread = kernel32.GetCurrentThreadId()

    try:
        user32.AttachThreadInput(cur_thread, fg_thread, True)
        user32.AttachThreadInput(cur_thread, target_thread, True)
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
    except Exception as exc:
        emit(f"WARN: SetForegroundWindow: {exc}")
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass
    finally:
        try:
            user32.AttachThreadInput(cur_thread, target_thread, False)
            user32.AttachThreadInput(cur_thread, fg_thread, False)
        except Exception:
            pass


def focus_main_window(win, *, log: Callable[[str], None] | None = None) -> None:
    emit = log or _log_default
    hwnd: int | None = None
    try:
        hwnd = int(win.handle)
    except Exception:
        pass
    try:
        if win.is_minimized():
            win.restore()
    except Exception:
        pass
    if hwnd:
        force_foreground(hwnd, log=emit)
    try:
        win.set_focus()
    except Exception:
        pass


def _click_hwnd(hwnd: int, *, log: Callable[[str], None] | None = None) -> bool:
    """BM_CLICK then mouse at control center."""
    import win32api
    import win32con
    import win32gui

    emit = log or _log_default
    try:
        win32gui.PostMessage(hwnd, win32con.BM_CLICK, 0, 0)
        time.sleep(0.08)
        return True
    except Exception as exc:
        emit(f"WARN: BM_CLICK on hwnd {hwnd}: {exc}")

    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        if right - left < 2 or bottom - top < 2:
            return False
        x = (left + right) // 2
        y = (top + bottom) // 2
        win32api.SetCursorPos((x, y))
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        return True
    except Exception as exc:
        emit(f"WARN: mouse click on hwnd {hwnd}: {exc}")
        return False


def _control_type(target) -> str:
    try:
        return (target.element_info.control_type or "").strip()
    except Exception:
        return ""


def _home_tab_click_y(win) -> int | None:
    """Y coordinate that reliably hits a ribbon tab (not the window drag border)."""
    for el in _descendant_controls(win, "Home", ("TabItem", "Button")):
        try:
            if not el.is_visible():
                continue
            r = el.rectangle()
            # Use lower half of Home tab — avoids resize/move cursor on top edge.
            return r.top + max(12, (r.bottom - r.top) * 2 // 3)
        except Exception:
            continue
    return None


def _click_point_in_rect(
    rect,
    *,
    control_type: str = "",
    win=None,
    log: Callable[[str], None] | None = None,
) -> bool:
    """
    Click inside a control rect. TabItem rects from UIA often include the window
    top border; center clicks land on the move/resize zone (four-arrow cursor).
    """
    from pywinauto import mouse

    emit = log or _log_default
    try:
        left, top, right, bottom = rect.left, rect.top, rect.right, rect.bottom
        w = right - left
        h = bottom - top
        if w < 2 or h < 2:
            return False

        x = left + w // 2
        if control_type == "TabItem":
            home_y = _home_tab_click_y(win) if win is not None else None
            if home_y is not None:
                y = home_y
            else:
                # Skip top third — that band is window chrome / drag handles.
                inset_top = max(16, h // 3)
                y = top + inset_top + (h - inset_top) // 2
        else:
            y = top + max(6, h // 2)

        emit(f"Mouse click at ({x}, {y}) [{control_type or 'control'}]")
        mouse.click(button="left", coords=(x, y))
        return True
    except Exception as exc:
        emit(f"WARN: mouse click in rect: {exc}")
        return False


def _try_uia_click(
    target,
    *,
    win=None,
    log: Callable[[str], None] | None = None,
) -> bool:
    emit = log or _log_default
    ctype = _control_type(target)

    if ctype == "TabItem":
        # Tabs use SelectionItem — not Invoke. click_input() hits the drag border.
        for method_name in ("select", "set_focus"):
            try:
                method = getattr(target, method_name)
                method()
                time.sleep(0.15)
                try:
                    from pywinauto.keyboard import send_keys

                    send_keys("{SPACE}")
                except Exception:
                    pass
                return True
            except Exception as exc:
                emit(f"WARN: TabItem {method_name}() failed: {type(exc).__name__}")
        try:
            rect = target.rectangle()
            if _click_point_in_rect(rect, control_type=ctype, win=win, log=emit):
                return True
        except Exception:
            pass
        return False

    for method_name in ("invoke", "click"):
        try:
            method = getattr(target, method_name)
            method()
            return True
        except Exception as exc:
            emit(f"WARN: {method_name}() failed: {type(exc).__name__}")

    try:
        rect = target.rectangle()
        if _click_point_in_rect(rect, control_type=ctype, win=win, log=emit):
            return True
    except Exception:
        pass

    # click_input last — can land on window chrome for ribbon controls.
    try:
        target.click_input()
        return True
    except Exception as exc:
        emit(f"WARN: click_input() failed: {type(exc).__name__}")

    try:
        ch = int(target.handle)
        if ch:
            return _click_hwnd(ch, log=emit)
    except Exception:
        pass
    return False


def _enum_hwnds_with_text(root_hwnd: int, needle: str, *, max_depth: int = 24) -> list[int]:
    import win32gui

    needle_low = needle.lower().replace("&", "")
    found: list[int] = []

    def _walk(parent: int, depth: int) -> None:
        if depth > max_depth:
            return

        def _cb(child, _):
            try:
                if not win32gui.IsWindowVisible(child):
                    return True
                text = (win32gui.GetWindowText(child) or "").strip()
                if text:
                    norm = text.lower().replace("&", "")
                    if norm == needle_low or needle_low in norm:
                        found.append(child)
            except Exception:
                pass
            _walk(child, depth + 1)
            return True

        try:
            win32gui.EnumChildWindows(parent, _cb, None)
        except Exception:
            pass

    _walk(root_hwnd, 0)
    return found


def _matching_controls(
    win,
    *,
    title: str,
    control_types: tuple[str, ...],
    max_index: int = 8,
):
    """Exact child_window match, then title_re, then full descendant scan."""
    seen: set[int] = set()
    exist_ms = 30
    for ctrl in control_types:
        for i in range(max_index):
            try:
                target = win.child_window(
                    title=title, control_type=ctrl, found_index=i
                )
                if not target.exists(timeout=exist_ms / 1000.0):
                    break
                key = id(target)
                if key not in seen:
                    seen.add(key)
                    yield target
            except Exception:
                break
        try:
            target = win.child_window(
                title_re=f".*{re.escape(title)}.*",
                control_type=ctrl,
            )
            if target.exists(timeout=exist_ms / 1000.0):
                key = id(target)
                if key not in seen:
                    seen.add(key)
                    yield target
        except Exception:
            pass

    for el in _descendant_controls(win, title, control_types):
        key = id(el)
        if key in seen:
            continue
        seen.add(key)
        yield el


def _descendant_controls(
    win,
    title: str,
    control_types: tuple[str, ...],
) -> list:
    out: list = []
    try:
        elements = win.descendants()
    except Exception:
        return out
    for el in elements:
        try:
            ctype = ""
            try:
                ctype = el.element_info.control_type or ""
            except Exception:
                pass
            if control_types and ctype and ctype not in control_types:
                continue
            name = _control_name(el)
            if not name or not _name_matches(name, title):
                continue
            out.append(el)
        except Exception:
            continue
    return out


def _dump_ribbon_names(win, *, log: Callable[[str], None]) -> None:
    """Log visible ribbon-like control names (helps diagnose RDP UIA gaps)."""
    names: list[str] = []
    try:
        for el in win.descendants():
            try:
                ctype = el.element_info.control_type or ""
                if ctype not in (
                    "Button",
                    "SplitButton",
                    "MenuItem",
                    "TabItem",
                    "Hyperlink",
                ):
                    continue
                if not el.is_visible():
                    continue
                name = _control_name(el)
                if name and name not in names:
                    names.append(name)
            except Exception:
                continue
    except Exception as exc:
        log(f"WARN: ribbon dump failed: {exc}")
        return
    if names:
        preview = ", ".join(names[:40])
        if len(names) > 40:
            preview += f", … (+{len(names) - 40} more)"
        log(f"Visible ribbon controls: {preview}")
    else:
        log("WARN: no ribbon control names visible to UIA (RDP/UIA issue).")


def ribbon_action_available(
    win,
    title: str,
    control_types: tuple[str, ...],
) -> bool:
    for target in _matching_controls(win, title=title, control_types=control_types):
        try:
            if target.is_visible() and target.is_enabled():
                return True
        except Exception:
            continue
    return False


def _click_import_export_by_position(
    win,
    *,
    log: Callable[[str], None],
) -> bool:
    """Click Import-Export tab by offset from Home tab (when UIA names fail)."""
    from pywinauto import mouse

    home_rect = None
    for el in _descendant_controls(win, "Home", ("TabItem", "Button")):
        try:
            if el.is_visible():
                home_rect = el.rectangle()
                break
        except Exception:
            continue

    if home_rect is not None:
        offset = _step_wait_s("WORLDSHIP_IMPORT_EXPORT_TAB_OFFSET_X", 300.0)
        x = int(home_rect.right + offset)
        y = home_rect.top + max(12, (home_rect.bottom - home_rect.top) * 2 // 3)
    else:
        try:
            wr = win.rectangle()
        except Exception:
            return False
        x = int(wr.left + _step_wait_s("WORLDSHIP_IMPORT_EXPORT_TAB_X", 520.0))
        y = int(wr.top + _step_wait_s("WORLDSHIP_IMPORT_EXPORT_TAB_Y", 95.0))

    log(f"Coordinate click for Import-Export at ({x}, {y})…")
    try:
        focus_main_window(win, log=log)
        mouse.click(button="left", coords=(x, y))
        time.sleep(0.4)
        return True
    except Exception as exc:
        log(f"WARN: coordinate Import-Export click: {exc}")
        return False


def _import_export_tab_rect(win):
    for label in ("Import-Export", "Import Export"):
        for el in _descendant_controls(win, label, ("TabItem", "Button")):
            try:
                if el.is_visible():
                    return el.rectangle()
            except Exception:
                continue
    return None


def _left_ribbon_tab_rect(win):
    """Home/Ship tab on the left — Import-Export panel buttons align under this edge."""
    for label in ("Home", "Ship"):
        for el in _descendant_controls(win, label, ("TabItem", "Button")):
            try:
                if el.is_visible():
                    return el.rectangle()
            except Exception:
                continue
    leftmost = None
    left_x = None
    try:
        for el in win.descendants():
            try:
                if _control_type(el) != "TabItem" or not el.is_visible():
                    continue
                r = el.rectangle()
            except Exception:
                continue
            if left_x is None or r.left < left_x:
                left_x = r.left
                leftmost = r
    except Exception:
        pass
    return leftmost


def _ribbon_content_anchor(win) -> tuple[int, int] | None:
    """
    Anchor for clicking buttons in the active tab's content row.
    X = left edge of the ribbon panel (Home tab), not the Import-Export tab on the right.
    Y = bottom of the tab strip.
    """
    left_tab = _left_ribbon_tab_rect(win)
    ie_tab = _import_export_tab_rect(win)

    tab_strip_bottom = None
    for rect in (ie_tab, left_tab):
        if rect is not None:
            tab_strip_bottom = rect.bottom
            break

    if left_tab is not None:
        anchor_x = left_tab.left
    else:
        try:
            win_rect = win.rectangle()
        except Exception:
            return None
        anchor_x = win_rect.left + 24
        if tab_strip_bottom is None:
            tab_strip_bottom = win_rect.top + 95

    if tab_strip_bottom is None:
        return None
    return (anchor_x, tab_strip_bottom)


def _step_wait_s(env_key: str, default: float) -> float:
    raw = (os.environ.get(env_key) or str(default)).strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


def _batch_import_attempts() -> int:
    raw = (os.environ.get("WORLDSHIP_BATCH_IMPORT_ATTEMPTS") or "6").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 6


def _visible_top_level_windows_with_text(needle: str) -> list[str]:
    import win32gui

    needle_low = needle.lower()
    titles: list[str] = []

    def _cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            title = (win32gui.GetWindowText(hwnd) or "").strip()
            if needle_low in title.lower():
                titles.append(title)
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:
        pass
    return titles


def batch_import_wizard_open(win, app=None) -> bool:
    """True when the Batch Import wizard (auto-process checkbox or dialog) is visible."""
    for el in _descendant_controls(
        win,
        "Process shipments automatically after import",
        ("CheckBox", "RadioButton"),
    ):
        try:
            if el.is_visible():
                return True
        except Exception:
            continue

    try:
        for el in win.descendants():
            try:
                if not el.is_visible():
                    continue
            except Exception:
                continue
            name = _control_name(el).lower()
            if _AUTO_PROCESS_LABEL_SNIPPET in name:
                return True
            ctype = _control_type(el)
            if ctype == "CheckBox" and "automatic" in name and "import" in name:
                return True
    except Exception:
        pass

    if app is not None:
        try:
            for title_re in (".*Batch Import.*", ".*Import.*Export.*"):
                cand = app.window(title_re=title_re)
                if cand.exists(timeout=0.05):
                    for box in _descendant_controls(
                        cand,
                        "Process shipments automatically after import",
                        ("CheckBox",),
                    ):
                        try:
                            if box.is_visible():
                                return True
                        except Exception:
                            continue
        except Exception:
            pass

    return bool(_visible_top_level_windows_with_text(_BATCH_IMPORT_TITLE_SNIPPET))


def _find_ribbon_controls_by_substrings(
    win,
    *parts: str,
    control_types: tuple[str, ...] = (
        "Button",
        "SplitButton",
        "MenuItem",
        "Hyperlink",
        "Text",
    ),
) -> list:
    parts_low = [p.lower() for p in parts if p]
    hits: list = []
    try:
        elements = win.descendants()
    except Exception:
        return hits
    for el in elements:
        try:
            ctype = _control_type(el)
            if control_types and ctype and ctype not in control_types:
                continue
            if not el.is_visible():
                continue
            if not el.is_enabled():
                continue
        except Exception:
            continue
        name = _control_name(el).lower()
        if not name:
            continue
        if parts_low and not all(p in name for p in parts_low):
            continue
        hits.append(el)
    return hits


def _click_first_matching_control(
    controls: list,
    *,
    win,
    log: Callable[[str], None],
    tag: str,
) -> bool:
    for el in controls:
        name = _control_name(el)
        log(f"Clicking {tag}: {name!r} ({_control_type(el)})…")
        if _try_uia_click(el, win=win, log=log):
            return True
    return False


def _click_batch_import_exact(win, *, log: Callable[[str], None]) -> bool:
    custom = (os.environ.get("WORLDSHIP_BATCH_IMPORT_LABEL") or "").strip()
    labels = [custom] if custom else []
    labels.extend(["Batch Import", "Batch  Import", "BatchImport"])
    for label in labels:
        for ctrl_type in ("Button", "SplitButton", "MenuItem", "Hyperlink", "Text"):
            for target in _matching_controls(
                win, title=label, control_types=(ctrl_type,)
            ):
                try:
                    if not target.is_visible() or not target.is_enabled():
                        continue
                except Exception:
                    continue
                if _try_uia_click(target, win=win, log=log):
                    return True
    try:
        root_hwnd = int(win.handle)
    except Exception:
        root_hwnd = 0
    if root_hwnd:
        for ch in _enum_hwnds_with_text(root_hwnd, "Batch Import"):
            if _click_hwnd(ch, log=log):
                return True
    return False


def _click_batch_import_fuzzy(win, *, log: Callable[[str], None]) -> bool:
    hits = _find_ribbon_controls_by_substrings(win, "batch", "import")
    if _click_first_matching_control(hits, win=win, log=log, tag="fuzzy Batch Import"):
        return True
    hits = _find_ribbon_controls_by_substrings(win, "batch")
    return _click_first_matching_control(hits, win=win, log=log, tag="fuzzy Batch*")


def _click_batch_import_win32(win, *, log: Callable[[str], None]) -> bool:
    try:
        root_hwnd = int(win.handle)
    except Exception:
        return False
    needles = ("Batch Import", "Batch  Import", "BatchImport")
    for needle in needles:
        for ch in _enum_hwnds_with_text(root_hwnd, needle):
            log(f"Win32 click hwnd {ch} for {needle!r}…")
            if _click_hwnd(ch, log=log):
                return True
    return False


def _click_batch_import_coordinate_grid(win, *, log: Callable[[str], None]) -> bool:
    """Try several offsets below the tab strip, anchored from the left ribbon edge."""
    from pywinauto import mouse

    anchor = _ribbon_content_anchor(win)
    if anchor is None:
        return False
    anchor_x, anchor_y = anchor

    # Batch Import is the 2nd large button (after Keyed Import) on the left.
    base_x = _step_wait_s("WORLDSHIP_BATCH_IMPORT_OFFSET_X", 130.0)
    base_y = _step_wait_s("WORLDSHIP_BATCH_IMPORT_OFFSET_Y", 42.0)
    x_deltas = (0.0, -35.0, -70.0, 35.0, 70.0, 105.0)
    y_deltas = (0.0, 10.0, -8.0, 18.0)

    focus_main_window(win, log=log)
    log(
        f"Batch Import grid anchor (left ribbon edge): ({anchor_x}, {anchor_y}) "
        f"+ offset ({int(base_x)}, {int(base_y)})"
    )

    for dy in y_deltas:
        for dx in x_deltas:
            x = int(anchor_x + base_x + dx)
            y = int(anchor_y + base_y + dy)
            log(f"Coordinate grid click for Batch Import at ({x}, {y})…")
            try:
                mouse.click(button="left", coords=(x, y))
                time.sleep(0.2)
                if batch_import_wizard_open(win):
                    return True
            except Exception as exc:
                log(f"WARN: coordinate grid click ({x}, {y}): {exc}")
    return False


def _click_batch_import_by_position(
    win,
    *,
    log: Callable[[str], None],
) -> bool:
    """
    When UIA cannot name ribbon buttons (common over RDP), click Batch Import
    below the tab strip, offset from the left ribbon edge (Home tab).
    """
    from pywinauto import mouse

    anchor = _ribbon_content_anchor(win)
    if anchor is None:
        return False
    anchor_x, anchor_y = anchor

    offset_x = _step_wait_s("WORLDSHIP_BATCH_IMPORT_OFFSET_X", 130.0)
    offset_y = _step_wait_s("WORLDSHIP_BATCH_IMPORT_OFFSET_Y", 42.0)
    x = int(anchor_x + offset_x)
    y = int(anchor_y + offset_y)
    log(
        f"Coordinate click for Batch Import at ({x}, {y}) "
        f"(anchor left edge {anchor_x}, tab strip bottom {anchor_y})…"
    )
    try:
        focus_main_window(win, log=log)
        mouse.click(button="left", coords=(x, y))
        time.sleep(0.35)
        return True
    except Exception as exc:
        log(f"WARN: coordinate Batch Import click: {exc}")
        return False


def click_ribbon(
    win,
    *,
    title: str,
    control_types: tuple[str, ...] = ("Button", "TabItem"),
    timeout_s: float = 8.0,
    log: Callable[[str], None] | None = None,
) -> None:
    """
    Click a WorldShip ribbon tab or button using several strategies (UIA invoke,
    message click, Win32 child search, mouse at rect). Works better over RDP than
    click_input() alone.
    """
    emit = log or _log_default
    poll_s = 0.08
    uia_cap_s = _step_wait_s("WORLDSHIP_RIBBON_UIA_TIMEOUT_S", 2.0)
    deadline = time.monotonic() + timeout_s
    uia_deadline = time.monotonic() + min(timeout_s, uia_cap_s)
    saw_any = False
    coord_tried = False
    focus_done = False

    try:
        root_hwnd = int(win.handle)
    except Exception:
        root_hwnd = 0

    def _try_uia_click_once() -> bool:
        nonlocal saw_any
        for ctrl_type in control_types:
            for target in _matching_controls(
                win, title=title, control_types=(ctrl_type,)
            ):
                saw_any = True
                try:
                    if not target.is_visible():
                        continue
                    if not target.is_enabled():
                        continue
                except Exception:
                    continue
                emit(f"Clicking {title!r} ({ctrl_type}) via UIA…")
                if _try_uia_click(target, win=win, log=emit):
                    return True
        if root_hwnd:
            for ch in _enum_hwnds_with_text(root_hwnd, title):
                emit(f"Clicking {title!r} via Win32 hwnd {ch}…")
                if _click_hwnd(ch, log=emit):
                    return True
        return False

    def _try_coordinate_fallback() -> bool:
        if _name_matches(title, "import-export") or _name_matches(title, "import export"):
            emit(f"Coordinate click for Import-Export…")
            return _click_import_export_by_position(win, log=emit)
        if _name_matches(title, "batch import"):
            emit(f"Coordinate click for Batch Import…")
            return _click_batch_import_by_position(win, log=emit)
        return False

    while time.monotonic() < deadline:
        if not focus_done:
            focus_main_window(win, log=emit)
            focus_done = True

        if _try_uia_click_once():
            time.sleep(0.25)
            return

        now = time.monotonic()
        if not coord_tried and now >= uia_deadline:
            coord_tried = True
            if _try_coordinate_fallback():
                time.sleep(0.25)
                return

        time.sleep(poll_s)

    if not coord_tried and _try_coordinate_fallback():
        time.sleep(0.25)
        return

    _dump_ribbon_names(win, log=emit)
    hint = "no matching controls found" if not saw_any else "controls not clickable"
    raise RuntimeError(f"Could not click {title!r}: {hint}")


def _running_over_rdp(win) -> bool:
    if (os.environ.get("WORLDSHIP_REMOTE_WORKSTATION") or "").strip() == "1":
        return True
    try:
        title = (win.window_text() or "").lower()
    except Exception:
        return False
    return "remote workstation" in title


def _click_import_export_tab_rect(
    win,
    *,
    log: Callable[[str], None],
) -> bool:
    """Click the Import-Export tab itself (not an offset from Home)."""
    for label in ("Import-Export", "Import Export"):
        for el in _descendant_controls(win, label, ("TabItem", "Button")):
            try:
                if not el.is_visible():
                    continue
                rect = el.rectangle()
                log(f"Mouse click on Import-Export tab ({label!r})…")
                if _click_point_in_rect(
                    rect, control_type="TabItem", win=win, log=log
                ):
                    time.sleep(0.35)
                    return True
            except Exception:
                continue
    return False


def _activate_import_export_tab(win, *, log: Callable[[str], None]) -> bool:
    """Try several ways to select Import-Export; return True if Batch Import is visible to UIA."""
    tab_timeout = _step_wait_s("WORLDSHIP_IMPORT_EXPORT_TAB_TIMEOUT_S", 5.0)
    after_tab_s = _step_wait_s("WORLDSHIP_AFTER_IMPORT_EXPORT_TAB_S", 2.0)

    def _batch_visible() -> bool:
        return ribbon_action_available(
            win, "Batch Import", ("Button", "MenuItem", "SplitButton", "Hyperlink")
        )

    attempts = (
        ("Import-Export tab rect", lambda: _click_import_export_tab_rect(win, log=log)),
        (
            "UIA Import-Export",
            lambda: click_ribbon(
                win,
                title="Import-Export",
                control_types=("TabItem", "Button"),
                timeout_s=tab_timeout,
                log=log,
            )
            or True,
        ),
        ("coordinate from Home", lambda: _click_import_export_by_position(win, log=log)),
        ("Import-Export tab rect (retry)", lambda: _click_import_export_tab_rect(win, log=log)),
    )

    for name, fn in attempts:
        if _batch_visible():
            log("Verified: Batch Import visible on ribbon.")
            return True
        log(f"Import-Export activate via {name}…")
        try:
            fn()
        except Exception as exc:
            log(f"WARN: {name} failed: {exc}")
        if after_tab_s > 0:
            time.sleep(after_tab_s)
        if _batch_visible():
            log(f"Verified: Batch Import visible after {name}.")
            return True
    return _batch_visible()


def ensure_import_export_tab(
    win,
    *,
    log: Callable[[str], None] | None = None,
) -> None:
    """Open Import-Export ribbon; on RDP, UIA may not see panel buttons — still continue."""
    emit = log or _log_default
    if ribbon_action_available(
        win, "Batch Import", ("Button", "MenuItem", "SplitButton")
    ):
        emit("Import-Export ribbon already active — skipping tab click.")
        return

    emit("Opening Import-Export tab…")
    focus_main_window(win, log=emit)
    if _activate_import_export_tab(win, log=emit):
        return

    _dump_ribbon_names(win, log=emit)
    if _running_over_rdp(win):
        emit(
            "WARN: Running on WorldShip Remote Workstation — UIA often cannot see "
            "Batch Import on the ribbon. Tab activation was attempted; continuing with "
            "coordinate Batch Import clicks."
        )
        return

    emit(
        "WARN: Batch Import is not visible to UIA after Import-Export tab clicks. "
        "Continuing with coordinate / Win32 Batch Import strategies anyway."
    )


def click_batch_import(
    win,
    *,
    log: Callable[[str], None] | None = None,
    app=None,
) -> None:
    """
    Open Batch Import using several strategies; each attempt is verified by wizard UI.
    """
    emit = log or _log_default
    verify_s = _step_wait_s("WORLDSHIP_BATCH_IMPORT_VERIFY_S", 3.0)
    attempts = _batch_import_attempts()

    focus_main_window(win, log=emit)
    emit(f"Ribbon click engine {_RIBBON_VERSION} — opening Batch Import")

    if batch_import_wizard_open(win, app=app):
        emit("Batch Import wizard is already open.")
        return

    if not ribbon_action_available(
        win, "Batch Import", ("Button", "MenuItem", "SplitButton", "Hyperlink")
    ):
        emit("Batch Import not visible on ribbon — ensuring Import-Export tab is active…")
        ensure_import_export_tab(win, log=emit)

    coordinate_strategies: list[tuple[str, Callable[[], bool]]] = [
        ("coordinate grid", lambda: _click_batch_import_coordinate_grid(win, log=emit)),
        (
            "coordinate default",
            lambda: _click_batch_import_by_position(win, log=emit),
        ),
    ]
    uia_strategies: list[tuple[str, Callable[[], bool]]] = [
        ("UIA exact", lambda: _click_batch_import_exact(win, log=emit)),
        ("UIA fuzzy", lambda: _click_batch_import_fuzzy(win, log=emit)),
        ("Win32 child", lambda: _click_batch_import_win32(win, log=emit)),
    ]
    if _running_over_rdp(win):
        strategies = coordinate_strategies + uia_strategies
    else:
        strategies = uia_strategies + coordinate_strategies

    last_strategy = ""
    for attempt in range(1, attempts + 1):
        for strategy_name, fn in strategies:
            if batch_import_wizard_open(win, app=app):
                emit("Batch Import wizard is open.")
                return
            emit(f"Batch Import try {attempt}/{attempts}: {strategy_name}…")
            last_strategy = strategy_name
            try:
                clicked = fn()
            except Exception as exc:
                emit(f"WARN: {strategy_name} raised {exc}")
                clicked = False
            if not clicked and strategy_name.startswith("coordinate"):
                # grid / default may still have opened wizard via mouse
                pass
            deadline = time.monotonic() + verify_s
            while time.monotonic() < deadline:
                if batch_import_wizard_open(win, app=app):
                    emit(f"Batch Import wizard open (strategy: {strategy_name}).")
                    return
                time.sleep(0.12)

    titles = _visible_top_level_windows_with_text("import")
    if titles:
        emit(f"Visible import-related windows: {titles[:6]}")
    _dump_ribbon_names(win, log=emit)
    raise RuntimeError(
        "Could not open the Batch Import wizard after "
        f"{attempts} round(s) of ribbon strategies (last: {last_strategy!r}). "
        "Set WORLDSHIP_BATCH_IMPORT_OFFSET_X/Y in .env if coordinate clicks miss the button."
    )
