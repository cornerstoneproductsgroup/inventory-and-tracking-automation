"""
Print workbooks via Excel COM (Windows): Page Layout → Print gridlines; landscape; print header
(file name) and footer (``Page n of m``); chosen printer. Depot, Lowe's, and Tractor Supply use the
same landscape print path (Depot, Lowe's, Tractor, Amazon).

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


def _allow_default_printer_fallback() -> bool:
    """If false (default), never print when the Toshiba/target printer cannot be set."""
    v = (os.environ.get("COMMERCEHUB_EXCEL_PRINT_ALLOW_DEFAULT") or "").strip().lower()
    return v in ("1", "true", "yes")


def _enum_installed_printer_names() -> list[str]:
    try:
        import win32print
    except ImportError:
        return []

    flags = win32print.PRINTER_ENUM_LOCAL
    if hasattr(win32print, "PRINTER_ENUM_CONNECTIONS"):
        flags |= win32print.PRINTER_ENUM_CONNECTIONS
    try:
        printers = win32print.EnumPrinters(flags, None, 2)
    except Exception:
        return []

    names: list[str] = []
    for p in printers:
        if isinstance(p, dict):
            name = (p.get("pPrinterName") or "").strip()
        elif isinstance(p, (list, tuple)) and len(p) > 2:
            name = str(p[2]).strip()
        else:
            continue
        if name:
            names.append(name)
    return names


def _find_toshiba_win32_name(names: list[str]) -> str | None:
    for name in names:
        u = name.upper().replace("/", "\\")
        if "3515" in u and "SVR01A" in u:
            return name
    return None


def _active_printer_candidates() -> list[str]:
    """
  Build ordered candidates for Excel's ``ActivePrinter`` (not always equal to Windows name).

  Set ``COMMERCEHUB_EXCEL_PRINTER`` per PC from Excel VBA: ``?Application.ActivePrinter``.
  """
    seen: set[str] = set()
    out: list[str] = []

    def add(raw: str) -> None:
        s = (raw or "").strip()
        if not s or s in seen:
            return
        seen.add(s)
        out.append(s)

    explicit = (os.environ.get("COMMERCEHUB_EXCEL_PRINTER") or "").strip()
    if explicit:
        add(explicit)

    win32_names = _enum_installed_printer_names()
    toshiba = _find_toshiba_win32_name(win32_names)
    if toshiba:
        add(toshiba)
        # Model name without trailing " on svr01a" / " on Ne04:" (Excel may want either form).
        base = toshiba.rsplit(" on ", 1)[0].strip() if " on " in toshiba else toshiba
        for server in ("svr01a", "SVR01A"):
            add(f"\\\\{server}\\{base}")
            add(f"\\\\{server}\\{base} on {server}")
        if " on Ne" not in toshiba.upper():
            for port in range(0, 16):
                add(f"{base} on Ne{port:02d}:")
                add(f"\\\\SVR01A\\{base} on Ne{port:02d}:")

    return out


def _try_set_excel_active_printer(xl, candidates: list[str]) -> str | None:
    for name in candidates:
        try:
            xl.ActivePrinter = name
            return name
        except Exception:
            continue
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

        candidates = _active_printer_candidates()
        chosen = _try_set_excel_active_printer(xl, candidates) if candidates else None
        if chosen:
            _post_log(f"Excel ActivePrinter set to: {chosen!r}")
        elif _allow_default_printer_fallback():
            _post_log(
                "WARNING: Could not set invoice printer — printing to Windows default. "
                "Set COMMERCEHUB_EXCEL_PRINTER in invoice report\\.env on this PC."
            )
            ws.PrintOut(Collate=True)
            _post_log("Print job sent to Excel (default printer).")
        else:
            installed = _enum_installed_printer_names()
            hint = (
                "Set COMMERCEHUB_EXCEL_PRINTER in invoice report\\.env to the exact string from "
                "Excel on THIS computer (Alt+F11 → Immediate → ?Application.ActivePrinter). "
                "Each PC often differs by ' on Ne04:' port. "
                "To allow default printer anyway: COMMERCEHUB_EXCEL_PRINT_ALLOW_DEFAULT=true"
            )
            raise RuntimeError(
                "Invoice print aborted: could not set Toshiba/target printer for Excel.\n"
                f"  Tried: {candidates!r}\n"
                f"  Installed printers: {installed!r}\n"
                f"  {hint}"
            )
        if chosen:
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
