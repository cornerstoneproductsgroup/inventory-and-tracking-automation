"""Read CornerstoneMaster order rows for WorldShip label naming."""

from __future__ import annotations

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
        "CornerstoneMaster*.xlsx",
        "CornerstoneMaster*.xlsm",
        "Cornerstone Master*.xlsx",
        "Cornerstone Master*.xlsm",
        "*Master*.xlsx",
        "*Master*.xlsm",
    )
    matches: list[Path] = []
    for pattern in patterns:
        matches.extend(folder.glob(pattern))
    if not matches:
        raise FileNotFoundError(
            f"No CornerstoneMaster workbook found in {folder}. "
            "Expected CornerstoneMaster.xlsx (or similar)."
        )
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


def load_cornerstone_orders(
    *,
    limit: int | None = None,
    master_path: Path | None = None,
) -> list[CornerstoneOrderRow]:
    path = master_path or _find_master_file(_resolve_master_dir())
    sku_col = column_index_from_string(COL_SKU)
    po_col = column_index_from_string(COL_PO)
    retailer_col = column_index_from_string(COL_RETAILER)

    rows: list[CornerstoneOrderRow] = []
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb.active
        for row_idx, row in enumerate(
            ws.iter_rows(min_row=DATA_START_ROW, values_only=True),
            start=DATA_START_ROW,
        ):
            if limit is not None and len(rows) >= limit:
                break
            if not row:
                continue
            sku = _cell_str(row[sku_col - 1] if len(row) >= sku_col else "")
            if not sku:
                break
            po = _cell_str(row[po_col - 1] if len(row) >= po_col else "")
            retailer_raw = _cell_str(row[retailer_col - 1] if len(row) >= retailer_col else "")
            if not po:
                raise ValueError(f"Row {row_idx}: PO column {COL_PO} is empty (SKU={sku!r}).")
            if not retailer_raw:
                raise ValueError(f"Row {row_idx}: retailer column {COL_RETAILER} is empty.")
            rows.append(
                CornerstoneOrderRow(
                    row_number=row_idx,
                    sku=sku,
                    po=po,
                    retailer_key=retailer_merchant_to_key(retailer_raw),
                    retailer_raw=retailer_raw,
                )
            )
    finally:
        wb.close()

    if not rows:
        raise ValueError(f"No order rows found in {path} starting at row {DATA_START_ROW}.")
    _log(f"Loaded {len(rows)} CornerstoneMaster row(s) from {path.name}.")
    return rows
