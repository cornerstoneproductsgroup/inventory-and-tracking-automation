"""Resolve Lowe's Order Splitter Output CSV for FedEx batch upload."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from automation.fedex_batch_config import (
    LOWES_CSV_OUTPUT_DIR,
    lowes_output_basename,
    lowes_output_path,
)
from automation.fedex_upload_state import last_used_note, mark_file_used, was_file_used
from automation.pull_orders_config import date_stamp


def _log(msg: str) -> None:
    print(f"[fedex/csv] {msg}", flush=True)


class LowesCsvSkip(Exception):
    """No today's Lowe's Output file — skip FedEx batch without failing the workflow."""

    def __init__(self, top_filename: str) -> None:
        self.top_filename = top_filename
        super().__init__(top_filename)


def _date_in_filename(name: str, order_date: date) -> bool:
    stamp = date_stamp(order_date)
    return stamp in name or lowes_output_basename(order_date) == name


def list_output_csvs() -> list[Path]:
    folder = LOWES_CSV_OUTPUT_DIR
    if not folder.is_dir():
        return []
    files = list(folder.glob("Lowe's * Output.csv")) + list(folder.glob("Lowe's * Output.CSV"))
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def resolve_upload_csv(
    *,
    order_date: date | None = None,
    explicit_path: Path | str | None = None,
) -> Path:
    """
    Pick today's Lowe's Output.csv from Order Splitter.

    - If explicit path or FEDEX_LOWES_CSV_PATH set, use that.
    - Else require the newest file to contain today's date in the name.
    - If the newest file is older, log it; skip when that filename was already used.
    """
    if explicit_path:
        path = Path(explicit_path)
        if not path.is_file():
            raise FileNotFoundError(f"Lowe's CSV not found: {path}")
        return path.resolve()

    env_raw = (os.environ.get("FEDEX_LOWES_CSV_PATH") or "").strip()
    if env_raw:
        env_path = Path(env_raw)
        if env_path.is_file():
            return env_path.resolve()

    d = order_date or date.today()
    expected = lowes_output_path(d)
    if expected.is_file():
        _log(f"Using today's file: {expected.name}")
        return expected

    candidates = list_output_csvs()
    if not candidates:
        raise FileNotFoundError(
            f"No Lowe's Output.csv files in:\n  {LOWES_CSV_OUTPUT_DIR}"
        )

    top = candidates[0]
    top_name = top.name

    if _date_in_filename(top_name, d):
        _log(f"Using today's file (newest match): {top_name}")
        return top.resolve()

    used_before = was_file_used(top_name)
    used_at = last_used_note(top_name) if used_before else None
    _log(
        f"No Lowe's file for today ({date_stamp(d)}). "
        f"Newest file in folder: {top_name!r}."
    )
    if used_before:
        _log(f"  That file was already uploaded on a previous run ({used_at}). Skipping FedEx batch.")
    else:
        _log("  Not today's date — skipping FedEx batch (Lowe's may have had no orders).")
    raise LowesCsvSkip(top_name)
