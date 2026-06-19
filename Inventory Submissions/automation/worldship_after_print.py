"""WorldShip after warehouse print: wait for processing, Close, End of Day, Batch Export."""

from __future__ import annotations

import os
import re
import time
from datetime import date


def _log(msg: str) -> None:
    print(f"[worldship] {msg}", flush=True)


def _step_wait_s(env_key: str, default: float) -> float:
    raw = (os.environ.get(env_key) or str(default)).strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


def _step_retry_attempts() -> int:
    raw = (os.environ.get("WORLDSHIP_STEP_RETRY_ATTEMPTS") or "5").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 5


def _step_retry_pause_s() -> float:
    return _step_wait_s("WORLDSHIP_STEP_RETRY_PAUSE_S", 12.0)


def _safe_enum_child_text(hwnd: int) -> list[str]:
    import win32gui

    parts: list[str] = []

    def _cb(child, _):
        try:
            t = (win32gui.GetWindowText(child) or "").strip()
            if t:
                parts.append(t)
        except Exception:
            pass
        return True

    try:
        win32gui.EnumChildWindows(hwnd, _cb, None)
    except Exception:
        pass
    return parts


def _enum_modal_hwnds(*, title_hint: str = "") -> list[tuple[int, str]]:
    import win32gui

    hint = title_hint.lower()
    out: list[tuple[int, str]] = []

    def _cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            title = (win32gui.GetWindowText(hwnd) or "").strip()
            cls = win32gui.GetClassName(hwnd) or ""
            if cls == "#32770" or (hint and hint in title.lower()):
                out.append((hwnd, title))
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:
        pass
    return out


def _find_button_hwnd(parent_hwnd: int, button_text: str) -> int:
    import win32gui

    target = button_text.lower().replace("&", "")
    found = 0

    def _cb(child, _):
        nonlocal found
        if found:
            return False
        try:
            if win32gui.GetClassName(child) != "Button":
                return True
            text = (win32gui.GetWindowText(child) or "").strip().lower().replace("&", "")
            if text == target:
                found = child
                return False
        except Exception:
            pass
        return True

    try:
        win32gui.EnumChildWindows(parent_hwnd, _cb, None)
    except Exception:
        pass
    return found


def _button_is_enabled(btn_hwnd: int) -> bool:
    import win32gui

    try:
        return bool(win32gui.IsWindowEnabled(btn_hwnd))
    except Exception:
        return False


def _click_button_win32(hwnd: int, button_text: str) -> bool:
    import win32con
    import win32gui

    btn = _find_button_hwnd(hwnd, button_text)
    if not btn or not _button_is_enabled(btn):
        return False
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass
    try:
        win32gui.PostMessage(btn, win32con.BM_CLICK, 0, 0)
        return True
    except Exception:
        return False


def _parse_progress_percent(hwnd: int) -> int | None:
    for part in _safe_enum_child_text(hwnd):
        m = re.search(r"(\d{1,3})\s*%", part)
        if m:
            return min(100, int(m.group(1)))
    blob = " ".join(_safe_enum_child_text(hwnd))
    m = re.search(r"(\d{1,3})\s*%", blob)
    if m:
        return min(100, int(m.group(1)))
    return None


def _find_automatic_processing_hwnd() -> tuple[int, str] | None:
    for hwnd, title in _enum_modal_hwnds():
        if "automatic processing progress" in title.lower():
            return hwnd, title
    return None


def _close_button_ready(progress_hwnd: int) -> bool:
    btn = _find_button_hwnd(progress_hwnd, "Close")
    return bool(btn and _button_is_enabled(btn))


def _processing_window_closed(hwnd: int) -> bool:
    import win32gui

    try:
        return not (win32gui.IsWindow(hwnd) and win32gui.IsWindowVisible(hwnd))
    except Exception:
        return True


def _wait_until_processing_window_closed(hwnd: int, *, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _processing_window_closed(hwnd):
            return True
        time.sleep(0.35)
    return _processing_window_closed(hwnd)


def _progress_dialog_text(hwnd: int) -> str:
    return " ".join(_safe_enum_child_text(hwnd)).lower()


def _still_completing_internally(hwnd: int) -> bool:
    """True when WorldShip shows 100% / done printing but Close is not ready yet."""
    if _close_button_ready(hwnd):
        return False
    pct = _parse_progress_percent(hwnd)
    text = _progress_dialog_text(hwnd)
    if "complet" in text:
        return True
    if pct is not None and pct >= 100:
        return True
    stats_remaining = None
    for part in _safe_enum_child_text(hwnd):
        m = re.match(r"remaining\s*:\s*(\d+)", part, re.I)
        if m:
            stats_remaining = int(m.group(1))
    return stats_remaining == 0 and pct is not None and pct >= 100


def _automatic_processing_open() -> bool:
    return _find_automatic_processing_hwnd() is not None


def _end_of_day_dialogs_open() -> bool:
    return any("end of day" in t.lower() for _, t in _enum_modal_hwnds())


def _wait_worldship_app_ready(main, *, timeout_s: float, step_label: str) -> None:
    """
    Wait until Automatic Processing / EOD modals are gone and a ribbon tab is clickable.
    WorldShip often needs 1–3 minutes after Close or End of Day before clicks work.
    """
    _log(f"{step_label}: waiting up to {timeout_s:.0f}s for WorldShip to accept clicks…")
    deadline = time.monotonic() + timeout_s
    last_log = 0.0
    while time.monotonic() < deadline:
        if _automatic_processing_open():
            if time.monotonic() - last_log >= 20.0:
                _log(f"{step_label}: Automatic Processing Progress still open…")
                last_log = time.monotonic()
            time.sleep(1.0)
            continue
        if _end_of_day_dialogs_open():
            if time.monotonic() - last_log >= 20.0:
                _log(f"{step_label}: End of Day dialog still open…")
                last_log = time.monotonic()
            time.sleep(1.0)
            continue
        if _home_tab_active(main) or _import_export_tab_active(main):
            _log(f"{step_label}: WorldShip ribbon is ready.")
            return
        if time.monotonic() - last_log >= 20.0:
            _log(f"{step_label}: WorldShip still busy — waiting for Home or Import-Export ribbon…")
            last_log = time.monotonic()
        time.sleep(1.0)
    raise TimeoutError(
        f"{step_label}: WorldShip did not become clickable within {timeout_s:.0f}s."
    )


def _warehouse_print_complete_timeout_s(print_count: int) -> float:
    base_raw = (os.environ.get("WORLDSHIP_PRINT_COMPLETE_TIMEOUT_S") or "2400").strip()
    per_raw = (os.environ.get("WORLDSHIP_PRINT_COMPLETE_PER_LABEL_S") or "8").strip()
    try:
        base = max(300.0, float(base_raw))
    except ValueError:
        base = 2400.0
    try:
        per = max(3.0, float(per_raw))
    except ValueError:
        per = 8.0
    return max(base, print_count * per)


def _wait_for_print_processing_complete(*, print_count: int) -> None:
    """Wait until Automatic Processing Progress hits 100% and Close is enabled."""
    timeout_s = _warehouse_print_complete_timeout_s(print_count)
    _log(
        f"Waiting for warehouse printing to finish (up to {timeout_s:.0f}s for "
        f"{print_count} label(s))…"
    )
    _log("Do NOT click Stop or Close — automation will click Close when ready.")

    deadline = time.monotonic() + timeout_s
    last_log = 0.0
    seen_progress = False

    while time.monotonic() < deadline:
        found = _find_automatic_processing_hwnd()
        if found:
            seen_progress = True
            hwnd, title = found
            pct = _parse_progress_percent(hwnd)
            close_ok = _close_button_ready(hwnd)
            stats_remaining = None
            for part in _safe_enum_child_text(hwnd):
                m = re.match(r"remaining\s*:\s*(\d+)", part, re.I)
                if m:
                    stats_remaining = int(m.group(1))

            if time.monotonic() - last_log >= 15.0:
                pct_s = f"{pct}%" if pct is not None else "?"
                completing = _still_completing_internally(hwnd)
                _log(
                    f"Printing… {title!r} — {pct_s}, "
                    f"Close enabled={close_ok}"
                    + (", completing internally" if completing else "")
                    + (
                        f", remaining={stats_remaining}"
                        if stats_remaining is not None
                        else ""
                    )
                )
                last_log = time.monotonic()
            elif _still_completing_internally(hwnd) and time.monotonic() - last_log >= 15.0:
                _log(
                    f"At 100% but Close still greyed on {title!r} — "
                    "WorldShip is completing internally (may take 1+ min)…"
                )
                last_log = time.monotonic()

            if close_ok and (pct is None or pct >= 100 or stats_remaining == 0):
                stable_s = _step_wait_s("WORLDSHIP_CLOSE_STABLE_S", 8.0)
                pct_label = f"{pct}%" if pct is not None else "?"
                _log(
                    f"Printing complete — Close enabled ({pct_label}); "
                    f"waiting {stable_s:.1f}s for Close to stay enabled…"
                )
                stable_deadline = time.monotonic() + stable_s
                while time.monotonic() < stable_deadline:
                    still = _find_automatic_processing_hwnd()
                    if not still:
                        _log("Automatic Processing Progress closed during stability wait.")
                        return
                    if not _close_button_ready(still[0]):
                        break
                    time.sleep(0.4)
                else:
                    _log("Close button stayed enabled — ready to click Close.")
                    return
        elif seen_progress:
            _log("Automatic Processing Progress closed — printing likely finished.")
            return

        time.sleep(0.5)

    raise TimeoutError(
        f"Timed out after {timeout_s:.0f}s waiting for warehouse printing to finish "
        f"({print_count} label(s))."
    )


def _click_close_on_processing() -> None:
    """Wait for Close to be enabled, click it, and verify the progress window closes."""
    if not _find_automatic_processing_hwnd():
        _log("No Automatic Processing Progress window — skipping Close click.")
        return

    close_wait = _step_wait_s("WORLDSHIP_CLOSE_READY_TIMEOUT_S", 420.0)
    verify_close_s = _step_wait_s("WORLDSHIP_CLOSE_DIALOG_DISMISS_S", 120.0)
    pre_click_s = _step_wait_s("WORLDSHIP_BEFORE_CLOSE_CLICK_S", 5.0)
    stable_s = _step_wait_s("WORLDSHIP_CLOSE_STABLE_S", 8.0)
    attempts = _step_retry_attempts()

    for attempt in range(1, attempts + 1):
        if not _find_automatic_processing_hwnd():
            _log("Automatic Processing Progress window already closed.")
            return

        _log(f"Close step attempt {attempt}/{attempts}…")
        deadline = time.monotonic() + close_wait
        ready_since: float | None = None
        closed = False

        while time.monotonic() < deadline:
            found = _find_automatic_processing_hwnd()
            if not found:
                _log("Automatic Processing Progress window closed.")
                return
            hwnd, title = found
            if _still_completing_internally(hwnd) and not _close_button_ready(hwnd):
                time.sleep(0.8)
                continue
            if _close_button_ready(hwnd):
                if ready_since is None:
                    ready_since = time.monotonic()
                    _log(f"Close enabled on {title!r} — holding {stable_s:.1f}s…")
                elif time.monotonic() - ready_since >= stable_s:
                    _log(f"Pausing {pre_click_s:.1f}s, then clicking Close…")
                    time.sleep(pre_click_s)
                    clicked_hwnd = hwnd
                    if _click_button_win32(clicked_hwnd, "Close"):
                        _log(f"Clicked Close on {title!r}.")
                        if _wait_until_processing_window_closed(
                            clicked_hwnd, timeout_s=verify_close_s
                        ):
                            _log("Verified: Automatic Processing Progress window closed.")
                            closed = True
                            break
                        _log(
                            "Close clicked but window still open — will retry Close "
                            "when button is enabled again…"
                        )
                        ready_since = None
            else:
                if ready_since is not None:
                    _log("Close greyed out again — waiting…")
                ready_since = None
            time.sleep(0.5)

        if closed:
            return
        if attempt < attempts:
            _log(
                f"Close step not verified on attempt {attempt} — "
                f"retrying in {_step_retry_pause_s():.0f}s…"
            )
            time.sleep(_step_retry_pause_s())

    raise TimeoutError(
        "Could not close Automatic Processing Progress after "
        f"{attempts} attempt(s) (up to {close_wait:.0f}s each)."
    )


def _nudge_worldship_main(main) -> None:
    """Double-click main window client area to help WorldShip finish loading."""
    import win32api
    import win32con
    import win32gui

    nudges = int((os.environ.get("WORLDSHIP_LOAD_NUDGE_CLICKS") or "2").strip() or "2")
    pause = _step_wait_s("WORLDSHIP_LOAD_NUDGE_PAUSE_S", 0.6)
    try:
        hwnd = main.handle
        if not hwnd:
            rect = main.rectangle()
            x, y = (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2
        else:
            rect = win32gui.GetWindowRect(hwnd)
            x = (rect[0] + rect[2]) // 2
            y = (rect[1] + rect[3]) // 2 + 40
        win32gui.SetForegroundWindow(hwnd or main.handle)
        time.sleep(0.2)
        for i in range(max(1, nudges)):
            win32api.SetCursorPos((x, y))
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            time.sleep(pause)
        _log(f"Nudged WorldShip main window ({nudges} click(s)) to finish loading.")
    except Exception as exc:
        _log(f"WARN: could not nudge main window: {exc}")


def _matching_controls(
    win,
    *,
    title: str,
    control_types: tuple[str, ...],
    max_index: int = 3,
):
    exist_ms = 30
    for ctrl in control_types:
        for i in range(max_index):
            try:
                target = win.child_window(title=title, control_type=ctrl, found_index=i)
                if not target.exists(timeout=exist_ms / 1000.0):
                    break
                yield target
            except Exception:
                break


def _is_tab_selected(target) -> bool:
    try:
        if target.is_selected():
            return True
    except Exception:
        pass
    try:
        return bool(target.get_toggle_state())
    except Exception:
        pass
    return False


def _ribbon_action_available(
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


def _click_ribbon(main, title: str, *, timeout_s: float = 8.0) -> None:
    from automation.worldship_ribbon_click import click_ribbon, focus_main_window

    focus_main_window(main, log=_log)
    click_ribbon(
        main,
        title=title,
        control_types=("TabItem", "Button", "SplitButton", "MenuItem"),
        timeout_s=timeout_s,
        log=_log,
    )


def _home_tab_active(main) -> bool:
    if _ribbon_action_available(main, "End of Day", ("Button", "SplitButton", "MenuItem")):
        return True
    for target in _matching_controls(main, title="Home", control_types=("TabItem",)):
        if _is_tab_selected(target):
            return True
    return False


def _ensure_home_tab(main) -> None:
    attempts = _step_retry_attempts()
    ready_timeout = _step_wait_s("WORLDSHIP_APP_READY_TIMEOUT_S", 180.0)
    tab_timeout = _step_wait_s("WORLDSHIP_HOME_TAB_TIMEOUT_S", 120.0)
    after_tab_s = _step_wait_s("WORLDSHIP_AFTER_HOME_TAB_S", 8.0)

    for attempt in range(1, attempts + 1):
        if _home_tab_active(main):
            _log("Verified: Home tab active (End of Day available).")
            return

        _log(f"Home tab attempt {attempt}/{attempts}…")
        _wait_worldship_app_ready(main, timeout_s=ready_timeout, step_label="Before Home tab")

        try:
            _click_ribbon(main, "Home", timeout_s=30.0)
            _log("Clicked Home tab.")
        except Exception as exc:
            _log(f"Home tab click failed: {exc}")
            if attempt < attempts:
                time.sleep(_step_retry_pause_s())
            continue

        deadline = time.monotonic() + tab_timeout
        while time.monotonic() < deadline:
            if _home_tab_active(main):
                _log("Verified: Home tab — End of Day is on the ribbon.")
                time.sleep(after_tab_s)
                return
            time.sleep(0.5)

        _log(f"Home tab not verified after click (attempt {attempt}/{attempts}).")
        if attempt < attempts:
            time.sleep(_step_retry_pause_s())

    raise TimeoutError(
        f"Home tab did not become active after {attempts} attempt(s)."
    )


def _import_export_tab_active(main) -> bool:
    if _ribbon_action_available(
        main, "Batch Import", ("Button", "MenuItem", "SplitButton")
    ):
        return True
    if _ribbon_action_available(
        main, "Batch Export", ("Button", "MenuItem", "SplitButton")
    ):
        return True
    for target in _matching_controls(main, title="Import-Export", control_types=("TabItem",)):
        if _is_tab_selected(target):
            return True
    return False


def _ensure_import_export_tab(main) -> None:
    attempts = _step_retry_attempts()
    ready_timeout = _step_wait_s("WORLDSHIP_APP_READY_TIMEOUT_S", 180.0)
    tab_timeout = _step_wait_s("WORLDSHIP_IMPORT_EXPORT_TAB_TIMEOUT_S", 120.0)
    after_tab_s = _step_wait_s("WORLDSHIP_AFTER_IMPORT_EXPORT_TAB_S", 8.0)

    for attempt in range(1, attempts + 1):
        if _import_export_tab_active(main):
            _log("Verified: Import-Export tab active (Batch Export available).")
            return

        _log(f"Import-Export tab attempt {attempt}/{attempts}…")
        _wait_worldship_app_ready(
            main, timeout_s=ready_timeout, step_label="Before Import-Export tab"
        )

        try:
            _click_ribbon(main, "Import-Export", timeout_s=30.0)
            _log("Clicked Import-Export tab.")
        except Exception as exc:
            _log(f"Import-Export tab click failed: {exc}")
            if attempt < attempts:
                time.sleep(_step_retry_pause_s())
            continue

        deadline = time.monotonic() + tab_timeout
        while time.monotonic() < deadline:
            if _import_export_tab_active(main):
                _log("Verified: Import-Export tab — Batch Export is on the ribbon.")
                time.sleep(after_tab_s)
                return
            time.sleep(0.5)

        _log(f"Import-Export tab not verified after click (attempt {attempt}/{attempts}).")
        if attempt < attempts:
            time.sleep(_step_retry_pause_s())

    raise TimeoutError(
        f"Import-Export tab did not become active after {attempts} attempt(s)."
    )


def _wait_for_dialog(title_hint: str, *, timeout_s: float) -> int:
    import win32gui

    hint = title_hint.lower()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for hwnd, title in _enum_modal_hwnds(title_hint=title_hint):
            if hint in title.lower():
                try:
                    win32gui.SetForegroundWindow(hwnd)
                except Exception:
                    pass
                _log(f"Found dialog: {title!r}")
                return hwnd
        time.sleep(0.2)
    visible = [t for _, t in _enum_modal_hwnds()]
    raise TimeoutError(
        f"Timed out waiting for dialog {title_hint!r}. Visible: {visible or '(none)'}"
    )


def _end_of_day_timeout_s() -> float:
    raw = (os.environ.get("WORLDSHIP_END_OF_DAY_TIMEOUT_S") or "900").strip()
    try:
        return max(120.0, float(raw))
    except ValueError:
        return 900.0


def _wait_for_end_of_day_complete(main) -> None:
    """Wait until EOD dialogs are gone and WorldShip accepts ribbon clicks again."""
    eod_timeout = _end_of_day_timeout_s()
    ready_timeout = _step_wait_s("WORLDSHIP_AFTER_EOD_READY_S", 240.0)
    settle_s = _step_wait_s("WORLDSHIP_AFTER_EOD_SETTLE_S", 15.0)

    _log(f"Waiting up to {eod_timeout:.0f}s for End of Day processing to finish…")
    deadline = time.monotonic() + eod_timeout
    last_log = 0.0
    while time.monotonic() < deadline:
        for hwnd, title in _enum_modal_hwnds():
            low = title.lower()
            if "end of day" in low and "processing" in low:
                if _close_button_ready(hwnd) and _click_button_win32(hwnd, "Close"):
                    _log(f"Clicked Close on {title!r}.")
                elif _click_button_win32(hwnd, "OK"):
                    _log(f"Clicked OK on {title!r}.")
            elif _click_button_win32(hwnd, "OK"):
                _log(f"Dismissed {title!r} during End of Day.")
        if not _end_of_day_dialogs_open():
            _log("End of Day dialogs closed — waiting for WorldShip to settle…")
            time.sleep(settle_s)
            if not _end_of_day_dialogs_open():
                _wait_worldship_app_ready(
                    main, timeout_s=ready_timeout, step_label="After End of Day"
                )
                _log("Verified: End of Day complete and WorldShip is clickable.")
                return
        if time.monotonic() - last_log >= 20.0:
            _log("End of Day still running…")
            last_log = time.monotonic()
        time.sleep(1.0)

    raise TimeoutError(
        f"Timed out after {eod_timeout:.0f}s waiting for End of Day to finish."
    )


def _run_end_of_day(main) -> None:
    attempts = _step_retry_attempts()
    ready_timeout = _step_wait_s("WORLDSHIP_APP_READY_TIMEOUT_S", 180.0)

    for attempt in range(1, attempts + 1):
        _log(f"End of Day attempt {attempt}/{attempts}…")
        _ensure_home_tab(main)
        _wait_worldship_app_ready(main, timeout_s=ready_timeout, step_label="Before End of Day")

        if not _home_tab_active(main):
            _log("Home tab lost before End of Day — retrying Home tab…")
            continue

        try:
            _click_ribbon(main, "End of Day", timeout_s=30.0)
            eod_hwnd = _wait_for_dialog("End of Day Processing", timeout_s=60.0)
            if not _click_button_win32(eod_hwnd, "Yes"):
                raise RuntimeError('Could not click Yes on "End of Day Processing".')
            _log("Clicked Yes on End of Day Processing.")
            _wait_for_end_of_day_complete(main)
            return
        except Exception as exc:
            _log(f"End of Day attempt {attempt} failed: {exc}")
            if attempt < attempts:
                time.sleep(_step_retry_pause_s())

    raise RuntimeError(f"End of Day failed after {attempts} attempt(s).")


def _worldship_date_display(d: date) -> str:
    """WorldShip export field format, e.g. 18-May-2026 (day not zero-padded)."""
    return f"{d.day}-{d.strftime('%b')}-{d.year}"


def _normalize_export_date(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").strip().lower())


def _export_date_matches(field_text: str, want: str) -> bool:
    norm_field = _normalize_export_date(field_text)
    norm_want = _normalize_export_date(want)
    if norm_field == norm_want:
        return True
    # Accept same calendar day if month/day/year tokens match loosely.
    return norm_want in norm_field or norm_field.endswith(norm_want.split("-", 1)[-1])


def _enum_visible_edit_hwnds(parent_hwnd: int) -> list[int]:
    import win32gui

    edits: list[int] = []

    def _cb(child, _):
        try:
            if win32gui.GetClassName(child) == "Edit" and win32gui.IsWindowVisible(child):
                edits.append(child)
        except Exception:
            pass
        return True

    try:
        win32gui.EnumChildWindows(parent_hwnd, _cb, None)
    except Exception:
        pass
    return edits


def _read_edit_hwnd_text(edit_hwnd: int) -> str:
    import win32con
    import win32gui

    try:
        n = win32gui.SendMessage(edit_hwnd, win32con.WM_GETTEXTLENGTH, 0, 0)
        if n <= 0:
            return ""
        buf = __import__("ctypes").create_unicode_buffer(n + 2)
        win32gui.SendMessage(edit_hwnd, win32con.WM_GETTEXT, n + 1, buf)
        return buf.value.strip()
    except Exception:
        return ""


def _notify_edit_changed(edit_hwnd: int) -> None:
    import win32con
    import win32gui

    try:
        parent = win32gui.GetParent(edit_hwnd)
        ctrl_id = win32gui.GetDlgCtrlID(edit_hwnd)
        if parent and ctrl_id:
            win32gui.SendMessage(
                parent,
                win32con.WM_COMMAND,
                (win32con.EN_CHANGE << 16) | (ctrl_id & 0xFFFF),
                edit_hwnd,
            )
    except Exception:
        pass


def _pick_batch_export_date_edit_hwnd(export_hwnd: int) -> int:
    import win32gui

    enabled: list[int] = []
    for edit in _enum_visible_edit_hwnds(export_hwnd):
        try:
            if win32gui.IsWindowEnabled(edit):
                enabled.append(edit)
        except Exception:
            continue
    if not enabled:
        return 0
    for edit in enabled:
        if re.search(r"-\w{3}-", _read_edit_hwnd_text(edit)):
            return edit
    return enabled[0]


def _focus_edit_hwnd_click(edit_hwnd: int, export_hwnd: int) -> None:
    """Mouse-click the center of a date edit field (commits better than WM_SETTEXT alone)."""
    import win32gui
    from pywinauto import mouse

    try:
        win32gui.SetForegroundWindow(export_hwnd)
        left, top, right, bottom = win32gui.GetWindowRect(edit_hwnd)
        x = (left + right) // 2
        y = (top + bottom) // 2
        mouse.click(button="left", coords=(x, y))
        time.sleep(0.15)
    except Exception:
        pass


def _type_date_into_edit_hwnd(edit_hwnd: int, want: str, *, export_hwnd: int) -> bool:
    """Focus the date field and type today's date (no calendar picker)."""
    import win32con
    import win32gui
    from pywinauto import keyboard

    try:
        win32gui.SetForegroundWindow(export_hwnd)
    except Exception:
        pass
    _focus_edit_hwnd_click(edit_hwnd, export_hwnd)

    try:
        win32gui.SetFocus(edit_hwnd)
    except Exception:
        pass
    time.sleep(0.15)

    try:
        win32gui.SendMessage(edit_hwnd, win32con.EM_SETSEL, 0, -1)
        win32gui.SendMessage(edit_hwnd, win32con.WM_SETTEXT, 0, want)
        _notify_edit_changed(edit_hwnd)
        time.sleep(0.2)
        if _export_date_matches(_read_edit_hwnd_text(edit_hwnd), want):
            return True
    except Exception:
        pass

    try:
        _focus_edit_hwnd_click(edit_hwnd, export_hwnd)
        keyboard.send_keys("^a", pause=0.05)
        keyboard.send_keys("{BACKSPACE}", pause=0.03)
        keyboard.send_keys(want, with_spaces=True, pause=0.03)
        time.sleep(0.15)
        keyboard.send_keys("{TAB}", pause=0.05)
        time.sleep(0.2)
        return _export_date_matches(_read_edit_hwnd_text(edit_hwnd), want)
    except Exception:
        return False


def _type_date_into_edit_uia(edit, want: str) -> bool:
    from pywinauto import keyboard

    try:
        edit.set_focus()
        edit.click_input()
        time.sleep(0.15)
        keyboard.send_keys("^a", pause=0.05)
        keyboard.send_keys(want, with_spaces=True, pause=0.03)
        time.sleep(0.15)
        keyboard.send_keys("{TAB}", pause=0.05)
        time.sleep(0.2)
        try:
            current = (edit.window_text() or "").strip()
        except Exception:
            current = ""
        return _export_date_matches(current, want)
    except Exception:
        return False


def _batch_export_set_date(app, export_hwnd: int, today: date) -> None:
    """Check 'All records on or after' and type today's date into the field (no calendar)."""
    from automation.worldship_batch_import import _ensure_checkbox_checked_win32

    want = _worldship_date_display(today)

    if not _ensure_checkbox_checked_win32(export_hwnd, "on or after"):
        dlg = app.window(handle=export_hwnd)
        checkboxes = []
        for cb in dlg.descendants(control_type="CheckBox"):
            try:
                if cb.is_visible():
                    checkboxes.append(cb)
            except Exception:
                continue
        if not checkboxes:
            raise RuntimeError("No checkboxes found on Batch export data dialog.")
        box = checkboxes[0]
        if box.get_toggle_state() != 1:
            box.click_input()
            time.sleep(0.35)
    _log("Checked 'All records on or after'.")

    time.sleep(_step_wait_s("WORLDSHIP_BATCH_EXPORT_AFTER_CHECK_S", 0.4))
    _log(f"Typing export date manually: {want!r}")

    edit_hwnd = _pick_batch_export_date_edit_hwnd(export_hwnd)
    if edit_hwnd and _type_date_into_edit_hwnd(
        edit_hwnd, want, export_hwnd=export_hwnd
    ):
        current = _read_edit_hwnd_text(edit_hwnd)
        _log(f"Export date field set to {current!r} (Win32/keyboard).")
        if not _export_date_matches(current, want):
            raise RuntimeError(
                f"Export date field shows {current!r} but expected {want!r}."
            )
        return

    dlg = app.window(handle=export_hwnd)
    for edit in dlg.descendants(control_type="Edit"):
        try:
            if not edit.is_visible() or not edit.is_enabled():
                continue
        except Exception:
            continue
        if _type_date_into_edit_uia(edit, want):
            try:
                current = (edit.window_text() or "").strip()
            except Exception:
                current = want
            _log(f"Export date field set to {current!r} (UIA/keyboard).")
            if not _export_date_matches(current, want):
                raise RuntimeError(
                    f"Export date field shows {current!r} but expected {want!r}."
                )
            return

    raise RuntimeError(
        f"Could not set batch export date to {want!r}. "
        "The date field may not be enabled or visible over RDP."
    )


def _check_all_records_on_or_after_uia(app, export_hwnd: int, today: date) -> None:
    want = _worldship_date_display(today)
    _log(f"Batch export: check 'All records on or after' and set date to {want!r}")
    _batch_export_set_date(app, export_hwnd, today)
    time.sleep(_step_wait_s("WORLDSHIP_BATCH_EXPORT_AFTER_DATE_S", 0.8))
    _log("Batch export date option set (today).")


def _open_batch_export_dialog(app, main) -> int:
    """Import-Export tab → Batch Export → wait for export data dialog."""
    from automation.worldship_ribbon_click import (
        _import_pacing_s,
        click_batch_export_for_export,
        ensure_import_export_tab_for_export,
        focus_main_window,
    )

    attempts = _step_retry_attempts()

    for attempt in range(1, attempts + 1):
        _log(f"Batch Export attempt {attempt}/{attempts}…")
        try:
            focus_main_window(main, log=_log)
            after_fg_s = _import_pacing_s("WORLDSHIP_AFTER_FOREGROUND_S", 1.5, 0.4, main)
            if after_fg_s > 0:
                _log(f"Waiting {after_fg_s:.1f}s after foreground…")
                time.sleep(after_fg_s)
            ensure_import_export_tab_for_export(main, log=_log)
            click_batch_export_for_export(main, log=_log)
            export_hwnd = _wait_for_dialog("Batch export", timeout_s=45.0)
            _log("Verified: Batch export dialog opened.")
            return export_hwnd
        except Exception as exc:
            _log(f"Batch Export open failed (attempt {attempt}): {exc}")
            if attempt >= attempts:
                raise
            time.sleep(_step_retry_pause_s())
    raise RuntimeError("Batch Export dialog could not be opened.")


def run_batch_export_workflow(app, main, *, today: date | None = None) -> None:
    """Import-Export → Batch Export → today's date → preview → Save (tracking CSV)."""
    export_day = today or date.today()
    _log(f"=== WorldShip Batch Export (Depot Shipments, {export_day.isoformat()}) ===")
    export_hwnd = _open_batch_export_dialog(app, main)
    _log("Depot Shipments map should be selected at top (default).")
    _check_all_records_on_or_after_uia(app, export_hwnd, export_day)
    _complete_batch_export_dialogs(export_hwnd)


def _complete_batch_export_dialogs(export_hwnd: int) -> None:
    if not _click_button_win32(export_hwnd, "Next"):
        raise RuntimeError('Could not click Next on "Batch export data".')
    _log("Clicked Next on Batch export data.")

    preview_hwnd = _wait_for_dialog("Import/Export Preview", timeout_s=60.0)
    preview_text = " ".join(_safe_enum_child_text(preview_hwnd)).lower()
    if "export" in preview_text or "to be exported" in preview_text:
        _log("Import/Export Preview (export) loaded.")
    if not _click_button_win32(preview_hwnd, "Next"):
        raise RuntimeError("Could not click Next on Import/Export Preview (export).")
    _log("Clicked Next on export preview.")

    progress_hwnd = _wait_for_dialog("Progress", timeout_s=45.0)
    export_deadline = time.monotonic() + _step_wait_s(
        "WORLDSHIP_BATCH_EXPORT_TIMEOUT_S", 600.0
    )
    last_log = 0.0
    while time.monotonic() < export_deadline:
        pct = _parse_progress_percent(progress_hwnd)
        if pct is not None and pct >= 100:
            time.sleep(1.0)
            break
        if time.monotonic() - last_log >= 12.0:
            _log(f"Export progress… {pct if pct is not None else '?'}%")
            last_log = time.monotonic()
        if not _dialog_visible(progress_hwnd):
            break
        time.sleep(0.5)

    summary_hwnd = _wait_for_dialog("Import/Export Summary", timeout_s=120.0)
    if "processed" in " ".join(_safe_enum_child_text(summary_hwnd)).lower():
        _log("Import/Export Summary ready.")
    if not _click_button_win32(summary_hwnd, "Save"):
        raise RuntimeError('Could not click Save on "Import/Export Summary".')
    _log("Clicked Save on Import/Export Summary — batch export complete.")


def run_worldship_batch_export(*, export_date: date | None = None) -> None:
    """Standalone entry: connect to WorldShip and run batch export only."""
    from automation.worldship_batch_import import (
        _connect_or_start,
        _focus_main_window,
        _require_pywinauto,
        _resolve_main_window,
        _startup_timeout_s,
    )
    from automation.worldship_ribbon_click import _running_over_rdp

    Application, _ = _require_pywinauto()
    app, cold = _connect_or_start(Application, startup_timeout_s=_startup_timeout_s())
    main = _resolve_main_window(app, cold_start=cold)
    _focus_main_window(main)
    if _running_over_rdp(main):
        _log("Export: using calibrated screen coordinates (Remote Workstation).")
    run_batch_export_workflow(app, main, today=export_date)


def _run_batch_export(app, main, *, today: date) -> None:
    run_batch_export_workflow(app, main, today=today)


def _dialog_visible(hwnd: int) -> bool:
    import win32gui

    try:
        return bool(win32gui.IsWindow(hwnd) and win32gui.IsWindowVisible(hwnd))
    except Exception:
        return False


def run_after_print_workflow(app, main, *, print_label_count: int) -> None:
    """
    After labels: wait for printing (if any) → Close → End of Day → Batch Export.
    """
    if print_label_count > 0:
        _log(f"=== Phase 3: wait for {print_label_count} warehouse print(s), then Close ===")
        _wait_for_print_processing_complete(print_count=print_label_count)
    else:
        _log("=== Phase 3: no warehouse prints — close processing if still open ===")
        found = _find_automatic_processing_hwnd()
        if found:
            deadline = time.monotonic() + 120.0
            while time.monotonic() < deadline:
                if _close_button_ready(found[0]):
                    break
                time.sleep(0.5)
        else:
            _log("No Automatic Processing Progress window open.")

    _click_close_on_processing()

    after_close_ready = _step_wait_s("WORLDSHIP_AFTER_CLOSE_READY_S", 180.0)
    after_close_settle = _step_wait_s("WORLDSHIP_AFTER_CLOSE_SETTLE_S", 20.0)
    _log(
        f"After Close: waiting up to {after_close_ready:.0f}s for WorldShip to accept clicks "
        f"(then {after_close_settle:.0f}s settle)…"
    )
    _wait_worldship_app_ready(main, timeout_s=after_close_ready, step_label="After Close")
    time.sleep(after_close_settle)
    _nudge_worldship_main(main)
    time.sleep(_step_wait_s("WORLDSHIP_AFTER_NUDGE_WAIT_S", 10.0))
    if not _home_tab_active(main) and not _import_export_tab_active(main):
        _log("Ribbon still not verified after nudge — waiting again…")
        _wait_worldship_app_ready(main, timeout_s=after_close_ready, step_label="After Close nudge")

    _log("=== Phase 4: Home tab → End of Day ===")
    _run_end_of_day(main)

    _log("=== Phase 5: Batch Export (Depot Shipments, today) ===")
    run_batch_export_workflow(app, main, today=date.today())
    _log("WorldShip batch import + export workflow complete.")
