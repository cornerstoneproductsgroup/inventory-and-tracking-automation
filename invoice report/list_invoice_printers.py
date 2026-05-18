"""List Windows printers and the Excel ActivePrinter string to put in invoice report\\.env."""
from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from depot_excel_print import (  # noqa: E402
    _active_printer_candidates,
    _enum_installed_printer_names,
    _try_set_excel_active_printer,
)


def main() -> int:
    print("Windows installed printers:")
    for name in _enum_installed_printer_names():
        print(f"  - {name}")

    print("\nExcel ActivePrinter candidates (invoice report will try in order):")
    candidates = _active_printer_candidates()
    for c in candidates:
        print(f"  - {c!r}")

    try:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        xl = win32com.client.DispatchEx("Excel.Application")
        xl.Visible = False
        chosen = _try_set_excel_active_printer(xl, candidates)
        if chosen:
            print(f"\nExcel accepted: {chosen!r}")
            print("\nPut this in invoice report\\.env:")
            print(f"COMMERCEHUB_EXCEL_PRINTER={chosen}")
        else:
            print("\nExcel did not accept any candidate. On this PC, open Excel, select the Toshiba,")
            print("then VBA Immediate window: ?Application.ActivePrinter")
        xl.Quit()
        pythoncom.CoUninitialize()
    except Exception as e:
        print(f"\nCould not probe Excel: {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
