"""RDP-friendly ribbon/tab clicks for UPS WorldShip (multi-strategy)."""

from __future__ import annotations

import os
import re
import time
from typing import Callable

_RIBBON_VERSION = "ribbon-click-v4"


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


def _click_batch_import_by_position(
    win,
    *,
    log: Callable[[str], None],
) -> bool:
    """
    When UIA cannot name ribbon buttons (common over RDP), click the first large
    button in the ribbon content row below the Import-Export tab.
    """
    from pywinauto import mouse

    tab_rect = _import_export_tab_rect(win)
    if tab_rect is None:
        return False
    try:
        win_rect = win.rectangle()
    except Exception:
        return False

    offset_x = _step_wait_s("WORLDSHIP_BATCH_IMPORT_OFFSET_X", 60.0)
    offset_y = _step_wait_s("WORLDSHIP_BATCH_IMPORT_OFFSET_Y", 42.0)
    x = int(max(win_rect.left + 80, tab_rect.left - 40 + offset_x))
    y = int(tab_rect.bottom + offset_y)
    log(f"Coordinate click for Batch Import at ({x}, {y})…")
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
    deadline = time.monotonic() + timeout_s
    saw_any = False
    last_fg = ""
    emit(f"Ribbon click engine {_RIBBON_VERSION} — target {title!r}")

    try:
        root_hwnd = int(win.handle)
    except Exception:
        root_hwnd = 0

    while time.monotonic() < deadline:
        focus_main_window(win, log=emit)
        fg = foreground_window_title()
        if fg and fg != last_fg:
            emit(f"Foreground window: {fg!r}")
            last_fg = fg

        clicked = False
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
                    clicked = True
                    break
            if clicked:
                break

        if not clicked and root_hwnd:
            for ch in _enum_hwnds_with_text(root_hwnd, title):
                emit(f"Clicking {title!r} via Win32 hwnd {ch}…")
                if _click_hwnd(ch, log=emit):
                    clicked = True
                    break

        if clicked:
            time.sleep(0.25)
            return
        time.sleep(poll_s)

    if _name_matches(title, "import-export") or _name_matches(title, "import export"):
        emit("UIA did not find Import-Export tab — trying coordinate click…")
        if _click_import_export_by_position(win, log=emit):
            return

    if _name_matches(title, "batch import"):
        emit("UIA did not find Batch Import — trying coordinate click…")
        if _click_batch_import_by_position(win, log=emit):
            return

    _dump_ribbon_names(win, log=emit)
    hint = "no matching controls found" if not saw_any else "controls not clickable"
    raise RuntimeError(f"Could not click {title!r}: {hint}")


def ensure_import_export_tab(
    win,
    *,
    log: Callable[[str], None] | None = None,
) -> None:
    """Open Import-Export ribbon; only skip when Batch Import is already visible."""
    emit = log or _log_default
    if ribbon_action_available(
        win, "Batch Import", ("Button", "MenuItem", "SplitButton")
    ):
        emit("Import-Export ribbon already active — skipping tab click.")
        return

    tab_timeout = _step_wait_s("WORLDSHIP_IMPORT_EXPORT_TAB_TIMEOUT_S", 15.0)
    emit("Clicking Import-Export tab…")
    click_ribbon(
        win,
        title="Import-Export",
        control_types=("TabItem", "Button"),
        timeout_s=tab_timeout,
        log=emit,
    )

    verify_s = _step_wait_s("WORLDSHIP_AFTER_TAB_S", 0.5)
    # wait for ribbon to switch
    deadline = time.monotonic() + max(verify_s, 3.0)
    while time.monotonic() < deadline:
        if ribbon_action_available(
            win, "Batch Import", ("Button", "MenuItem", "SplitButton")
        ):
            emit("Verified: Batch Import visible on ribbon.")
            return
        time.sleep(0.12)

    emit("WARN: Batch Import not visible after tab click — retrying Import-Export…")
    click_ribbon(
        win,
        title="Import-Export",
        control_types=("TabItem", "Button"),
        timeout_s=tab_timeout,
        log=emit,
    )

    deadline = time.monotonic() + 4.0
    while time.monotonic() < deadline:
        if ribbon_action_available(
            win, "Batch Import", ("Button", "MenuItem", "SplitButton", "Hyperlink")
        ):
            emit("Verified: Batch Import visible on ribbon (after retry).")
            return
        time.sleep(0.12)

    _dump_ribbon_names(win, log=emit)
    raise RuntimeError(
        "Import-Export tab did not expose Batch Import on the ribbon. "
        "WorldShip may still be on Home, or UIA cannot see the ribbon over RDP."
    )


def click_batch_import(
    win,
    *,
    log: Callable[[str], None] | None = None,
) -> None:
    emit = log or _log_default
    timeout_s = _step_wait_s("WORLDSHIP_BATCH_IMPORT_CLICK_TIMEOUT_S", 20.0)
    emit("Clicking Batch Import…")
    focus_main_window(win, log=emit)
    custom = (os.environ.get("WORLDSHIP_BATCH_IMPORT_LABEL") or "").strip()
    labels = [custom] if custom else []
    labels.extend(["Batch Import", "Batch  Import"])
    deadline = time.monotonic() + timeout_s
    last_err: Exception | None = None
    for label in labels:
        remaining = deadline - time.monotonic()
        if remaining <= 0.5:
            break
        try:
            click_ribbon(
                win,
                title=label,
                control_types=(
                    "Button",
                    "MenuItem",
                    "SplitButton",
                    "Hyperlink",
                    "Text",
                ),
                timeout_s=remaining,
                log=emit,
            )
            return
        except RuntimeError as exc:
            last_err = exc
    if last_err:
        raise last_err
    raise RuntimeError("Could not click 'Batch Import': timed out")


def _step_wait_s(env_key: str, default: float) -> float:
    raw = (os.environ.get(env_key) or str(default)).strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default
