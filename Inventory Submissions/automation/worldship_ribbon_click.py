"""RDP-friendly ribbon/tab clicks for UPS WorldShip (multi-strategy)."""

from __future__ import annotations

import os
import re
import time
from typing import Callable

_RIBBON_VERSION = "ribbon-click-v16"
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
    """BM_CLICK then physical mouse at control center."""
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

    return _click_hwnd_physical(hwnd, log=emit)


def _click_hwnd_physical(hwnd: int, *, log: Callable[[str], None] | None = None) -> bool:
    """Move mouse to hwnd center and click (works over RDP)."""
    import win32api
    import win32con
    import win32gui

    emit = log or _log_default
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        if right - left < 2 or bottom - top < 2:
            return False
        x = (left + right) // 2
        y = (top + bottom) // 2
        win32api.SetCursorPos((x, y))
        time.sleep(0.05)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.04)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        return True
    except Exception as exc:
        emit(f"WARN: physical click on hwnd {hwnd}: {exc}")
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
        return _physical_screen_click(
            x, y, win=win, log=emit, label=control_type or "control"
        )
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


def _uia_deep_scan_enabled(win=None) -> bool:
    """Full win.descendants() walks — very slow over RDP; off in fast import mode."""
    flag = (os.environ.get("WORLDSHIP_UIA_DEEP_SCAN") or "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return True
    if flag in ("0", "false", "no", "off"):
        return False
    return not _fast_ribbon_clicks_enabled(win)


def _matching_controls(
    win,
    *,
    title: str,
    control_types: tuple[str, ...],
    max_index: int = 8,
):
    """Exact child_window match, then title_re; optional full descendant scan."""
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

    if not _uia_deep_scan_enabled(win):
        return

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

    abs_coords = _env_screen_coords(
        "WORLDSHIP_IMPORT_EXPORT_ABS_X", "WORLDSHIP_IMPORT_EXPORT_ABS_Y"
    )
    if abs_coords is not None:
        x, y = abs_coords
        log(f"Coordinate click for Import-Export at ({x}, {y}) [calibrated ABS]…")
        try:
            focus_main_window(win, log=log)
            mouse.click(button="left", coords=(x, y))
            time.sleep(0.4)
            return True
        except Exception as exc:
            log(f"WARN: coordinate Import-Export click: {exc}")
            return False

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
    if _fast_ribbon_clicks_enabled(win):
        # Calibrated Remote Workstation — avoid win.descendants() (minutes over RDP).
        return 1181, 190

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


def _env_screen_coords(x_key: str, y_key: str) -> tuple[int, int] | None:
    """Optional absolute screen coords from .env (calibrated Remote Workstation)."""
    x_raw = (os.environ.get(x_key) or "").strip()
    y_raw = (os.environ.get(y_key) or "").strip()
    if not x_raw or not y_raw:
        return None
    try:
        return int(float(x_raw)), int(float(y_raw))
    except ValueError:
        return None


def _calibrated_import_export_coords(win) -> tuple[int, int] | None:
    coords = _env_screen_coords(
        "WORLDSHIP_IMPORT_EXPORT_ABS_X", "WORLDSHIP_IMPORT_EXPORT_ABS_Y"
    )
    if coords is not None:
        return coords
    if _fast_ribbon_clicks_enabled(win):
        return 1464, 182
    return None


def _calibrated_batch_import_coords(win) -> tuple[int, int] | None:
    coords = _env_screen_coords(
        "WORLDSHIP_BATCH_IMPORT_ABS_X", "WORLDSHIP_BATCH_IMPORT_ABS_Y"
    )
    if coords is not None:
        return coords
    if _fast_ribbon_clicks_enabled(win):
        return 1276, 232
    return None


def _physical_screen_click(
    x: int,
    y: int,
    *,
    win,
    log: Callable[[str], None],
    label: str,
) -> bool:
    """
    Move the real mouse and click (works over RDP; pywinauto.mouse.click alone often does not).
    """
    import win32api
    import win32con

    ix, iy = int(x), int(y)
    log(f"{label} at ({ix}, {iy}) [physical mouse]…")
    focus_main_window(win, log=log)
    fast = _fast_ribbon_clicks_enabled(win)
    time.sleep(0.06 if fast else 0.15)

    try:
        win32api.SetCursorPos((ix, iy))
        time.sleep(0.04 if fast else 0.08)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.03 if fast else 0.05)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        time.sleep(_click_settle_s(win))
        return True
    except Exception as exc:
        log(f"WARN: physical mouse click failed: {exc}")

    try:
        from pywinauto import mouse

        mouse.click(button="left", coords=(ix, iy))
        time.sleep(_click_settle_s(win))
        log(f"{label} at ({ix}, {iy}) [pywinauto fallback]…")
        return True
    except Exception as exc:
        log(f"WARN: pywinauto click failed: {exc}")
        return False


def _click_screen_coords(
    x: int,
    y: int,
    *,
    win,
    log: Callable[[str], None],
    label: str,
) -> bool:
    return _physical_screen_click(x, y, win=win, log=log, label=label)


def _click_import_export_tab_fast(win, *, log: Callable[[str], None]) -> bool:
    """Direct screen click — no UIA tree walk (RDP-safe)."""
    coords = _calibrated_import_export_coords(win)
    if coords is not None:
        x, y = coords
        return _click_screen_coords(
            x, y, win=win, log=log, label="Import-Export click"
        )
    return _click_import_export_tab_rect(win, log=log)


def _calibrated_batch_export_coords(win) -> tuple[int, int] | None:
    coords = _env_screen_coords(
        "WORLDSHIP_BATCH_EXPORT_ABS_X", "WORLDSHIP_BATCH_EXPORT_ABS_Y"
    )
    if coords is not None:
        return coords
    if _fast_ribbon_clicks_enabled(win):
        anchor = _ribbon_content_anchor(win)
        if anchor is not None:
            ox = _step_wait_s("WORLDSHIP_BATCH_EXPORT_OFFSET_X", 285.0)
            oy = _step_wait_s("WORLDSHIP_BATCH_EXPORT_OFFSET_Y", 42.0)
            return int(anchor[0] + ox), int(anchor[1] + oy)
        return 1466, 232
    return None


def _resolve_import_export_coords(win) -> tuple[int, int]:
    """Screen point for Import-Export tab (env → fast/RDP default → fallback)."""
    coords = _env_screen_coords(
        "WORLDSHIP_IMPORT_EXPORT_ABS_X", "WORLDSHIP_IMPORT_EXPORT_ABS_Y"
    )
    if coords is not None:
        return coords
    if _fast_ribbon_clicks_enabled(win):
        return 1464, 182
    return 1464, 182


def _resolve_batch_export_coords(win) -> tuple[int, int]:
    """Screen point for Batch Export button (env → offset → fallback)."""
    coords = _env_screen_coords(
        "WORLDSHIP_BATCH_EXPORT_ABS_X", "WORLDSHIP_BATCH_EXPORT_ABS_Y"
    )
    if coords is not None:
        return coords
    if _fast_ribbon_clicks_enabled(win):
        anchor = _ribbon_content_anchor(win)
        if anchor is not None:
            ox = _step_wait_s("WORLDSHIP_BATCH_EXPORT_OFFSET_X", 285.0)
            oy = _step_wait_s("WORLDSHIP_BATCH_EXPORT_OFFSET_Y", 42.0)
            return int(anchor[0] + ox), int(anchor[1] + oy)
        return 1466, 232
    return 1466, 232


def ensure_import_export_tab_for_export(
    win,
    *,
    log: Callable[[str], None] | None = None,
) -> None:
    """Always activate Import-Export — tab rect click first, then calibrated coordinates."""
    emit = log or _log_default
    focus_main_window(win, log=emit)
    after_s = _import_pacing_s("WORLDSHIP_AFTER_IMPORT_EXPORT_TAB_S", 0.75, 0.15, win)

    emit("Export: clicking Import-Export tab…")
    if _click_import_export_tab_rect(win, log=emit):
        if after_s > 0:
            time.sleep(after_s)
        return

    x, y = _resolve_import_export_coords(win)
    emit(f"Export: Import-Export coordinate fallback at ({x}, {y})…")
    if not _physical_screen_click(
        x, y, win=win, log=emit, label="Import-Export tab"
    ):
        raise RuntimeError(f"Import-Export tab click failed at ({x}, {y})")
    if after_s > 0:
        time.sleep(after_s)


def click_batch_export_for_export(
    win,
    *,
    log: Callable[[str], None] | None = None,
) -> None:
    """Click Batch Export — try several X offsets (4th ribbon button; calibrate via .env)."""
    emit = log or _log_default
    focus_main_window(win, log=emit)

    anchor = _ribbon_content_anchor(win)
    base_y = (
        int(anchor[1] + _step_wait_s("WORLDSHIP_BATCH_EXPORT_OFFSET_Y", 42.0))
        if anchor
        else 232
    )
    base_x = _step_wait_s("WORLDSHIP_BATCH_EXPORT_OFFSET_X", 285.0)
    abs_coords = _env_screen_coords(
        "WORLDSHIP_BATCH_EXPORT_ABS_X", "WORLDSHIP_BATCH_EXPORT_ABS_Y"
    )
    if abs_coords is not None:
        candidates = [abs_coords]
    elif anchor is not None:
        ax = int(anchor[0])
        candidates = [
            (int(ax + base_x + dx), base_y)
            for dx in (0.0, -30.0, 30.0, 60.0, -60.0, 90.0)
        ]
    else:
        candidates = [_resolve_batch_export_coords(win)]

    emit(f"Export: trying Batch Export click(s) at Y={base_y}…")
    for x, y in candidates:
        if not _physical_screen_click(x, y, win=win, log=emit, label="Batch Export"):
            continue
        time.sleep(0.35)
        if _visible_top_level_windows_with_text("batch export"):
            emit("Batch export dialog detected.")
            return
    raise RuntimeError(
        f"Batch Export click failed (tried {len(candidates)} point(s)). "
        "Set WORLDSHIP_BATCH_EXPORT_ABS_X/Y in .env."
    )


def click_batch_export(
    win,
    *,
    log: Callable[[str], None] | None = None,
) -> None:
    """Open Batch Export — export workflow uses coordinates; else UIA fallback."""
    if _calibrated_batch_export_coords(win) is not None or _fast_ribbon_clicks_enabled(win):
        click_batch_export_for_export(win, log=log)
        return
    emit = log or _log_default
    emit('Clicking "Batch Export" (UIA)…')
    focus_main_window(win, log=emit)
    click_ribbon(
        win,
        title="Batch Export",
        control_types=("Button", "SplitButton", "MenuItem"),
        timeout_s=8.0,
        log=emit,
    )


def _fast_ribbon_clicks_enabled(win=None) -> bool:
    """True when calibrated coordinate clicks are in use (shorter pauses, fewer retries)."""
    flag = (os.environ.get("WORLDSHIP_FAST_IMPORT") or "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return True
    if flag in ("0", "false", "no", "off"):
        return False
    if _env_screen_coords("WORLDSHIP_BATCH_IMPORT_ABS_X", "WORLDSHIP_BATCH_IMPORT_ABS_Y"):
        return True
    if _env_screen_coords("WORLDSHIP_IMPORT_EXPORT_ABS_X", "WORLDSHIP_IMPORT_EXPORT_ABS_Y"):
        return True
    if win is not None and _running_over_rdp(win):
        return True
    return False


def _import_pacing_s(
    env_key: str,
    slow_default: float,
    fast_default: float,
    win=None,
) -> float:
    default = fast_default if _fast_ribbon_clicks_enabled(win) else slow_default
    return _step_wait_s(env_key, default)


def _click_settle_s(win=None, *, slow: float = 0.35, fast: float = 0.08) -> float:
    return fast if _fast_ribbon_clicks_enabled(win) else slow


def _batch_import_attempts(win=None) -> int:
    raw = (os.environ.get("WORLDSHIP_BATCH_IMPORT_ATTEMPTS") or "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return 2 if _fast_ribbon_clicks_enabled(win) else 6


def _modal_dialog_titles(*needles: str) -> list[str]:
    """Visible #32770 dialog titles matching any needle (fast Win32 check)."""
    import win32gui

    needles_low = [n.lower() for n in needles if n]
    titles: list[str] = []

    def _cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            if win32gui.GetClassName(hwnd) != "#32770":
                return True
            title = (win32gui.GetWindowText(hwnd) or "").strip()
            if not title:
                return True
            low = title.lower()
            if any(n in low for n in needles_low):
                titles.append(title)
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:
        pass
    return titles


def _batch_import_wizard_dialog_open() -> bool:
    """True when a Batch Import wizard modal is open (not preview/summary)."""
    skip = ("preview", "summary", "progress", "automatic processing")
    for title in _modal_dialog_titles(
        "batch import", "import/export", "import export"
    ):
        low = title.lower()
        if any(s in low for s in skip):
            continue
        return True
    return False


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
    if _batch_import_wizard_dialog_open():
        return True

    if _visible_top_level_windows_with_text(_BATCH_IMPORT_TITLE_SNIPPET):
        return True

    if app is not None and not _fast_ribbon_clicks_enabled(win):
        try:
            for title_re in (r".*Batch Import.*", r".*Import.*Export.*"):
                cand = app.window(title_re=title_re)
                if cand.exists(timeout=0.35):
                    return True
        except Exception:
            pass
    elif app is not None:
        try:
            cand = app.window(title_re=r".*Batch Import.*")
            if cand.exists(timeout=0.05):
                return True
        except Exception:
            pass

    # Full UIA tree walks are very slow over RDP — skip when using coordinate clicks.
    if not _uia_deep_scan_enabled(win):
        return False

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

    return False


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
    if not _uia_deep_scan_enabled(win):
        return []
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
            if _click_hwnd_physical(ch, log=log):
                return True
    return False


def _click_batch_import_child_window(win, *, log: Callable[[str], None]) -> bool:
    for ctype in ("Button", "SplitButton", "MenuItem", "Hyperlink"):
        try:
            btn = win.child_window(title="Batch Import", control_type=ctype)
            if not btn.exists(timeout=0.25):
                continue
            log(f"Batch Import via child_window ({ctype})…")
            if _try_uia_click(btn, win=win, log=log):
                return True
        except Exception:
            continue
    return False


def _click_batch_import_coordinate_grid(
    win,
    *,
    log: Callable[[str], None],
    fast: bool = False,
) -> bool:
    """Try several offsets below the tab strip, anchored from the left ribbon edge."""
    anchor = _ribbon_content_anchor(win)
    if anchor is None:
        return False
    anchor_x, anchor_y = anchor

    base_x = _step_wait_s("WORLDSHIP_BATCH_IMPORT_OFFSET_X", 95.0)
    base_y = _step_wait_s("WORLDSHIP_BATCH_IMPORT_OFFSET_Y", 42.0)
    if fast:
        x_deltas = (0.0, -20.0, 20.0)
        y_deltas = (0.0, 10.0)
        settle_s = 0.12
    else:
        x_deltas = (0.0, -20.0, 20.0, -40.0, 40.0)
        y_deltas = (0.0, 10.0, -8.0, 18.0)
        settle_s = 0.25

    focus_main_window(win, log=log)
    log(
        f"Batch Import grid anchor (left ribbon edge): ({anchor_x}, {anchor_y}) "
        f"+ offset ({int(base_x)}, {int(base_y)})"
    )

    for dy in y_deltas:
        for dx in x_deltas:
            x = int(anchor_x + base_x + dx)
            y = int(anchor_y + base_y + dy)
            if not _physical_screen_click(
                x, y, win=win, log=log, label=f"Batch Import grid ({x}, {y})"
            ):
                continue
            time.sleep(settle_s)
            if batch_import_wizard_open(win):
                return True
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
    calibrated = _calibrated_batch_import_coords(win)
    if calibrated is not None:
        x, y = calibrated
        return _click_screen_coords(
            x, y, win=win, log=log, label="Batch Import click"
        )

    anchor = _ribbon_content_anchor(win)
    if anchor is None:
        return False
    anchor_x, anchor_y = anchor

    offset_x = _step_wait_s("WORLDSHIP_BATCH_IMPORT_OFFSET_X", 95.0)
    offset_y = _step_wait_s("WORLDSHIP_BATCH_IMPORT_OFFSET_Y", 42.0)
    x = int(anchor_x + offset_x)
    y = int(anchor_y + offset_y)
    return _click_screen_coords(
        x,
        y,
        win=win,
        log=log,
        label=f"Batch Import click (anchor {anchor_x}, {anchor_y})",
    )


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
                    time.sleep(_click_settle_s(win))
                    return True
            except Exception:
                continue
    return False


def _batch_import_on_ribbon(win) -> bool:
    """True when Batch Import is visible (Import-Export panel is active, not Home)."""
    try:
        root = int(win.handle)
    except Exception:
        return False
    for needle in ("Batch Import", "Batch  Import", "BatchImport"):
        if _enum_hwnds_with_text(root, needle):
            return True
    for ctype in ("Button", "SplitButton", "MenuItem", "Hyperlink"):
        try:
            btn = win.child_window(title="Batch Import", control_type=ctype)
            if btn.exists(timeout=0.2):
                try:
                    return btn.is_visible()
                except Exception:
                    return True
        except Exception:
            continue
    return False


def _click_import_export_tab_child_window(win, *, log: Callable[[str], None]) -> bool:
    for label in ("Import-Export", "Import Export"):
        for ctype in ("TabItem", "Button"):
            try:
                tab = win.child_window(title=label, control_type=ctype)
                if not tab.exists(timeout=0.25):
                    continue
                log(f"Import-Export tab via child_window ({label!r}, {ctype})…")
                if _try_uia_click(tab, win=win, log=log):
                    return True
            except Exception:
                continue
    return False


def _click_import_export_tab_win32(win, *, log: Callable[[str], None]) -> bool:
    try:
        root = int(win.handle)
    except Exception:
        return False
    for needle in ("Import-Export", "Import Export", "Import&-Export"):
        for ch in _enum_hwnds_with_text(root, needle):
            log(f"Import-Export Win32 hwnd {ch} ({needle!r})…")
            if _click_hwnd_physical(ch, log=log):
                return True
    return False


def _click_import_export_tab_coords(win, *, log: Callable[[str], None]) -> bool:
    x, y = _resolve_import_export_coords(win)
    return _physical_screen_click(
        x, y, win=win, log=log, label="Import-Export tab"
    )


def _activate_import_export_tab_fast(
    win,
    *,
    log: Callable[[str], None],
    after_tab_s: float,
) -> bool:
    """
    Select Import-Export without slow UIA tree walks; verify Batch Import appears.
    """
    focus_main_window(win, log=log)

    if _batch_import_on_ribbon(win):
        log("Import-Export panel already active (Batch Import on ribbon).")
        return True

    steps: list[tuple[str, Callable[[], bool]]] = [
        ("Import-Export child_window", lambda: _click_import_export_tab_child_window(win, log=log)),
        ("Import-Export Win32", lambda: _click_import_export_tab_win32(win, log=log)),
        ("Import-Export coordinates", lambda: _click_import_export_tab_coords(win, log=log)),
    ]

    for name, fn in steps:
        if _batch_import_on_ribbon(win):
            log(f"Import-Export active — {name} not needed.")
            return True
        log(f"Fast ribbon: {name}…")
        try:
            fn()
        except Exception as exc:
            log(f"WARN: {name} failed: {exc}")
        if after_tab_s > 0:
            time.sleep(after_tab_s)
        if _batch_import_on_ribbon(win):
            log(f"Import-Export active after {name}.")
            return True

    x, y = _resolve_import_export_coords(win)
    log(f"Fast ribbon: Import-Export coordinate retry (2 clicks) at ({x}, {y})…")
    _physical_screen_click(x, y, win=win, log=log, label="Import-Export tab")
    time.sleep(0.1)
    _physical_screen_click(x, y, win=win, log=log, label="Import-Export tab retry")
    if after_tab_s > 0:
        time.sleep(after_tab_s)

    if _batch_import_on_ribbon(win):
        log("Import-Export active after coordinate retry.")
        return True

    log(
        "WARN: Batch Import still not on ribbon — Home tab may still be active. "
        "Check WORLDSHIP_IMPORT_EXPORT_ABS_X/Y if coordinates drifted."
    )
    return False


def _activate_import_export_tab(win, *, log: Callable[[str], None]) -> bool:
    """Try several ways to select Import-Export; return True if Batch Import is on ribbon."""
    fast = _fast_ribbon_clicks_enabled(win)
    tab_timeout = _step_wait_s("WORLDSHIP_IMPORT_EXPORT_TAB_TIMEOUT_S", 5.0)
    after_tab_s = _import_pacing_s(
        "WORLDSHIP_AFTER_IMPORT_EXPORT_TAB_S", 0.75, 0.4, win
    )

    if fast:
        return _activate_import_export_tab_fast(win, log=log, after_tab_s=after_tab_s)

    def _batch_visible() -> bool:
        return _batch_import_on_ribbon(win) or ribbon_action_available(
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

    if _fast_ribbon_clicks_enabled(win):
        emit("Fast ribbon: clicking Import-Export tab…")
        focus_main_window(win, log=emit)
        _activate_import_export_tab(win, log=emit)
        return

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
    fast = _fast_ribbon_clicks_enabled(win)
    verify_s = _import_pacing_s("WORLDSHIP_BATCH_IMPORT_VERIFY_S", 1.5, 0.7, win)
    attempts = _batch_import_attempts(win)
    poll_s = 0.06 if fast else 0.12

    focus_main_window(win, log=emit)
    mode = "fast calibrated" if fast else "standard"
    emit(f"Ribbon click engine {_RIBBON_VERSION} ({mode}) — opening Batch Import")

    if batch_import_wizard_open(win, app=app):
        emit("Batch Import wizard is already open.")
        return

    emit("Ensuring Import-Export tab is active…")
    ensure_import_export_tab(win, log=emit)

    if fast and not _batch_import_on_ribbon(win):
        emit(
            "WARN: Batch Import not on ribbon after tab click — "
            "Import-Export may not have activated."
        )

    coordinate_strategies: list[tuple[str, Callable[[], bool]]] = [
        (
            "coordinate default",
            lambda: _click_batch_import_by_position(win, log=emit),
        ),
        (
            "coordinate grid",
            lambda: _click_batch_import_coordinate_grid(win, log=emit, fast=fast),
        ),
    ]
    uia_strategies: list[tuple[str, Callable[[], bool]]] = [
        ("Win32 child", lambda: _click_batch_import_win32(win, log=emit)),
        ("UIA child_window", lambda: _click_batch_import_child_window(win, log=emit)),
        ("UIA exact", lambda: _click_batch_import_exact(win, log=emit)),
        ("UIA fuzzy", lambda: _click_batch_import_fuzzy(win, log=emit)),
    ]
    if fast:
        emit(
            "Fast ribbon: Win32/child_window first, then coordinates "
            "(Import-Export tab must be active)."
        )
        strategies = uia_strategies[:2] + coordinate_strategies
    elif _running_over_rdp(win):
        strategies = coordinate_strategies + uia_strategies
    else:
        strategies = uia_strategies + coordinate_strategies

    last_strategy = ""
    after_tab_s = _import_pacing_s(
        "WORLDSHIP_AFTER_IMPORT_EXPORT_TAB_S", 0.75, 0.4, win
    )
    for attempt in range(1, attempts + 1):
        if fast and (attempt > 1 or not _batch_import_on_ribbon(win)):
            emit(f"Batch Import try {attempt}/{attempts}: re-activate Import-Export tab…")
            _activate_import_export_tab_fast(win, log=emit, after_tab_s=after_tab_s)
        for strategy_name, fn in strategies:
            emit(f"Batch Import try {attempt}/{attempts}: {strategy_name}…")
            last_strategy = strategy_name
            if batch_import_wizard_open(win, app=app):
                emit("Batch Import wizard is open.")
                return
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
                time.sleep(poll_s)

    titles = _visible_top_level_windows_with_text("import")
    if titles:
        emit(f"Visible import-related windows: {titles[:6]}")
    if _uia_deep_scan_enabled(win):
        _dump_ribbon_names(win, log=emit)
    raise RuntimeError(
        "Could not open the Batch Import wizard after "
        f"{attempts} round(s) of ribbon strategies (last: {last_strategy!r}). "
        "Set WORLDSHIP_BATCH_IMPORT_OFFSET_X/Y in .env if coordinate clicks miss the button."
    )
