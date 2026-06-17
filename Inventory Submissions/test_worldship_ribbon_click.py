"""Dry-run: connect to WorldShip and try Import-Export + Batch Import ribbon clicks only."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from automation.worldship_batch_import import (  # noqa: E402
    _connect_or_start,
    _focus_main_window,
    _log,
    _require_pywinauto,
    _resolve_main_window,
)
from automation.worldship_ribbon_click import (  # noqa: E402
    click_batch_import,
    ensure_import_export_tab,
    foreground_window_title,
    ribbon_action_available,
)


def main() -> int:
    from automation.worldship_ribbon_click import _RIBBON_VERSION

    _log(f"=== WorldShip ribbon test ({_RIBBON_VERSION}) ===")
    Application, _ = _require_pywinauto()
    app, cold = _connect_or_start(Application, startup_timeout_s=120.0)
    main_win = _resolve_main_window(app, cold_start=cold)
    _log(f"Main window title: {main_win.window_text()!r}")
    _log(f"Foreground before focus: {foreground_window_title()!r}")
    _focus_main_window(main_win)
    _log(f"Foreground after focus: {foreground_window_title()!r}")
    _log(
        f"Batch Import visible before: "
        f"{ribbon_action_available(main_win, 'Batch Import', ('Button', 'MenuItem', 'SplitButton'))}"
    )
    ensure_import_export_tab(main_win, log=_log)
    _log(
        f"Batch Import visible after tab: "
        f"{ribbon_action_available(main_win, 'Batch Import', ('Button', 'MenuItem', 'SplitButton'))}"
    )
    click_batch_import(main_win, log=_log, app=app)
    _log("Ribbon click test finished — check WorldShip for Batch Import wizard.")
    return 0


if __name__ == "__main__":
    import traceback

    try:
        exit_code = main()
    except Exception as exc:
        print(f"[worldship] FATAL: {exc}", flush=True)
        traceback.print_exc()
        exit_code = 1
    raise SystemExit(exit_code)
