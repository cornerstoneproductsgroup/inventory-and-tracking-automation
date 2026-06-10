"""Resolve Home Depot Order Splitter Output CSV for UPS.com batch upload."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from automation.pull_orders_config import date_stamp
from automation.ups_batch_config import (
    DEPOT_CSV_OUTPUT_DIR,
    depot_output_basename,
    depot_output_path,
)


def _log(msg: str) -> None:
    print(f"[ups/csv] {msg}", flush=True)


class DepotCsvSkip(Exception):
    """No today's Depot Output file — skip UPS batch without failing the workflow."""

    def __init__(self, top_filename: str) -> None:
        self.top_filename = top_filename
        super().__init__(top_filename)


def _date_in_filename(name: str, order_date: date) -> bool:
    stamp = date_stamp(order_date)
    return stamp in name or depot_output_basename(order_date) == name


def list_output_csvs() -> list[Path]:
    folder = DEPOT_CSV_OUTPUT_DIR
    if not folder.is_dir():
        return []
    patterns = ("Depot * Output.csv", "Depot * Output.CSV", "Depot *.csv", "Depot *.CSV")
    files: list[Path] = []
    for pat in patterns:
        files.extend(folder.glob(pat))
    seen: set[str] = set()
    unique: list[Path] = []
    for p in sorted(files, key=lambda x: x.stat().st_mtime, reverse=True):
        key = str(p).lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(p)
    return unique


def resolve_upload_csv(
    *,
    order_date: date | None = None,
    explicit_path: Path | str | None = None,
) -> Path:
    if explicit_path:
        path = Path(explicit_path)
        if not path.is_file():
            raise FileNotFoundError(f"Depot CSV not found: {path}")
        return path.resolve()

    env_raw = (os.environ.get("UPS_DEPOT_CSV_PATH") or "").strip()
    if env_raw:
        env_path = Path(env_raw)
        if env_path.is_file():
            return env_path.resolve()

    d = order_date or date.today()
    expected = depot_output_path(d)
    if expected.is_file():
        _log(f"Using today's file: {expected.name}")
        return expected.resolve()

    candidates = list_output_csvs()
    if not candidates:
        raise FileNotFoundError(
            f"No Depot CSV files in:\n  {DEPOT_CSV_OUTPUT_DIR}"
        )

    top = candidates[0]
    top_name = top.name

    if _date_in_filename(top_name, d):
        _log(f"Using today's file (newest match): {top_name}")
        return top.resolve()

    _log(
        f"No Depot file for today ({date_stamp(d)}). "
        f"Newest file in folder: {top_name!r} — skipping UPS batch."
    )
    raise DepotCsvSkip(top_name)
