"""Wait for warehouse print PDFs and send them to the correct printers."""

from __future__ import annotations

import os
import time
from datetime import date
from pathlib import Path

from automation.pull_orders_config import _DAILY_VENDOR, date_stamp


def _log(msg: str) -> None:
    print(f"[pull-orders/print] {msg}", flush=True)


def warehouse_pdf_names(d: date | None = None) -> tuple[str, str]:
    stamp = date_stamp(d)
    return f"Warehouse Print {stamp}.pdf", f"Warehouse SOS Tags {stamp}.pdf"


def _wait_interval_s() -> float:
    raw = (os.environ.get("PULL_ORDERS_WAREHOUSE_POLL_S") or "15").strip()
    try:
        return max(5.0, float(raw))
    except ValueError:
        return 15.0


def _wait_timeout_s() -> float:
    raw = (os.environ.get("PULL_ORDERS_WAREHOUSE_WAIT_MINUTES") or "60").strip()
    try:
        return max(60.0, float(raw) * 60.0)
    except ValueError:
        return 60.0 * 60.0


def post_download_settle_seconds() -> float:
    """Fixed wait after all retailer downloads before polling for warehouse PDFs."""
    raw = (os.environ.get("PULL_ORDERS_POST_DOWNLOAD_WAIT_S") or "60").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 60.0


def settle_after_downloads() -> None:
    """Let Order Splitter finish combining today's PDFs into warehouse print files."""
    delay_s = post_download_settle_seconds()
    if delay_s <= 0:
        return
    _log(
        f"All retailer downloads finished; waiting {delay_s:.0f}s "
        "before checking warehouse print files…"
    )
    time.sleep(delay_s)


def _file_stable(path: Path, *, settle_s: float = 2.0) -> bool:
    if not path.is_file():
        return False
    try:
        size_a = path.stat().st_size
        time.sleep(settle_s)
        size_b = path.stat().st_size
        return size_a > 0 and size_a == size_b
    except OSError:
        return False


def wait_for_warehouse_pdfs(
    *,
    order_date: date | None = None,
    daily_dir: Path | None = None,
) -> tuple[Path, Path]:
    """Poll until both warehouse PDFs exist and sizes are stable."""
    folder = daily_dir or _DAILY_VENDOR
    print_name, sos_name = warehouse_pdf_names(order_date)
    print_path = folder / print_name
    sos_path = folder / sos_name
    deadline = time.monotonic() + _wait_timeout_s()
    interval = _wait_interval_s()
    _log(f"Waiting for warehouse PDFs in {folder} …")
    _log(f"  Expect: {print_name}")
    _log(f"  Expect: {sos_name}")
    while time.monotonic() < deadline:
        if _file_stable(print_path) and _file_stable(sos_path):
            _log("Both warehouse PDFs are ready.")
            return print_path, sos_path
        time.sleep(interval)
    missing = []
    if not print_path.is_file():
        missing.append(print_name)
    if not sos_path.is_file():
        missing.append(sos_name)
    raise TimeoutError(
        f"Timed out waiting for warehouse PDFs in {folder}. Missing: {', '.join(missing)}"
    )


def _resolve_printer(env_key: str, fallback_env: str, default_substring: str) -> str:
    direct = (os.environ.get(env_key) or "").strip()
    if direct:
        return direct
    fallback = (os.environ.get(fallback_env) or "").strip()
    if fallback:
        return fallback
    try:
        import win32print

        flags = win32print.PRINTER_ENUM_LOCAL
        if hasattr(win32print, "PRINTER_ENUM_CONNECTIONS"):
            flags |= win32print.PRINTER_ENUM_CONNECTIONS
        printers = win32print.EnumPrinters(flags, None, 2)
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
        needle = default_substring.lower()
        for name in names:
            if needle in name.lower():
                return name
        if names:
            return names[0]
    except Exception:
        pass
    return default_substring


def print_pdf_windows(pdf_path: Path, printer_name: str) -> None:
    """Send a PDF to a Windows printer via the default print verb."""
    import win32api
    import win32print

    if not pdf_path.is_file():
        raise FileNotFoundError(pdf_path)
    old = win32print.GetDefaultPrinter()
    try:
        win32print.SetDefaultPrinter(printer_name)
        win32api.ShellExecute(0, "print", str(pdf_path), None, ".", 0)
    finally:
        try:
            win32print.SetDefaultPrinter(old)
        except Exception:
            pass


def print_warehouse_files(
    *,
    order_date: date | None = None,
    daily_dir: Path | None = None,
    skip_wait: bool = False,
) -> None:
    if skip_wait:
        folder = daily_dir or _DAILY_VENDOR
        print_name, sos_name = warehouse_pdf_names(order_date)
        print_path = folder / print_name
        sos_path = folder / sos_name
        if not print_path.is_file() or not sos_path.is_file():
            raise FileNotFoundError(
                f"Warehouse PDFs not found in {folder} ({print_name}, {sos_name})."
            )
    else:
        print_path, sos_path = wait_for_warehouse_pdfs(order_date=order_date, daily_dir=daily_dir)

    office_printer = _resolve_printer(
        "PULL_ORDERS_WAREHOUSE_PRINT_PRINTER",
        "COMMERCEHUB_EXCEL_PRINTER",
        "3515",
    )
    label_printer = _resolve_printer(
        "PULL_ORDERS_SOS_LABEL_PRINTER",
        "PULL_ORDERS_LABEL_PRINTER",
        "Zebra ZP 450",
    )

    _log(f"Printing {print_path.name} on {office_printer!r}")
    print_pdf_windows(print_path, office_printer)
    time.sleep(2.0)
    _log(f"Printing {sos_path.name} on {label_printer!r}")
    print_pdf_windows(sos_path, label_printer)
    _log("Warehouse print jobs submitted.")
