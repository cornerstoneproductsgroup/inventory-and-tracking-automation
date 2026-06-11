"""Resolve Order Splitter Output CSV for UPS.com batch upload (per retailer lane)."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from automation.pull_orders_config import date_stamp
from automation.ups_batch_config import (
    lane_csv_path_env_key,
    lane_file_label,
    lane_output_basename,
    lane_output_path,
    normalize_ups_lane,
)
from automation.ups_batch_config import lane_csv_dir as _lane_csv_dir


def _log(msg: str) -> None:
    print(f"[ups/csv] {msg}", flush=True)


class UpsCsvSkip(Exception):
    """No today's Output file for this lane — skip without failing the workflow."""

    def __init__(self, lane: str, top_filename: str) -> None:
        self.lane = lane
        self.top_filename = top_filename
        super().__init__(top_filename)


DepotCsvSkip = UpsCsvSkip


def _date_in_filename(name: str, lane: str, order_date: date) -> bool:
    stamp = date_stamp(order_date)
    return stamp in name or lane_output_basename(lane, order_date) == name


def list_output_csvs(lane: str) -> list[Path]:
    key = normalize_ups_lane(lane)
    folder = _lane_csv_dir(key)
    label = lane_file_label(key)
    if not folder.is_dir():
        return []
    patterns = (
        f"{label} * Output.csv",
        f"{label} * Output.CSV",
        f"{label} *.csv",
        f"{label} *.CSV",
    )
    files: list[Path] = []
    for pat in patterns:
        files.extend(folder.glob(pat))
    seen: set[str] = set()
    unique: list[Path] = []
    for p in sorted(files, key=lambda x: x.stat().st_mtime, reverse=True):
        path_key = str(p).lower()
        if path_key in seen:
            continue
        seen.add(path_key)
        unique.append(p)
    return unique


def resolve_upload_csv(
    *,
    lane: str = "depot",
    order_date: date | None = None,
    explicit_path: Path | str | None = None,
) -> Path:
    key = normalize_ups_lane(lane)
    label = lane_file_label(key)

    if explicit_path:
        path = Path(explicit_path)
        if not path.is_file():
            raise FileNotFoundError(f"{label} CSV not found: {path}")
        return path.resolve()

    env_raw = (os.environ.get(lane_csv_path_env_key(key)) or "").strip()
    if env_raw:
        env_path = Path(env_raw)
        if env_path.is_file():
            return env_path.resolve()

    d = order_date or date.today()
    expected = lane_output_path(key, d)
    if expected.is_file():
        _log(f"[{key}] Using today's file: {expected.name}")
        return expected.resolve()

    candidates = list_output_csvs(key)
    if not candidates:
        raise FileNotFoundError(
            f"No {label} CSV files in:\n  {_lane_csv_dir(key)}"
        )

    top = candidates[0]
    top_name = top.name

    if _date_in_filename(top_name, key, d):
        _log(f"[{key}] Using today's file (newest match): {top_name}")
        return top.resolve()

    _log(
        f"[{key}] No {label} file for today ({date_stamp(d)}). "
        f"Newest file in folder: {top_name!r} — skipping UPS batch."
    )
    raise UpsCsvSkip(key, top_name)
