"""UPS WorldShip desktop automation — Batch Import wizard (start phase)."""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

RECORD_COUNT_RE = re.compile(
    r"There are\s+(\d+)\s+record\(s\)\s+to be imported",
    re.IGNORECASE,
)
IMPORT_PATH_RE = re.compile(
    r"Importing from\s+(.+?)(?:\s+There are|\Z)",
    re.IGNORECASE | re.DOTALL,
)

WORLDSHIP_TITLE_RE = r".*UPS WorldShip.*"
PREVIEW_DIALOG_TITLE = "Import/Export Preview"
AUTO_PROCESS_LABEL = "Process shipments automatically after import"


def _looks_like_worldship_label(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    if "worldship" in t:
        return True
    return "ups" in t and "ship" in t


def _pinned_taskbar_dirs() -> list[Path]:
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        return []
    base = Path(appdata) / r"Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar"
    dirs = [base, base / "removed"]
    return [d for d in dirs if d.is_dir()]


def _resolve_lnk(path: Path) -> tuple[str, str]:
    import win32com.client

    shell = win32com.client.Dispatch("WScript.Shell")
    sc = shell.CreateShortCut(str(path))
    target = (sc.Targetpath or "").strip()
    workdir = (sc.WorkingDirectory or "").strip()
    return target, workdir


def _find_exe_from_pinned_shortcuts() -> Path | None:
    for folder in _pinned_taskbar_dirs():
        for lnk in folder.glob("*.lnk"):
            try:
                target, _ = _resolve_lnk(lnk)
            except Exception:
                continue
            hay = f"{lnk.stem} {target}".lower()
            if not _looks_like_worldship_label(hay):
                continue
            p = Path(target)
            if p.is_file():
                _log(f"Found WorldShip from pinned shortcut: {lnk.name} → {p}")
                return p.resolve()
    return None


def _click_worldship_taskbar() -> bool:
    """Click the WorldShip icon on the Windows taskbar (pinned or running)."""
    from pywinauto import Desktop

    backends = [_uia_backend_only()]
    preferred = (os.environ.get("WORLDSHIP_UI_BACKEND") or "uia").strip() or "uia"
    if preferred not in backends:
        backends.insert(0, preferred)

    for backend in backends:
        try:
            tray = Desktop(backend=backend).window(class_name="Shell_TrayWnd")
            if not tray.exists(timeout=3):
                continue
        except Exception:
            continue

        candidates: list[tuple[str, object]] = []
        try:
            for el in tray.descendants():
                try:
                    text = (el.window_text() or "").strip()
                except Exception:
                    text = ""
                try:
                    name = (el.element_info.name or "").strip()
                except Exception:
                    name = ""
                label = text or name
                if not _looks_like_worldship_label(label):
                    continue
                candidates.append((label, el))
        except Exception as exc:
            _log(f"WARN: taskbar scan skipped ({type(exc).__name__}).")
            continue

        if not candidates:
            continue

        # Prefer the shortest exact-ish match (pinned label vs long window title).
        candidates.sort(key=lambda t: len(t[0]))
        label, btn = candidates[0]
        try:
            btn.click_input()
            _log(f"Clicked taskbar button: {label!r}")
            return True
        except Exception as exc:
            _log(f"WARN: could not click taskbar button {label!r}: {exc}")
    return False


@dataclass(frozen=True)
class WorldShipBatchImportResult:
    record_count: int
    import_source: str | None
    preview_text: str


def _log(msg: str) -> None:
    print(f"[worldship] {msg}", flush=True)


def _startup_timeout_s() -> float:
    """Max time to attach to any WorldShip window after launch."""
    raw = (os.environ.get("WORLDSHIP_STARTUP_TIMEOUT_S") or "360").strip()
    try:
        return max(60.0, float(raw))
    except ValueError:
        return 360.0


def _ok_dialog_timeout_s() -> float:
    """Max wait for the 'database will now be checked' OK dialog after launch."""
    raw = (os.environ.get("WORLDSHIP_OK_DIALOG_TIMEOUT_S") or "90").strip()
    try:
        return max(15.0, float(raw))
    except ValueError:
        return 90.0


def _ready_timeout_s(*, cold_start: bool) -> float:
    """Max wait for Import-Export tab to become clickable."""
    key = "WORLDSHIP_READY_TIMEOUT_S" if cold_start else "WORLDSHIP_WARM_READY_TIMEOUT_S"
    default = "300" if cold_start else "5"
    raw = (os.environ.get(key) or default).strip()
    try:
        return max(3.0 if cold_start else 1.0, float(raw))
    except ValueError:
        return 300.0 if cold_start else 5.0


def _step_wait_s(env_key: str, default: float) -> float:
    raw = (os.environ.get(env_key) or str(default)).strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


STARTUP_OK_TEXT_HINTS = (
    "did not shut down normally",
    "database will now be checked",
)
BLOCKING_DIALOG_TITLE_HINTS = (
    "software update",
    "ups worldship",
)
BLOCKING_DIALOG_TEXT_HINTS = STARTUP_OK_TEXT_HINTS + ("software update",)


def _uia_backend_only() -> str:
    return "uia"


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


def _enum_candidate_dialog_hwnds() -> list[tuple[int, str]]:
    import win32gui

    out: list[tuple[int, str]] = []

    def _cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            title = (win32gui.GetWindowText(hwnd) or "").strip()
            cls = win32gui.GetClassName(hwnd) or ""
            tlow = title.lower()
            if cls == "#32770":
                out.append((hwnd, title))
                return True
            if any(h in tlow for h in BLOCKING_DIALOG_TITLE_HINTS):
                out.append((hwnd, title))
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:
        pass
    return out


def _dialog_blob(hwnd: int, title: str) -> str:
    parts = [title, *_safe_enum_child_text(hwnd)]
    return " ".join(p for p in parts if p).lower()


def _blocking_dialog_kind(hwnd: int, title: str) -> str | None:
    blob = _dialog_blob(hwnd, title)
    tlow = title.lower()
    if "software update" in tlow or "software update" in blob:
        return "Software Update"
    if any(h in blob for h in STARTUP_OK_TEXT_HINTS):
        return "database check"
    if "worldship" in tlow and "remote workstation" not in tlow:
        import win32gui

        try:
            if win32gui.GetDlgItem(hwnd, 1):
                return title or "WorldShip dialog"
        except Exception:
            pass
    return None


def _click_ok_on_dialog_hwnd(hwnd: int, *, label: str) -> bool:
    from pywinauto import Application

    import win32con
    import win32gui

    try:
        app = Application(backend=_uia_backend_only()).connect(handle=hwnd, timeout=4)
        dlg = app.window(handle=hwnd)
        ok = dlg.child_window(title="OK", control_type="Button")
        if ok.exists(timeout=2):
            ok.click_input()
            _log(f"Clicked OK on {label!r}.")
            return True
    except Exception:
        pass

    for dlg_id in (1, 2):
        try:
            ok_btn = win32gui.GetDlgItem(hwnd, dlg_id)
            if not ok_btn:
                continue
            text = (win32gui.GetWindowText(ok_btn) or "").strip().lower()
            if text not in ("ok", "&ok", ""):
                continue
            win32gui.PostMessage(ok_btn, win32con.BM_CLICK, 0, 0)
            _log(f"Clicked OK (Win32) on {label!r}.")
            return True
        except Exception:
            continue
    return False


def _dismiss_blocking_dialogs_once() -> bool:
    clicked = False
    for hwnd, title in _enum_candidate_dialog_hwnds():
        kind = _blocking_dialog_kind(hwnd, title)
        if kind is None:
            continue
        if _click_ok_on_dialog_hwnd(hwnd, label=kind):
            clicked = True
            time.sleep(0.6)
    return clicked


def _blocking_dialog_visible() -> bool:
    for hwnd, title in _enum_candidate_dialog_hwnds():
        if _blocking_dialog_kind(hwnd, title) is not None:
            return True
    return False


def _wait_and_dismiss_startup_dialogs(*, timeout_s: float) -> bool:
    _log(f"Waiting up to {timeout_s:.0f}s for startup dialogs (OK to click)…")
    deadline = time.monotonic() + timeout_s
    clicked_any = False
    while time.monotonic() < deadline:
        if _dismiss_blocking_dialogs_once():
            clicked_any = True
        elif clicked_any and not _blocking_dialog_visible():
            return True
        time.sleep(1.5)
    return clicked_any


def _import_export_tab_ready(app, *, timeout_s: float = 2.0, fast: bool = False) -> bool:
    if _blocking_dialog_visible():
        return False
    win_timeout = 0.4 if fast else min(3.0, timeout_s)
    try:
        win = app.window(title_re=WORLDSHIP_TITLE_RE)
        if not win.exists(timeout=win_timeout):
            return False
        for tab in _matching_controls(
            win, title="Import-Export", control_types=("TabItem", "Button")
        ):
            try:
                if tab.is_enabled() and tab.is_visible():
                    return True
            except Exception:
                continue
        return False
    except Exception:
        return False


def _focus_main_window(win) -> None:
    try:
        if win.is_minimized():
            win.restore()
    except Exception:
        pass
    try:
        win.set_focus()
    except Exception:
        pass


def _wait_until_import_export_ready(app, *, timeout_s: float, poll_interval_s: float = 2.0):
    _log(f"Waiting up to {timeout_s:.0f}s for Import-Export tab to be ready…")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        _dismiss_blocking_dialogs_once()
        if not _blocking_dialog_visible() and _import_export_tab_ready(app, fast=False):
            win = app.window(title_re=WORLDSHIP_TITLE_RE)
            _focus_main_window(win)
            _log("Import-Export tab is ready (no blocking dialogs).")
            return win
        time.sleep(poll_interval_s)
    raise TimeoutError(
        f"WorldShip Import-Export tab did not become ready within {timeout_s:.0f}s. "
        "A dialog (Software Update, database check, etc.) may still be open, or the app "
        "is still loading — increase WORLDSHIP_READY_TIMEOUT_S."
    )


def _resolve_main_window(app, *, cold_start: bool):
    """Return the main WorldShip window; skip long waits when already loaded."""
    _dismiss_blocking_dialogs_once()
    if _import_export_tab_ready(app, fast=True):
        win = app.window(title_re=WORLDSHIP_TITLE_RE)
        _focus_main_window(win)
        if not cold_start:
            _log("WorldShip already open — proceeding immediately.")
        else:
            _log("Import-Export tab is ready (no blocking dialogs).")
        return win
    if not cold_start:
        _log("WorldShip is open but Import-Export is not ready yet — brief wait…")
    return _wait_until_import_export_ready(
        app,
        timeout_s=_ready_timeout_s(cold_start=cold_start),
        poll_interval_s=2.0 if cold_start else 0.5,
    )


def _default_exe_candidates() -> list[Path]:
    env = (os.environ.get("WORLDSHIP_EXE") or "").strip()
    out: list[Path] = []
    if env:
        out.append(Path(env))
    pinned = _find_exe_from_pinned_shortcuts()
    if pinned is not None:
        out.append(pinned)
    for p in (
        Path(r"C:\Program Files (x86)\UPS\WorldShip\WorldShip.exe"),
        Path(r"C:\Program Files\UPS\WorldShip\WorldShip.exe"),
        Path(r"C:\UPS\WorldShip\WorldShip.exe"),
    ):
        out.append(p)
    # De-dupe while preserving order.
    seen: set[str] = set()
    unique: list[Path] = []
    for p in out:
        key = str(p).lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(p)
    return unique


def _resolve_worldship_exe() -> Path | None:
    for cand in _default_exe_candidates():
        try:
            if cand.is_file():
                return cand.resolve()
        except OSError:
            continue
    return None


def _launch_worldship_exe(exe: Path) -> None:
    _log(f"Launching WorldShip from {exe}")
    subprocess.Popen([str(exe)], cwd=str(exe.parent) if exe.parent.is_dir() else None)


def _require_pywinauto():
    try:
        from pywinauto import Application
        from pywinauto.findwindows import ElementNotFoundError

        return Application, ElementNotFoundError
    except ImportError as exc:
        raise RuntimeError(
            "pywinauto is required for WorldShip automation. "
            'Install: pip install "pywinauto>=0.6.8"'
        ) from exc


def _connect_or_start(app_factory, *, startup_timeout_s: float) -> tuple[object, bool]:
    """
    Return (app, cold_start).

    cold_start is True when WorldShip was launched this run and we must wait for
    startup OK + full load before using the ribbon.
    """
    Application, ElementNotFoundError = _require_pywinauto()
    backend = (os.environ.get("WORLDSHIP_UI_BACKEND") or "uia").strip() or "uia"

    def _connect():
        return app_factory(backend=backend).connect(title_re=WORLDSHIP_TITLE_RE, timeout=8)

    try:
        _log("Connecting to running WorldShip window…")
        app = _connect()
        if _import_export_tab_ready(app, fast=True):
            _log("Connected — WorldShip is already loaded.")
            return app, False
        _log("WorldShip is open but still loading (Import-Export not ready yet).")
        cold = True
    except Exception:
        app = None
        cold = True

    if app is None:
        _log("WorldShip not running — clicking taskbar icon…")
        launched = _click_worldship_taskbar()
        if not launched:
            exe = _resolve_worldship_exe()
            if exe is not None:
                _launch_worldship_exe(exe)
                launched = True
            else:
                raise FileNotFoundError(
                    "Could not start WorldShip.\n"
                    "  Pin UPS WorldShip to the taskbar (recommended — works on both PCs), or\n"
                    "  set WORLDSHIP_EXE in Inventory Submissions\\.env (see .env.example)."
                )
        if not launched:
            raise FileNotFoundError("Could not launch WorldShip.")

        deadline = time.monotonic() + startup_timeout_s
        last_err: Exception | None = None
        while time.monotonic() < deadline:
            _dismiss_blocking_dialogs_once()
            try:
                app = _connect()
                _log("Attached to WorldShip window (loading may still be in progress).")
                break
            except Exception as exc:
                last_err = exc
                time.sleep(2.0)
        else:
            raise TimeoutError(
                f"WorldShip did not appear within {startup_timeout_s:.0f}s. {last_err}"
            )
        cold = True

    if cold:
        _wait_and_dismiss_startup_dialogs(timeout_s=_ok_dialog_timeout_s())

    return app, cold


def _matching_controls(
    win,
    *,
    title: str | None = None,
    title_re: str | None = None,
    control_types: tuple[str, ...],
    max_index: int = 3,
):
    """Yield controls when WorldShip exposes duplicate UIA nodes for one ribbon item."""
    exist_ms = 30
    for ctrl in control_types:
        for i in range(max_index):
            try:
                kwargs: dict = {"control_type": ctrl, "found_index": i}
                if title is not None:
                    kwargs["title"] = title
                if title_re is not None:
                    kwargs["title_re"] = title_re
                target = win.child_window(**kwargs)
                if not target.exists(timeout=exist_ms / 1000.0):
                    break
                yield target
            except Exception:
                break


_RIBBON_POLL_S = 0.03


def _click_when_ready(
    win,
    *,
    title: str,
    control_types: tuple[str, ...] = ("Button", "TabItem"),
    timeout_s: float = 3.0,
) -> None:
    """Poll quickly and click the first visible, enabled match."""
    deadline = time.monotonic() + timeout_s
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        clicked = False
        for target in _matching_controls(win, title=title, control_types=control_types):
            try:
                if not target.is_visible():
                    continue
                if not target.is_enabled():
                    continue
                target.click_input()
                clicked = True
                break
            except Exception as exc:
                last_err = exc
        if clicked:
            return
        time.sleep(_RIBBON_POLL_S)
    raise RuntimeError(f"Could not click {title!r}: {last_err}")


def _wait_for_batch_import_wizard(app, main, *, timeout_s: float = 8.0):
    """Return the wizard host as soon as the auto-process checkbox appears."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for title_re in (".*Batch Import.*", ".*Import.*Export.*"):
            try:
                cand = app.window(title_re=title_re)
                if not cand.exists(timeout=0.03):
                    continue
                for box in _matching_controls(
                    cand, title=AUTO_PROCESS_LABEL, control_types=("CheckBox",)
                ):
                    if box.is_visible():
                        return cand
            except Exception:
                continue
        for box in _matching_controls(
            main, title=AUTO_PROCESS_LABEL, control_types=("CheckBox",)
        ):
            try:
                if box.is_visible():
                    return main
            except Exception:
                continue
        time.sleep(_RIBBON_POLL_S)
    return main


def _ensure_checkbox_checked(dlg, label: str, *, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        for target in _matching_controls(
            dlg, title=label, control_types=("CheckBox", "RadioButton")
        ):
            try:
                if not target.is_visible():
                    continue
                try:
                    state = target.get_toggle_state()
                    if state != 1:
                        target.click_input()
                except Exception:
                    target.click_input()
                return
            except Exception as exc:
                last_err = exc
        time.sleep(_RIBBON_POLL_S)
    raise RuntimeError(f"Could not set checkbox {label!r}: {last_err}")


def _click_button(dlg, title: str, *, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        for target in _matching_controls(dlg, title=title, control_types=("Button",)):
            try:
                if not target.is_visible():
                    continue
                if not target.is_enabled():
                    continue
                target.click_input()
                return
            except Exception as exc:
                last_err = exc
        time.sleep(_RIBBON_POLL_S)
    raise RuntimeError(f"Could not click button {title!r}: {last_err}")


def _find_dialog(app, title: str, *, timeout_s: float = 90):
    dlg = app.window(title=title)
    dlg.wait("visible", timeout=int(timeout_s))
    try:
        dlg.set_focus()
    except Exception:
        pass
    return dlg


def _read_preview_text(preview) -> str:
    chunks: list[str] = []
    for ctrl in ("Edit", "Document", "Text", "Pane"):
        try:
            for child in preview.descendants(control_type=ctrl):
                try:
                    t = (child.window_text() or "").strip()
                except Exception:
                    t = ""
                if t and t not in chunks:
                    chunks.append(t)
        except Exception:
            continue
    if not chunks:
        try:
            t = (preview.window_text() or "").strip()
            if t:
                chunks.append(t)
        except Exception:
            pass
    return "\n".join(chunks)


def _parse_preview(preview_text: str) -> tuple[int, str | None]:
    m_count = RECORD_COUNT_RE.search(preview_text)
    if not m_count:
        raise RuntimeError(
            "Import/Export Preview did not show a record count. Text was:\n"
            f"{preview_text[:500]}"
        )
    m_path = IMPORT_PATH_RE.search(preview_text)
    source = m_path.group(1).strip() if m_path else None
    return int(m_count.group(1)), source


def run_worldship_batch_import_start() -> WorldShipBatchImportResult:
    """
    WorldShip: Import-Export → Batch Import → auto-process checkbox → Next →
    Import/Export Preview (read count) → Next.
    """
    Application, _ = _require_pywinauto()
    startup_timeout_s = _startup_timeout_s()

    app, cold_start = _connect_or_start(Application, startup_timeout_s=startup_timeout_s)
    main = _resolve_main_window(app, cold_start=cold_start)

    _log("Clicking Import-Export tab…")
    tab_clicked_at = time.monotonic()
    _click_when_ready(main, title="Import-Export", control_types=("TabItem", "Button"), timeout_s=3)

    _log("Clicking Batch Import…")
    _click_when_ready(
        main,
        title="Batch Import",
        control_types=("Button", "MenuItem", "SplitButton"),
        timeout_s=4,
    )

    _log("Waiting for Batch Import wizard…")
    wizard = _wait_for_batch_import_wizard(app, main, timeout_s=8)

    before_checkbox_s = _step_wait_s("WORLDSHIP_BEFORE_CHECKBOX_WAIT_S", 2.0)
    elapsed_since_tab = time.monotonic() - tab_clicked_at
    remaining = before_checkbox_s - elapsed_since_tab
    if remaining > 0:
        _log(f"Waiting {remaining:.1f}s before auto-process checkbox…")
        time.sleep(remaining)

    _log(f"Ensuring {AUTO_PROCESS_LABEL!r} is checked…")
    _ensure_checkbox_checked(wizard, AUTO_PROCESS_LABEL, timeout_s=5)

    before_next_s = _step_wait_s("WORLDSHIP_BEFORE_NEXT_WAIT_S", 1.0)
    if before_next_s > 0:
        _log(f"Waiting {before_next_s:.1f}s before Next…")
        time.sleep(before_next_s)

    _log("Clicking Next (wizard step 1)…")
    _click_button(wizard, "Next", timeout_s=5)

    _log(f"Waiting for {PREVIEW_DIALOG_TITLE!r}…")
    preview = _find_dialog(app, PREVIEW_DIALOG_TITLE, timeout_s=120)
    preview_text = _read_preview_text(preview)
    record_count, import_source = _parse_preview(preview)
    if import_source:
        _log(f"Import source: {import_source}")
    _log(f"There are {record_count} record(s) to be imported.")
    if record_count == 0:
        _log("WARN: zero records — continuing with Next as configured.")

    _log("Clicking Next (Import/Export Preview)…")
    _click_button(preview, "Next")

    return WorldShipBatchImportResult(
        record_count=record_count,
        import_source=import_source,
        preview_text=preview_text,
    )
