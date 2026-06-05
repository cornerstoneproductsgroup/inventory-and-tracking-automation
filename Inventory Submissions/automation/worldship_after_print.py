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
                _log(
                    f"Printing… {title!r} — {pct_s}, "
                    f"Close enabled={close_ok}"
                    + (
                        f", remaining={stats_remaining}"
                        if stats_remaining is not None
                        else ""
                    )
                )
                last_log = time.monotonic()

            if close_ok and (pct is None or pct >= 100 or stats_remaining == 0):
                _log("Printing complete — Close button is ready.")
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
    found = _find_automatic_processing_hwnd()
    if not found:
        _log("No Automatic Processing Progress window — skipping Close click.")
        return
    hwnd, title = found
    deadline = time.monotonic() + 120.0
    while time.monotonic() < deadline:
        if _close_button_ready(hwnd):
            if _click_button_win32(hwnd, "Close"):
                _log(f"Clicked Close on {title!r}.")
                time.sleep(0.8)
                return
        time.sleep(0.4)
    raise TimeoutError("Close button on Automatic Processing Progress never became clickable.")


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


def _click_ribbon(main, title: str, *, timeout_s: float = 8.0) -> None:
    deadline = time.monotonic() + timeout_s
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        for ctrl_type in ("Button", "SplitButton", "MenuItem", "TabItem"):
            try:
                target = main.child_window(title=title, control_type=ctrl_type)
                if target.exists(timeout=0.05) and target.is_visible() and target.is_enabled():
                    target.click_input()
                    return
            except Exception as exc:
                last_err = exc
        time.sleep(0.08)
    raise RuntimeError(f"Could not click ribbon {title!r}: {last_err}")


def _ensure_import_export_tab(main) -> None:
    try:
        bi = main.child_window(title="Batch Import", control_type="Button")
        if bi.exists(timeout=0.1) and bi.is_visible() and bi.is_enabled():
            _log("Import-Export tab already active.")
            return
    except Exception:
        pass
    _click_ribbon(main, "Import-Export", timeout_s=6.0)
    _log("Clicked Import-Export tab.")


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


def _run_end_of_day(main) -> None:
    _log("Starting End of Day…")
    _click_ribbon(main, "End of Day", timeout_s=10.0)
    eod_hwnd = _wait_for_dialog("End of Day Processing", timeout_s=45.0)
    if not _click_button_win32(eod_hwnd, "Yes"):
        raise RuntimeError('Could not click Yes on "End of Day Processing".')
    _log("Clicked Yes on End of Day Processing.")

    deadline = time.monotonic() + _end_of_day_timeout_s()
    while time.monotonic() < deadline:
        for hwnd, title in _enum_modal_hwnds():
            low = title.lower()
            if "end of day" in low and "processing" in low:
                if _close_button_ready(hwnd) or _click_button_win32(hwnd, "Close"):
                    _log(f"End of Day dialog closed ({title!r}).")
                    time.sleep(1.0)
                    return
            if _click_button_win32(hwnd, "OK"):
                _log(f"Dismissed {title!r} during End of Day.")
                time.sleep(0.5)
        if not any(
            "end of day" in t.lower() for _, t in _enum_modal_hwnds()
        ):
            time.sleep(2.0)
            if not any(
                "end of day" in t.lower() for _, t in _enum_modal_hwnds()
            ):
                _log("End of Day processing finished.")
                return
        time.sleep(1.0)

    raise TimeoutError("Timed out waiting for End of Day processing to finish.")


def _worldship_date_display(d: date) -> str:
    return f"{d.day:02d}-{d.strftime('%b')}-{d.year}"


def _batch_export_uia_date(dlg, today: date) -> None:
    """Check 'All records on or after' and pick today's date from the calendar."""
    want = _worldship_date_display(today)
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

    for btn in dlg.descendants(control_type="Button"):
        try:
            if not btn.is_visible():
                continue
            r = btn.rectangle()
            if r.width() < 45 and r.height() < 35 and r.width() > 10:
                btn.click_input()
                time.sleep(0.5)
                break
        except Exception:
            continue

    try:
        today_btn = dlg.child_window(title_re=r".*Today.*")
        if today_btn.exists(timeout=2.0):
            today_btn.click_input()
            time.sleep(0.35)
            _log("Selected today from calendar.")
            return
    except Exception:
        pass

    for edit in dlg.descendants(control_type="Edit"):
        try:
            if edit.is_visible():
                edit.set_edit_text(want)
                time.sleep(0.25)
                _log(f"Set export date field to {want!r}.")
                return
        except Exception:
            continue


def _check_all_records_on_or_after_uia(app, export_hwnd: int, today: date) -> None:
    want = _worldship_date_display(today)
    _log(f"Batch export: check 'All records on or after' and set date to {want!r}")
    dlg = app.window(handle=export_hwnd)
    _batch_export_uia_date(dlg, today)
    time.sleep(_step_wait_s("WORLDSHIP_BATCH_EXPORT_AFTER_DATE_S", 0.8))
    _log("Batch export date option set (today).")


def _run_batch_export(app, main, *, today: date) -> None:
    _ensure_import_export_tab(main)
    _log('Clicking "Batch Export"…')
    _click_ribbon(main, "Batch Export", timeout_s=10.0)

    export_hwnd = _wait_for_dialog("Batch export", timeout_s=30.0)
    _log("Depot Shipments map should be selected at top (default).")
    _check_all_records_on_or_after_uia(app, export_hwnd, today)

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

    settle = _step_wait_s("WORLDSHIP_AFTER_CLOSE_SETTLE_S", 2.0)
    _log(f"Waiting {settle:.1f}s after Close…")
    time.sleep(settle)
    _nudge_worldship_main(main)
    time.sleep(_step_wait_s("WORLDSHIP_AFTER_NUDGE_WAIT_S", 3.0))

    _log("=== Phase 4: End of Day ===")
    _run_end_of_day(main)
    _nudge_worldship_main(main)
    time.sleep(_step_wait_s("WORLDSHIP_AFTER_EOD_WAIT_S", 2.0))

    _log("=== Phase 5: Batch Export (Depot Shipments, today) ===")
    _run_batch_export(app, main, today=date.today())
    _log("WorldShip batch import + export workflow complete.")
