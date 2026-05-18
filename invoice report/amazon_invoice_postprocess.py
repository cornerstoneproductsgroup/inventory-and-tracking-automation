"""
Post-process Amazon transaction exports from the Cornerstone share: trim header junk,
filter by run-day rules, keep Order rows only, format columns, save, and print (landscape + gridlines).

Drop a new raw .csv/.xlsx into the Amazon **Input** share folder. Formatted output is saved to
**Output** as ``{same base name} Output.xlsx`` (then printed).

Date rules (by run date = today unless overridden):
  Tue–Fri: keep transactions dated yesterday only.
  Monday: keep Friday, Saturday, and Sunday (three calendar days before the run).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment

from depot_invoice_postprocess import (
    _accounting_format,
    _apply_openpyxl_print_file_and_page_footer,
    _save_xlsx_or_fallback,
)

_SCRIPT_DIR = Path(__file__).resolve().parent
_ENV_FILE = _SCRIPT_DIR / ".env"
DEFAULT_AMAZON_BASE_DIR = r"\\rygarcorp.com\shares\Cornerstone\Invoice Reports\Amazon"
DEFAULT_AMAZON_INPUT_DIR = DEFAULT_AMAZON_BASE_DIR + r"\Input"
DEFAULT_AMAZON_OUTPUT_DIR = DEFAULT_AMAZON_BASE_DIR + r"\Output"

# Raw column letters → output A..F (0-based indices into the header row).
_RAW_COL_INDICES: tuple[int, ...] = (0, 2, 3, 4, 6, 13)  # A, C, D, E, G, N

_AMAZON_TXN_RE = re.compile(
    r"^(?P<dt>[A-Za-z]{3}\s+\d{1,2},\s+\d{4}\s+\d{1,2}:\d{2}:\d{2}\s+[AP]M)",
    re.IGNORECASE,
)
_OUTPUT_STEM_SUFFIX = " Output"
_ELIGIBLE_SUFFIXES = {".xlsx", ".xlsm", ".csv"}
_SKIP_NAME_PREFIXES = ("~$", ".")


def load_project_dotenv() -> None:
    load_dotenv(_ENV_FILE)


def resolve_amazon_base_dir() -> Path:
    raw = (os.environ.get("AMAZON_INVOICE_BASE_DIR") or DEFAULT_AMAZON_BASE_DIR).strip()
    return Path(raw).expanduser()


def resolve_amazon_input_dir() -> Path:
    raw = (os.environ.get("AMAZON_INVOICE_INPUT_DIR") or DEFAULT_AMAZON_INPUT_DIR).strip()
    return Path(raw).expanduser()


def resolve_amazon_output_dir() -> Path:
    raw = (os.environ.get("AMAZON_INVOICE_OUTPUT_DIR") or DEFAULT_AMAZON_OUTPUT_DIR).strip()
    return Path(raw).expanduser()


def output_path_for_source(source: Path, output_dir: Path | None = None) -> Path:
    """e.g. ``report.csv`` -> ``report Output.xlsx`` in the Output folder."""
    out_dir = (output_dir or resolve_amazon_output_dir()).resolve()
    return out_dir / f"{source.stem}{_OUTPUT_STEM_SUFFIX}.xlsx"


def transaction_dates_to_keep(run_day: date | None = None) -> set[date]:
    """Tue–Fri: yesterday only. Monday: previous Fri/Sat/Sun."""
    d = run_day or date.today()
    if d.weekday() == 0:
        return {d - timedelta(days=3), d - timedelta(days=2), d - timedelta(days=1)}
    return {d - timedelta(days=1)}


def parse_amazon_transaction_date(value: object) -> date | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    m = _AMAZON_TXN_RE.match(s)
    if not m:
        return None
    try:
        return datetime.strptime(m.group("dt"), "%b %d, %Y %I:%M:%S %p").date()
    except ValueError:
        return None


def _row_width(row: tuple) -> int:
    for i in range(len(row) - 1, -1, -1):
        v = row[i]
        if v is not None and str(v).strip():
            return i + 1
    return 0


def _pad_row(row: tuple, width: int) -> tuple:
    w = max(width, _row_width(row))
    if len(row) >= w:
        return row
    return tuple(row) + (None,) * (w - len(row))


def _load_rows(path: Path) -> list[tuple]:
    suf = path.suffix.lower()
    if suf == ".csv":
        import csv

        rows: list[tuple] = []
        with path.open(newline="", encoding="utf-8-sig", errors="replace") as f:
            for row in csv.reader(f):
                rows.append(tuple(row))
        return rows

    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb.active
        return [tuple(c.value for c in row) for row in ws.iter_rows()]
    finally:
        wb.close()


def find_header_row_index(rows: list[tuple]) -> int:
    """First row with non-empty values in at least two columns."""
    for i, row in enumerate(rows):
        padded = _pad_row(row, 29)
        filled = sum(1 for v in padded if v is not None and str(v).strip())
        if filled >= 2:
            return i
    raise ValueError(f"Could not find header row (≥2 columns with data) in {len(rows)} rows.")


def _is_output_artifact(path: Path) -> bool:
    """Skip formatted workbooks if they were copied into Input by mistake."""
    return path.stem.strip().endswith(_OUTPUT_STEM_SUFFIX)


def _is_skipped_filename(name: str) -> bool:
    n = name.strip()
    if not n:
        return True
    if n.startswith(_SKIP_NAME_PREFIXES):
        return True
    if n.lower().endswith(".tmp"):
        return True
    return False


def _processed_state_path() -> Path:
    raw = (os.environ.get("AMAZON_PROCESSED_STATE_FILE") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return resolve_amazon_input_dir() / ".amazon_invoice_processed.json"


def _eligible_inputs(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    out: list[Path] = []
    for p in folder.iterdir():
        if not p.is_file():
            continue
        if _is_skipped_filename(p.name):
            continue
        if p.suffix.lower() not in _ELIGIBLE_SUFFIXES:
            continue
        if _is_output_artifact(p):
            continue
        out.append(p)
    return sorted(out, key=lambda p: p.stat().st_mtime, reverse=True)


def wait_for_file_stable(path: Path, *, settle_s: float = 2.0, timeout_s: float = 120.0) -> bool:
    """Wait until size/mtime stop changing (file finished saving from browser/Excel)."""
    deadline = time.monotonic() + timeout_s
    last_size = -1
    last_mtime = -1.0
    stable_since: float | None = None
    while time.monotonic() < deadline:
        try:
            st = path.stat()
        except OSError:
            time.sleep(0.5)
            continue
        if st.st_size == last_size and st.st_mtime == last_mtime:
            if stable_since is None:
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= settle_s:
                return True
        else:
            last_size = st.st_size
            last_mtime = st.st_mtime
            stable_since = None
        time.sleep(0.5)
    return False


def _load_processed_state() -> dict[str, float]:
    state_path = _processed_state_path()
    if not state_path.is_file():
        return {}
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
        return {str(k): float(v) for k, v in raw.items()}
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}


def _save_processed_state(state: dict[str, float]) -> None:
    state_path = _processed_state_path()
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, indent=0), encoding="utf-8")
    except OSError:
        pass


def _mark_processed(source: Path) -> None:
    key = str(source.resolve())
    state = _load_processed_state()
    state[key] = source.stat().st_mtime
    _save_processed_state(state)


def _already_processed(source: Path) -> bool:
    key = str(source.resolve())
    state = _load_processed_state()
    try:
        return state.get(key) == source.stat().st_mtime
    except OSError:
        return False


def mark_existing_input_files_processed(folder: Path | None = None) -> int:
    """
    Mark every eligible raw file already in Input as processed (no format/print).

    Used when the watcher starts so restarts only pick up files saved after startup.
    """
    folder = folder or resolve_amazon_input_dir()
    marked = 0
    for path in _eligible_inputs(folder):
        if _already_processed(path):
            continue
        _mark_processed(path)
        marked += 1
    return marked


def _select_columns(header: tuple, data_rows: list[tuple]) -> tuple[list[str], list[tuple]]:
    width = max(29, max(_row_width(header), max((_row_width(r) for r in data_rows), default=0)))
    header = _pad_row(header, width)
    headers_out: list[str] = []
    for idx in _RAW_COL_INDICES:
        if idx >= len(header):
            headers_out.append("")
        else:
            headers_out.append("" if header[idx] is None else str(header[idx]).strip())

    rows_out: list[tuple] = []
    for row in data_rows:
        row = _pad_row(row, width)
        if not any(v is not None and str(v).strip() for v in row):
            continue
        picked = tuple(row[i] if i < len(row) else None for i in _RAW_COL_INDICES)
        rows_out.append(picked)
    return headers_out, rows_out


def _type_column_index(headers: list[str]) -> int:
    for i, h in enumerate(headers):
        if "order" in h.lower() and "refund" not in h.lower():
            return i
        if h.strip().lower() in ("type", "transaction type"):
            return i
    return 1  # raw column C → output B


def _filter_rows(
    headers: list[str],
    rows: list[tuple],
    *,
    keep_dates: set[date],
) -> list[tuple]:
    type_ix = _type_column_index(headers)
    kept: list[tuple] = []
    for row in rows:
        tx_date = parse_amazon_transaction_date(row[0] if row else None)
        if tx_date is None or tx_date not in keep_dates:
            continue
        type_val = "" if type_ix >= len(row) or row[type_ix] is None else str(row[type_ix]).strip()
        if type_val.lower() == "refund":
            continue
        kept.append(row)
    return kept


def _coerce_accounting_cell_value(value: object) -> float | str | None:
    """Parse currency/amount text from CSV so column F can use Excel Accounting format."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s or s.lower() in ("nan", "none"):
        return None
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1].strip()
    s = s.replace(",", "").replace("$", "").strip()
    if not s:
        return None
    try:
        n = float(s)
        return -n if negative else n
    except ValueError:
        return str(value).strip()


def write_amazon_workbook(headers: list[str], rows: list[tuple], out_path: Path) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "Amazon"
    _COL_AMOUNT = 6  # column F (raw N)

    for c, name in enumerate(headers, start=1):
        ws.cell(row=1, column=c, value=name)

    for r, row in enumerate(rows, start=2):
        for c, val in enumerate(row, start=1):
            if c == _COL_AMOUNT:
                val = _coerce_accounting_cell_value(val)
            ws.cell(row=r, column=c, value=val)

    acct = _accounting_format()
    center = Alignment(horizontal="center", vertical="center")
    ws.column_dimensions["A"].width = 31.50
    ws.column_dimensions["B"].width = 10.0
    ws.column_dimensions["C"].width = 25.0
    ws.column_dimensions["D"].width = 18.50
    ws.column_dimensions["E"].width = 8.0
    ws.column_dimensions["F"].width = 13.30

    last_row = max(1, 1 + len(rows))
    for r in range(1, last_row + 1):
        ws.cell(row=r, column=2).alignment = center
        ws.cell(row=r, column=5).alignment = center
    for r in range(2, last_row + 1):
        ws.cell(row=r, column=_COL_AMOUNT).number_format = acct

    _apply_openpyxl_print_file_and_page_footer(ws)

    return _save_xlsx_or_fallback(wb, out_path)


def process_amazon_export(
    source: Path,
    *,
    run_day: date | None = None,
    output_dir: Path | None = None,
    print_workbook: bool = True,
) -> Path:
    load_project_dotenv()
    run_day = run_day or date.today()
    keep_dates = transaction_dates_to_keep(run_day)
    source = source.resolve()

    rows = _load_rows(source)
    header_i = find_header_row_index(rows)
    header = rows[header_i]
    body = rows[header_i + 1 :]

    headers, selected = _select_columns(header, body)
    filtered = _filter_rows(headers, selected, keep_dates=keep_dates)

    out_dir = (output_dir or resolve_amazon_output_dir()).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_path_for_source(source, out_dir)

    saved = write_amazon_workbook(headers, filtered, out_path)
    date_list = ", ".join(sorted(d.isoformat() for d in keep_dates))
    print(
        f"[amazon] {source.name}: kept {len(filtered)} Order row(s) for {date_list} -> {saved}",
        flush=True,
    )

    if not filtered:
        print(
            f"[amazon] WARNING: No rows matched the date filter ({date_list}). "
            "Report saved with headers only; print skipped. "
            "Check transaction dates in column A or run on the correct weekday.",
            flush=True,
        )
        print_workbook = False

    if print_workbook:
        try:
            from depot_excel_print import print_landscape_with_gridlines

            print_landscape_with_gridlines(saved)
            print(f"[amazon] Print sent for {saved.name}", flush=True)
        except Exception as e:
            import traceback

            print(f"[amazon] Excel print failed (workbook saved): {e}", flush=True)
            traceback.print_exc()

    _mark_processed(source)
    return saved


def _skip_reason(path: Path) -> str:
    if _is_skipped_filename(path.name):
        return "ignored (temp/hidden filename)"
    if path.suffix.lower() not in _ELIGIBLE_SUFFIXES:
        return f"ignored (extension {path.suffix!r} - need .xlsx, .xlsm, or .csv)"
    if _is_output_artifact(path):
        return "ignored (formatted Output workbook — belongs in Output folder)"
    if _already_processed(path):
        return (
            "already processed (delete "
            f"{_processed_state_path()} or save under a new name)"
        )
    return "eligible"


def describe_folder_state(folder: Path | None = None) -> str:
    """Human-readable summary for logs when nothing runs."""
    folder = folder or resolve_amazon_input_dir()
    if not folder.is_dir():
        return f"folder missing: {folder}"
    eligible = _eligible_inputs(folder)
    if not eligible:
        names = [p.name for p in folder.iterdir() if p.is_file()][:12]
        return f"no raw .xlsx/.csv in {folder}" + (f" (files seen: {names})" if names else " (empty)")
    newest = eligible[0]
    reason = _skip_reason(newest)
    if reason != "eligible":
        return f"newest file {newest.name}: {reason}"
    return f"ready: {newest.name}"


def folder_file_snapshot(folder: Path) -> dict[str, float]:
    """All files in folder (any extension) -> mtime, for change detection."""
    snap: dict[str, float] = {}
    if not folder.is_dir():
        return snap
    for p in folder.iterdir():
        if not p.is_file():
            continue
        try:
            snap[p.name] = p.stat().st_mtime
        except OSError:
            continue
    return snap


def log_folder_scan(folder: Path) -> None:
    """Print one line per file so logs show what the watcher actually sees."""
    folder = folder.resolve()
    print(f"[amazon] Folder scan: {folder}", flush=True)
    if not folder.is_dir():
        print("[amazon]   (folder not accessible)", flush=True)
        return
    files = sorted(folder.iterdir(), key=lambda p: p.name.lower())
    file_items = [p for p in files if p.is_file()]
    if not file_items:
        print("[amazon]   (no files in folder)", flush=True)
        return
    for p in file_items:
        print(f"[amazon]   {p.name}: {_skip_reason(p)}", flush=True)


def pick_newest_unprocessed(folder: Path | None = None) -> Path | None:
    folder = folder or resolve_amazon_input_dir()
    for p in _eligible_inputs(folder):
        if not _already_processed(p):
            return p
    return None


def process_newest_in_folder(folder: Path | None = None, *, force: bool = False) -> Path | None:
    folder = folder or resolve_amazon_input_dir()
    if force:
        candidates = _eligible_inputs(folder)
        if not candidates:
            return None
        return process_amazon_export(candidates[0])
    path = pick_newest_unprocessed(folder)
    if path is None:
        return None
    return process_amazon_export(path)


def watch_folder(
    folder: Path | None = None,
    *,
    interval_s: float = 30.0,
) -> None:
    folder = folder or resolve_amazon_input_dir()
    print(f"[amazon] Watching {folder} every {interval_s:.0f}s for new exports…", flush=True)
    while True:
        try:
            path = pick_newest_unprocessed(folder)
            if path is not None:
                process_amazon_export(path)
        except Exception as e:
            print(f"[amazon] ERROR: {e}", flush=True)
        time.sleep(interval_s)


def main(argv: list[str] | None = None) -> int:
    load_project_dotenv()
    parser = argparse.ArgumentParser(description="Format and print Amazon invoice exports from the share.")
    parser.add_argument(
        "path",
        nargs="?",
        help="Raw Amazon export (.xlsx/.csv). If omitted, process newest unprocessed file in the Amazon folder.",
    )
    parser.add_argument("--watch", action="store_true", help="Poll the Amazon folder for new files.")
    parser.add_argument("--force", action="store_true", help="Re-process newest raw file even if already done.")
    parser.add_argument("--interval", type=float, default=30.0, help="Watch poll interval in seconds.")
    parser.add_argument("--no-print", action="store_true", help="Save only; skip Excel print.")
    args = parser.parse_args(argv)

    if (os.environ.get("AMAZON_INVOICE_POSTPROCESS") or "true").strip().lower() in ("0", "false", "no"):
        print("[amazon] Skipped (AMAZON_INVOICE_POSTPROCESS is false).", flush=True)
        return 0

    if args.watch:
        watch_folder(interval_s=args.interval)
        return 0

    if args.path:
        process_amazon_export(
            Path(args.path),
            print_workbook=not args.no_print,
        )
        return 0

    result = process_newest_in_folder(force=args.force)
    if result is None:
        folder = resolve_amazon_input_dir()
        print(f"[amazon] No new raw export to process in {folder}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
