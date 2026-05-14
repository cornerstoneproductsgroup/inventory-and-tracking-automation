"""
Print workbooks via Excel COM (Windows): Page Layout → Print gridlines; landscape; print header
(file name) and footer (``Page n of m``); chosen printer. Depot, Lowe's, and Tractor Supply use the
same landscape print path.

Requires Microsoft Excel and: pip install pywin32
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _post_log(msg: str) -> None:
    print(msg, flush=True)


def _excel_print_enabled() -> bool:
    v = (os.environ.get("COMMERCEHUB_EXCEL_PRINT") or "true").strip().lower()
    return v not in ("0", "false", "no", "")


def _resolve_active_printer_string() -> str | None:
    """
    Excel's ActivePrinter must match the machine's installed name (often includes ' on Ne00:').
    Set COMMERCEHUB_EXCEL_PRINTER to the exact string (run in Excel VBA: ?Application.ActivePrinter).
    Otherwise we try to find a printer whose name contains 3515 and SVR01A.
    """
    explicit = (os.environ.get("COMMERCEHUB_EXCEL_PRINTER") or "").strip()
    if explicit:
        return explicit
    try:
        import win32print
    except ImportError:
        return None

    flags = win32print.PRINTER_ENUM_LOCAL
    if hasattr(win32print, "PRINTER_ENUM_CONNECTIONS"):
        flags |= win32print.PRINTER_ENUM_CONNECTIONS
    try:
        printers = win32print.EnumPrinters(flags, None, 2)
    except Exception:
        return None

    for p in printers:
        if isinstance(p, dict):
            name = (p.get("pPrinterName") or "").strip()
        elif isinstance(p, (list, tuple)) and len(p) > 2:
            name = str(p[2]).strip()
        else:
            continue
        if not name:
            continue
        u = name.upper().replace("/", "\\")
        if "3515" in u and "SVR01A" in u:
            return name
    return None


# Excel PageSetup.Orientation: 2 = xlLandscape
_XL_LANDSCAPE = 2


def _print_workbook_with_gridlines(
    workbook_path: Path,
    *,
    orientation: int,
    save_changes: bool,
) -> None:
    """
    Page Layout → Sheet Options → Gridlines → Print; File → Print orientation; ``PrintOut``.
    """
    if not _excel_print_enabled():
        _post_log("Excel print skipped (COMMERCEHUB_EXCEL_PRINT is false).")
        return

    from dotenv import load_dotenv

    load_dotenv()

    try:
        import pythoncom
        import win32com.client
    except ImportError as e:
        raise RuntimeError(
            "Excel printing needs pywin32. Install with: pip install pywin32"
        ) from e

    path = str(workbook_path.resolve())
    if not Path(path).is_file():
        raise FileNotFoundError(path)

    co_init = False
    xl = None
    wb = None
    try:
        pythoncom.CoInitialize()
        co_init = True
        xl = win32com.client.DispatchEx("Excel.Application")
        xl.Visible = False
        xl.DisplayAlerts = False
        wb = xl.Workbooks.Open(path, ReadOnly=False)
        ws = wb.Worksheets(1)

        ps = ws.PageSetup
        # Page Layout → Sheet Options → Gridlines → Print (not "View").
        ps.PrintGridlines = True
        ps.Orientation = orientation
        # Print header/footer: file name (&F) + "Page n of m" on every sheet (Depot/Lowe's/Tractor).
        ps.CenterHeader = "&F"
        ps.CenterFooter = "Page &P of &N"

        printer = _resolve_active_printer_string()
        if printer:
            try:
                xl.ActivePrinter = printer
                _post_log(f"Excel ActivePrinter set to: {printer!r}")
            except Exception as ex:
                _post_log(
                    f"Could not set ActivePrinter to {printer!r} ({ex}); "
                    "set COMMERCEHUB_EXCEL_PRINTER to the exact value from Excel VBA "
                    "(?Application.ActivePrinter). Printing with current default."
                )

        ws.PrintOut(Collate=True)
        _post_log("Print job sent to Excel.")

        wb.Close(SaveChanges=save_changes)
        wb = None
    finally:
        if wb is not None:
            try:
                wb.Close(SaveChanges=False)
            except Exception:
                pass
        if xl is not None:
            try:
                xl.Quit()
            except Exception:
                pass
        if co_init:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass


def print_landscape_with_gridlines(workbook_path: Path) -> None:
    """
    Page Layout equivalent: Print gridlines; landscape; print header (file name) and footer
    (Page n of m); print to COMMERCEHUB_EXCEL_PRINTER if set. Used for Depot, Lowe's, and Tractor.
    """
    _print_workbook_with_gridlines(
        workbook_path, orientation=_XL_LANDSCAPE, save_changes=True
    )


def main(argv: list[str]) -> int:
    """Manual test: py -3 depot_excel_print.py path.xlsx"""
    if len(argv) < 2:
        print("Usage: depot_excel_print.py <workbook.xlsx>", file=sys.stderr)
        return 2
    print_landscape_with_gridlines(Path(argv[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
