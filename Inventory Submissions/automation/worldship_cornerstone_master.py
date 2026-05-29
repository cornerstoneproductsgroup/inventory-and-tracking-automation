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

SKU_HEADER_HINTS = ("sku",)
PO_HEADER_HINTS = (
    "purchase_order_number",
    "purchase_order",
    "purchaseordernumber",
    "po_number",
    "ponumber",
    "customer_po",
)
RETAILER_HEADER_HINTS = (
    "merchant_id",
    "merchantid",
    "merchant",
    "retailer",
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
    """Match column by exact header name first; avoid short hints like 'po' in SHPTO_*."""
    normalized = [(i, _norm_header(raw)) for i, raw in enumerate(header)]
    for hint in hints:
        h = _norm_header(hint)
        for i, norm in normalized:
            if norm == h:
                return i
    for hint in hints:
        h = _norm_header(hint)
        if len(h) < 5:
            continue
        for i, norm in normalized:
            if h in norm or norm in h:
                return i
    return None


def _column_indices(header: list[str]) -> tuple[int, int, int]:
    """SKU, PO, retailer column indices (0-based). Prefer named CSV headers."""
    sku_i = _header_index(header, SKU_HEADER_HINTS)
    po_i = _header_index(header, PO_HEADER_HINTS)
    retailer_i = _header_index(header, RETAILER_HEADER_HINTS)
    if sku_i is not None and po_i is not None and retailer_i is not None:
        return sku_i, po_i, retailer_i
    _log(
        "WARN: CornerstoneMaster header names not recognized — "
        f"using Excel columns {COL_SKU}/{COL_PO}/{COL_RETAILER}."
    )
    return (
        column_index_from_string(COL_SKU) - 1,
        column_index_from_string(COL_PO) - 1,
        column_index_from_string(COL_RETAILER) - 1,
    )


def _find_header_row_index(all_rows: list[list[str]]) -> int:
    """Locate the header row (WorldShip CSV may use row 1)."""
    for idx, row in enumerate(all_rows[:15]):
        header = [str(c).strip() for c in row]
        if not any(header):
            continue
        if _header_index(header, SKU_HEADER_HINTS) is None:
            continue
        if _header_index(header, PO_HEADER_HINTS) is None:
            continue
        if _header_index(header, RETAILER_HEADER_HINTS) is None:
            continue
        return idx
    return 0


def _read_csv_rows(path: Path) -> list[list[str]]:
    raw = path.read_bytes()
    text: str | None = None
    for enc in ("utf-8-sig", "utf-8", "latin1", "cp1252"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError(f"Could not decode CSV: {path}")

    delimiter = ","
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",\t;|")
        delimiter = dialect.delimiter
    except csv.Error:
        if text.count("\t") > text.count(","):
            delimiter = "\t"

    return list(csv.reader(text.splitlines(), delimiter=delimiter))


def _append_order_row(
    rows: list[CornerstoneOrderRow],
    *,
    row_idx: int,
    row: tuple | list,
    sku_i: int,
    po_i: int,
    retailer_i: int,
) -> str:
    """
    Append one order row.

    Returns 'added', 'skip' (blank line), or 'stop' (end of data).
    """
    if not row or not any(_cell_str(c) for c in row):
        return "stop"
    sku = _cell_str(row[sku_i] if len(row) > sku_i else "")
    if not sku:
        return "skip"
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
    return "added"


def _load_csv(path: Path, *, limit: int | None) -> list[CornerstoneOrderRow]:
    all_rows = _read_csv_rows(path)
    if not all_rows:
        raise ValueError(f"CSV is empty: {path}")

    header_idx = _find_header_row_index(all_rows)
    header = [str(c).strip() for c in all_rows[header_idx]]
    sku_i, po_i, retailer_i = _column_indices(header)
    _log(
        "CornerstoneMaster columns: "
        f"SKU={header[sku_i]!r} (col {sku_i + 1}), "
        f"PO={header[po_i]!r} (col {po_i + 1}), "
        f"retailer={header[retailer_i]!r} (col {retailer_i + 1})"
    )

    rows: list[CornerstoneOrderRow] = []
    data_start = max(header_idx + 1, DATA_START_ROW - 1)
    for offset, row in enumerate(all_rows[data_start:], start=data_start + 1):
        if limit is not None and len(rows) >= limit:
            break
        result = _append_order_row(
            rows,
            row_idx=offset,
            row=row,
            sku_i=sku_i,
            po_i=po_i,
            retailer_i=retailer_i,
        )
        if result == "stop":
            break
    return rows


def _load_xlsx(path: Path, *, limit: int | None) -> list[CornerstoneOrderRow]:
    rows: list[CornerstoneOrderRow] = []
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb.active
        first_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), None)
        header = [str(c or "").strip() for c in (first_row or ())]
        if _header_index(header, SKU_HEADER_HINTS) is not None:
            sku_i, po_i, retailer_i = _column_indices(header)
            start_row = 2
        else:
            sku_i, po_i, retailer_i = _column_indices([])
            start_row = DATA_START_ROW

        for row_idx, row in enumerate(
            ws.iter_rows(min_row=start_row, values_only=True),
            start=start_row,
        ):
            if limit is not None and len(rows) >= limit:
                break
            result = _append_order_row(
                rows,
                row_idx=row_idx,
                row=row,
                sku_i=sku_i,
                po_i=po_i,
                retailer_i=retailer_i,
            )
            if result == "stop":
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
        raise ValueError(
            f"No order rows found in {path}. "
            "Expected headers SKU, PURCHASE_ORDER_NUMBER, and MERCHANT_ID with data below."
        )
    _log(f"Loaded {len(rows)} CornerstoneMaster row(s) from {path.name}.")
    return rows
