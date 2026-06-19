"""CLI: UPS WorldShip Batch Export only (Depot Shipments tracking CSV)."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

_HERE = Path(__file__).resolve().parent
_ENV_FILE = _HERE / ".env"


def _load_env() -> None:
    load_dotenv(_ENV_FILE)


def _ensure_pywinauto() -> None:
    try:
        import pywinauto  # noqa: F401
    except ImportError:
        print("[worldship] pywinauto not installed — installing now…", flush=True)
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "pywinauto>=0.6.8"],
        )
        import pywinauto  # noqa: F401


def main() -> int:
    os.chdir(_HERE)
    if str(_HERE) not in sys.path:
        sys.path.insert(0, str(_HERE))
    _load_env()

    try:
        _ensure_pywinauto()
    except subprocess.CalledProcessError as exc:
        print(
            f"[worldship] ERROR: could not install pywinauto (exit {exc.returncode}). "
            "Run Inventory Submissions\\Install-Deps.bat manually.",
            flush=True,
        )
        return 1

    from automation.worldship_after_print import run_worldship_batch_export

    try:
        run_worldship_batch_export()
    except Exception as exc:
        print(f"[worldship] ERROR: {exc}", flush=True)
        return 1

    print("[worldship] Batch export completed successfully.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
