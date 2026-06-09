"""Wait for warehouse print PDFs and send them to the correct printers."""

from __future__ import annotations

import os
import re
import time
from datetime import date
from pathlib import Path

from automation.pull_orders_config import _DAILY_VENDOR, date_stamp


def _log(msg: str) -> None:
    print(f"[pull-orders/print] {msg}", flush=True)


_DATE_IN_WAREHOUSE_NAME = re.compile(r"\d{1,2}-\d{1,2}-\d{4}")


def warehouse_pdf_names(d: date | None = None) -> tuple[str, str]:
    stamp = date_stamp(d)
    return f"Warehouse Print {stamp}.pdf", f"Warehouse SOS Tags {stamp}.pdf"


def warehouse_pdf_has_expected_date(path: Path, order_date: date | None = None) -> bool:
    """True when the PDF filename contains only the expected order date stamp."""
    expected = date_stamp(order_date)
    if expected not in path.name:
        return False
    for found in _DATE_IN_WAREHOUSE_NAME.findall(path.name):
        if found != expected:
            return False
    return True


def _validate_warehouse_pdf_for_print(path: Path, order_date: date | None = None) -> None:
    """Refuse to print warehouse PDFs that are not named for the target order date."""
    expected = date_stamp(order_date)
    if not path.is_file():
        raise FileNotFoundError(f"Warehouse PDF not found: {path}")
    if not warehouse_pdf_has_expected_date(path, order_date):
        other_dates = [
            d for d in _DATE_IN_WAREHOUSE_NAME.findall(path.name) if d != expected
        ]
        if other_dates:
            raise ValueError(
                f"Refusing to print {path.name}: filename date {other_dates[0]!r} "
                f"does not match expected {expected!r}."
            )
        raise ValueError(
            f"Refusing to print {path.name}: expected today's date {expected!r} in filename."
        )


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
            _validate_warehouse_pdf_for_print(print_path, order_date)
            _validate_warehouse_pdf_for_print(sos_path, order_date)
            _log("Both warehouse PDFs are ready for today's date.")
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


_VIRTUAL_PRINTER_HINTS = (
    "onenote",
    "microsoft print to pdf",
    "print to pdf",
    "microsoft xps",
    "xps document writer",
    "fax",
    "send to",
    "redirected",
    "adobe pdf",
    "foxit",
    "cutepdf",
    "bullzip",
    "pdfcreator",
    "pdfsam",
    "abs pdf",
    "pdf driver",
    "pdf printer",
)


def _normalize_printer_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _is_zebra_label_printer(name: str) -> bool:
    norm = _normalize_printer_text(name)
    if "zebra" not in norm:
        return False
    return any(token in norm for token in ("zp 450", "zp450", "zp-450", "zdesigner"))


def _printer_name_matches(name: str, needle: str) -> bool:
    """Match printer names ignoring extra spaces (Windows often uses double spaces)."""
    norm_name = _normalize_printer_text(name)
    norm_needle = _normalize_printer_text(needle)
    if not norm_needle:
        return False
    if norm_needle in norm_name:
        return True
    if _is_zebra_label_printer(norm_needle) and _is_zebra_label_printer(norm_name):
        return True
    return False


def _find_installed_printer(
    target: str,
    *,
    physical: list[str] | None = None,
) -> str | None:
    """Resolve *target* to an installed printer name (exact, then normalized/fuzzy)."""
    needle = (target or "").strip()
    if not needle:
        return None
    names = physical if physical is not None else list_installed_printers(include_virtual=False)
    if _printer_exists(needle):
        return needle
    for name in names:
        if _normalize_printer_text(name) == _normalize_printer_text(needle):
            return name
    for name in names:
        if _printer_name_matches(name, needle):
            return name
    return None


def _is_virtual_printer(name: str) -> bool:
    low = (name or "").strip().lower()
    return any(hint in low for hint in _VIRTUAL_PRINTER_HINTS)


def list_installed_printers(*, include_virtual: bool = True) -> list[str]:
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
        if include_virtual:
            return names
        return [name for name in names if not _is_virtual_printer(name)]
    except Exception:
        return []


def _printer_exists(printer_name: str) -> bool:
    try:
        import win32print

        handle = win32print.OpenPrinter(printer_name)
        win32print.ClosePrinter(handle)
        return True
    except Exception:
        return False


def _resolve_printer(env_key: str, fallback_env: str, default_substring: str) -> str:
    physical = list_installed_printers(include_virtual=False)

    def _resolve_configured(value: str, *, label: str) -> str | None:
        if not value:
            return None
        if _is_virtual_printer(value):
            raise RuntimeError(
                f"{label}={value!r} is a virtual printer. "
                f"Set {env_key} to your physical Zebra name in .env."
            )
        resolved = _find_installed_printer(value, physical=physical)
        if resolved:
            return resolved
        raise RuntimeError(
            f"{label}={value!r} is not installed on this PC. "
            f"Check Windows Printers or fix .env."
        )

    direct = (os.environ.get(env_key) or "").strip()
    if direct:
        return _resolve_configured(direct, label=env_key)

    fallback = (os.environ.get(fallback_env) or "").strip()
    if fallback:
        return _resolve_configured(fallback, label=fallback_env)

    needles = [default_substring]
    if not _is_zebra_label_printer(default_substring):
        needles.append("zebra")
    for needle in needles:
        for name in physical:
            if _printer_name_matches(name, needle):
                return name
    if _is_zebra_label_printer(default_substring) or "zebra" in default_substring.lower():
        for name in physical:
            if _is_zebra_label_printer(name):
                return name

    installed = list_installed_printers(include_virtual=True)
    physical_hint = ", ".join(physical[:10]) or "(no physical printers found)"
    installed_hint = ", ".join(installed[:12]) or "(none)"
    raise RuntimeError(
        f"No physical printer matching {default_substring!r} found. "
        f"Set {env_key} in Inventory Submissions\\.env to the exact Windows printer name "
        f"(e.g. Zebra ZP 450). Physical printers: {physical_hint}. "
        f"All printers: {installed_hint}"
    )


def print_pdf_windows(pdf_path: Path, printer_name: str) -> None:
    """Send a PDF to a Windows printer via the default print verb."""
    import win32api
    import win32print

    if not pdf_path.is_file():
        raise FileNotFoundError(pdf_path)
    if _is_virtual_printer(printer_name):
        raise RuntimeError(f"Refusing to print to virtual printer {printer_name!r}")
    if not _printer_exists(printer_name):
        raise RuntimeError(f"Printer not found: {printer_name!r}")
    old = win32print.GetDefaultPrinter()
    try:
        win32print.SetDefaultPrinter(printer_name)
        result = win32api.ShellExecute(0, "print", str(pdf_path), None, ".", 0)
        if int(result) <= 32:
            raise RuntimeError(
                f"Windows could not queue print job for {pdf_path.name} "
                f"on {printer_name!r} (ShellExecute={result})"
            )
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
    target_date = order_date or date.today()
    if skip_wait:
        folder = daily_dir or _DAILY_VENDOR
        print_name, sos_name = warehouse_pdf_names(target_date)
        print_path = folder / print_name
        sos_path = folder / sos_name
        _validate_warehouse_pdf_for_print(print_path, target_date)
        _validate_warehouse_pdf_for_print(sos_path, target_date)
    else:
        print_path, sos_path = wait_for_warehouse_pdfs(
            order_date=target_date, daily_dir=daily_dir
        )

    _validate_warehouse_pdf_for_print(print_path, target_date)
    _validate_warehouse_pdf_for_print(sos_path, target_date)

    _log(
        f"Printing warehouse PDFs for {date_stamp(target_date)}: "
        f"{print_path.name}, {sos_path.name}"
    )

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
