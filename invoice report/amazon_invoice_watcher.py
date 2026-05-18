"""
Dedicated Amazon share watcher (separate from Run Full Workflow and CommerceHub invoice export).

Polls ``AMAZON_INVOICE_INPUT_DIR`` for new raw exports, then runs the Amazon pipeline.
Today: format + print via ``amazon_invoice_postprocess``. Add more steps in ``run_amazon_pipeline``.

Start: ``Run Amazon Invoice Watcher.bat`` (repo root) or ``run_amazon_invoice_watcher.bat`` here.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

_SCRIPT_DIR = Path(__file__).resolve().parent
_ENV_FILE = _SCRIPT_DIR / ".env"


def load_project_dotenv() -> None:
    load_dotenv(_ENV_FILE)


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[amazon-watcher {ts}] {msg}", flush=True)


def run_amazon_pipeline(source: Path) -> None:
    """
    Run all Amazon automation steps for one new raw export.

    Extend this when you add more Amazon steps (e.g. QuickBooks, email, second report).
    """
    from amazon_invoice_postprocess import process_amazon_export

    process_amazon_export(source)


def _watch_interval_s() -> float:
    raw = (os.environ.get("AMAZON_WATCH_INTERVAL_S") or "15").strip()
    try:
        return max(3.0, float(raw))
    except ValueError:
        return 15.0


def _skip_existing_on_start() -> bool:
    """Default true: files already in Input when the watcher starts are not processed."""
    v = (os.environ.get("AMAZON_WATCH_SKIP_EXISTING_ON_START") or "true").strip().lower()
    return v not in ("0", "false", "no", "")


def _process_on_start() -> bool:
    """Opt-in: run the pipeline on one backlog file when the watcher starts (usually off)."""
    v = (os.environ.get("AMAZON_WATCH_PROCESS_ON_START") or "false").strip().lower()
    return v in ("1", "true", "yes")


def run_watcher(
    *,
    interval_s: float | None = None,
    skip_existing_on_start: bool | None = None,
    process_on_start: bool | None = None,
) -> None:
    from amazon_invoice_postprocess import (
        describe_folder_state,
        folder_file_snapshot,
        log_folder_scan,
        mark_existing_input_files_processed,
        pick_newest_unprocessed,
        resolve_amazon_input_dir,
        resolve_amazon_output_dir,
        wait_for_file_stable,
    )

    load_project_dotenv()
    if (os.environ.get("AMAZON_INVOICE_POSTPROCESS") or "true").strip().lower() in (
        "0",
        "false",
        "no",
    ):
        _log("AMAZON_INVOICE_POSTPROCESS is false - watcher exiting.")
        return

    folder = resolve_amazon_input_dir()
    interval = interval_s if interval_s is not None else _watch_interval_s()
    skip_existing = (
        skip_existing_on_start if skip_existing_on_start is not None else _skip_existing_on_start()
    )
    on_start = process_on_start if process_on_start is not None else _process_on_start()

    if not folder.is_dir():
        _log(f"ERROR: Amazon folder not found or not accessible: {folder}")
        sys.exit(1)

    env_dir = (os.environ.get("AMAZON_INVOICE_INPUT_DIR") or "").strip()
    if env_dir:
        _log(f"AMAZON_INVOICE_INPUT_DIR from .env: {env_dir}")
    _log(f"Input (raw):  {folder}")
    _log(f"Output (xlsx): {resolve_amazon_output_dir()}")
    _log(f"Poll every {interval:.0f}s - close this window to stop.")
    _log("Pipeline: format export -> save report -> print (more steps can be added later).")
    log_folder_scan(folder)

    def _process_path(path: Path) -> None:
        _log(f"Waiting for file to finish saving: {path.name}")
        if not wait_for_file_stable(path):
            _log(f"Timed out waiting for stable file: {path.name}")
            return
        run_amazon_pipeline(path)

    if skip_existing:
        n = mark_existing_input_files_processed(folder)
        if n:
            _log(
                f"Startup: skipped {n} file(s) already in Input "
                "(only files saved after this moment will be processed)."
            )
        else:
            _log("Startup: no new files to skip in Input (folder empty or already tracked).")
    elif on_start:
        pending = pick_newest_unprocessed(folder)
        if pending is not None:
            _log(f"Found unprocessed file on start: {pending.name}")
            try:
                _process_path(pending)
                _log(f"Finished: {pending.name}")
            except Exception as e:
                _log(f"ERROR processing {pending.name}: {e}")
                traceback.print_exc()
        else:
            _log(f"On start: {describe_folder_state(folder)}")

    poll_n = 0
    last_snap: dict[str, float] = folder_file_snapshot(folder)
    heartbeat_every = max(1, int(60.0 / interval))  # about once per minute

    while True:
        try:
            snap = folder_file_snapshot(folder)
            if snap != last_snap:
                _log("Folder contents changed - rescanning:")
                log_folder_scan(folder)
                last_snap = snap

            path = pick_newest_unprocessed(folder)
            if path is not None:
                _log(f"New export detected: {path.name}")
                _process_path(path)
                _log(f"Finished: {path.name}")
                last_snap = folder_file_snapshot(folder)
            else:
                poll_n += 1
                if poll_n % heartbeat_every == 0:
                    _log(f"Still watching ({describe_folder_state(folder)})")
        except KeyboardInterrupt:
            _log("Stopped.")
            return
        except Exception as e:
            _log(f"ERROR: {e}")
            traceback.print_exc()
        time.sleep(interval)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Watch the Amazon invoice share for new exports and run the Amazon pipeline.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=None,
        help="Poll interval in seconds (default: AMAZON_WATCH_INTERVAL_S or 15).",
    )
    parser.add_argument(
        "--process-existing-on-start",
        action="store_true",
        help="Process one backlog file in Input on start (default: skip all files already there).",
    )
    parser.add_argument(
        "--no-skip-existing-on-start",
        action="store_true",
        help="Do not mark files already in Input as skipped when the watcher starts.",
    )
    args = parser.parse_args(argv)
    run_watcher(
        interval_s=args.interval,
        skip_existing_on_start=not args.no_skip_existing_on_start,
        process_on_start=args.process_existing_on_start,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
