"""Read CornerstoneMaster order rows for WorldShip label naming."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.utils import column_index_from_string

from automation.worldship_label_config import (
    COL_PO,
    COL_RETAILER,
    COL_SKU,
    CORNERSTONE_MASTER_DIR,
    CORNERSTONE_MASTER_DIR_FALLBACK,
    DATA_START_ROW,
    retailer_merchant_to_key,
)


def _log(msg: str) -> None:
    print(f"[worldship] {msg}", flush=True)


@dataclass(frozen=True)
class CornerstoneOrderRow:
    row_number: int
    sku: str
    po: str
    retailer_key: str
    retailer_raw: str


def _resolve_master_dir() -> Path:
    for cand in (CORNERSTONE_MASTER_DIR, CORNERSTONE_MASTER_DIR_FALLBACK):
        try:
            if cand.is_dir():
                return cand
        except OSError:
            continue
    raise FileNotFoundError(
        f"CornerstoneMaster folder not found. Checked:\n"
        f"  {CORNERSTONE_MASTER_DIR}\n"
        f"  {CORNERSTONE_MASTER_DIR_FALLBACK}\n"
        "Set WORLDSHIP_CORNERSTONE_MASTER_DIR in Inventory Submissions\\.env"
    )


def _find_master_file(folder: Path) -> Path:
    patterns = (
        "CornerstoneMaster.csv",
        "CornerstoneMaster*.csv",
        "CornerstoneMaster*.xlsx",
        "CornerstoneMaster*.xlsm",
        "Cornerstone Master*.csv",
        "Cornerstone Master*.xlsx",
    )
    matches: list[Path] = []
    for pattern in patterns:
        matches.extend(folder.glob(pattern))
    if not matches:
        raise FileNotFoundError(
            f"No CornerstoneMaster file found in {folder}.\n"
            "  Expected CornerstoneMaster.csv or CornerstoneMaster.xlsx"
        )
    exact_csv = folder / "CornerstoneMaster.csv"
    if exact_csv in matches:
        chosen = exact_csv.resolve()
    else:
        matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        chosen = matches[0].resolve()
    _log(f"Using CornerstoneMaster file: {chosen}")
    return chosen


def _cell_str(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _norm_header(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")


def _header_index(header: list[str], hints: tuple[str, ...]) -> int | None:
    for i, raw in enumerate(header):
        norm = _norm_header(raw)
        for hint in hints:
            h = _norm_header(hint)
            if norm == h or h in norm or norm in h:
                return i
    return None


def _column_indices(header: list[str]) -> tuple[int, int, int]:
    """SKU, PO, retailer column indices (0-based). Prefer CSV header names."""
    sku_i = _header_index(header, ("sku",))
    po_i = _header_index(
        header,
        ("purchase_order_number", "purchase_order", "po_number", "po", "po#"),
    )
    retailer_i = _header_index(
        header,
        ("merchant_id", "merchant", "retailer", "merchantid"),
    )
    if sku_i is not None and po_i is not None and retailer_i is not None:
        return sku_i, po_i, retailer_i
    return (
        column_index_from_string(COL_SKU) - 1,
        column_index_from_string(COL_PO) - 1,
        column_index_from_string(COL_RETAILER) - 1,
    )


def _append_order_row(
    rows: list[CornerstoneOrderRow],
    *,
    row_idx: int,
    row: tuple | list,
    sku_i: int,
    po_i: int,
    retailer_i: int,
) -> bool:
    """Append one order; return False when data rows are exhausted."""
    if not row:
        return False
    sku = _cell_str(row[sku_i] if len(row) > sku_i else "")
    if not sku:
        return False
    po = _cell_str(row[po_i] if len(row) > po_i else "")
    retailer_raw = _cell_str(row[retailer_i] if len(row) > retailer_i else "")
    if not po:
        raise ValueError(f"Row {row_idx}: PO is empty (SKU={sku!r}).")
    if not retailer_raw:
        raise ValueError(f"Row {row_idx}: retailer/merchant is empty.")
    rows.append(
        CornerstoneOrderRow(
            row_number=row_idx,
            sku=sku,
            po=po,
            retailer_key=retailer_merchant_to_key(retailer_raw),
            retailer_raw=retailer_raw,
        )
    )
    return True


def _load_csv(path: Path, *, limit: int | None) -> list[CornerstoneOrderRow]:
    rows: list[CornerstoneOrderRow] = []
    for enc in ("utf-8-sig", "latin1"):
        try:
            with path.open(newline="", encoding=enc) as fh:
                reader = csv.reader(fh)
                all_rows = list(reader)
            break
        except UnicodeDecodeError:
            all_rows = None
    if not all_rows:
        raise ValueError(f"Could not read CSV: {path}")

    header = [str(c).strip() for c in all_rows[0]]
    sku_i, po_i, retailer_i = _column_indices(header)
    data_rows = all_rows[DATA_START_ROW - 1 :]
    for offset, row in enumerate(data_rows, start=DATA_START_ROW):
        if limit is not None and len(rows) >= limit:
            break
        if not _append_order_row(
            rows,
            row_idx=offset,
            row=row,
            sku_i=sku_i,
            po_i=po_i,
            retailer_i=retailer_i,
        ):
            break
    return rows


def _load_xlsx(path: Path, *, limit: int | None) -> list[CornerstoneOrderRow]:
    rows: list[CornerstoneOrderRow] = []
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb.active
        first_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), None)
        header = [str(c or "").strip() for c in (first_row or ())]
        if header and _header_index(header, ("sku",)) is not None:
            sku_i, po_i, retailer_i = _column_indices(header)
            start_row = DATA_START_ROW
        else:
            sku_i, po_i, retailer_i = _column_indices([])
            start_row = DATA_START_ROW

        for row_idx, row in enumerate(
            ws.iter_rows(min_row=start_row, values_only=True),
            start=start_row,
        ):
            if limit is not None and len(rows) >= limit:
                break
            if not _append_order_row(
                rows,
                row_idx=row_idx,
                row=row,
                sku_i=sku_i,
                po_i=po_i,
                retailer_i=retailer_i,
            ):
                break
    finally:
        wb.close()
    return rows


def load_cornerstone_orders(
    *,
    limit: int | None = None,
    master_path: Path | None = None,
) -> list[CornerstoneOrderRow]:
    path = master_path or _find_master_file(_resolve_master_dir())
    suffix = path.suffix.lower()
    if suffix == ".csv":
        rows = _load_csv(path, limit=limit)
    elif suffix in (".xlsx", ".xlsm"):
        rows = _load_xlsx(path, limit=limit)
    else:
        raise ValueError(f"Unsupported CornerstoneMaster format: {path}")

    if not rows:
        raise ValueError(f"No order rows found in {path} starting at row {DATA_START_ROW}.")
    _log(f"Loaded {len(rows)} CornerstoneMaster row(s) from {path.name}.")
    return rows
