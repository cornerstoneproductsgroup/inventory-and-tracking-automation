"""CommerceHub packing slip (PDF) and order file (CSV) downloads for pull-orders."""

from __future__ import annotations

import csv
import tempfile
from datetime import date
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

from automation.pull_orders_config import (
    COMMERCEHUB_HOME_URL,
    COMMERCEHUB_ORDER_FILES_URL,
    COMMERCEHUB_PACKSLIPS_URL,
    RETAILERS,
    csv_filename,
    merchant_column_to_key,
    partner_text_to_key,
    pdf_filename,
)

CH_COMMERCEHUB_KEYS = ("depot", "lowes", "thdso")
DOWNLOAD_TIMEOUT_MS = 120_000


def _log(msg: str) -> None:
    print(f"[pull-orders/ch] {msg}", flush=True)


def _goto_packslips(page: Page) -> None:
    page.goto(COMMERCEHUB_PACKSLIPS_URL, wait_until="domcontentloaded", timeout=120_000)
    page.wait_for_timeout(800)
    page.locator("text=/packing slip/i").first.wait_for(state="visible", timeout=60_000)


def _goto_order_files(page: Page) -> None:
    page.goto(COMMERCEHUB_ORDER_FILES_URL, wait_until="domcontentloaded", timeout=120_000)
    page.wait_for_timeout(800)
    page.locator("text=/order file/i").first.wait_for(state="visible", timeout=60_000)


def _row_partner_key(row_text: str) -> str | None:
    lines = [ln.strip() for ln in (row_text or "").splitlines() if ln.strip()]
    for line in lines:
        key = partner_text_to_key(line)
        if key:
            return key
    return partner_text_to_key(row_text)


def _download_buttons_in_row(row) -> list:
    selectors = (
        "span.download-dialog-info__element:has-text('Download')",
        "span.ch-icon-export.download-dialog-info__element",
        "a:has-text('Download')",
        "span:has-text('Download')",
    )
    out = []
    for sel in selectors:
        loc = row.locator(sel)
        n = loc.count()
        for i in range(n):
            btn = loc.nth(i)
            try:
                if btn.is_visible():
                    out.append(btn)
            except Exception:
                continue
        if out:
            return out
    return out


def _iter_retailer_rows(page: Page):
    rows = page.locator("table tbody tr, table tr")
    count = rows.count()
    seen_keys: set[str] = set()
    for i in range(count):
        row = rows.nth(i)
        try:
            if not row.is_visible():
                continue
        except Exception:
            continue
        try:
            text = row.inner_text()
        except Exception:
            continue
        key = _row_partner_key(text)
        if not key or key not in CH_COMMERCEHUB_KEYS or key in seen_keys:
            continue
        buttons = _download_buttons_in_row(row)
        if not buttons:
            continue
        seen_keys.add(key)
        yield key, buttons[0]


def _save_download(download, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    download.save_as(str(dest))
    return dest


def pull_commercehub_packing_slips(page: Page, *, order_date: date | None = None) -> list[Path]:
    """Download Depot / Lowe's / Special Order packing slip PDFs."""
    _log("Opening packing slips page…")
    _goto_packslips(page)
    saved: list[Path] = []
    for key, download_btn in _iter_retailer_rows(page):
        cfg = RETAILERS[key]
        dest = cfg.pdf_dir / pdf_filename(cfg.label, order_date)
        _log(f"Downloading packing slip for {cfg.label} → {dest}")
        try:
            with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as dl_info:
                download_btn.click()
            download = dl_info.value
            path = _save_download(download, dest)
            saved.append(path)
            _log(f"Saved {path.name}")
        except PlaywrightTimeout:
            _log(f"WARN: no download started for {cfg.label}; skipping.")
        page.wait_for_timeout(500)
    if not saved:
        _log("WARN: no CommerceHub packing slips were downloaded.")
    return saved


def _neworders_to_csv(src: Path) -> Path:
    suffix = src.suffix.lower()
    if suffix == ".csv":
        return src
    if suffix != ".neworders":
        raise ValueError(f"Expected .neworders or .csv, got {src.name!r}")
    dest = src.with_suffix(".csv")
    src.rename(dest)
    if dest.suffix.lower() != ".csv" or not dest.is_file():
        raise RuntimeError(f"Failed to rename {src.name} to .csv")
    return dest


def _read_csv_merchant_key(path: Path) -> str | None:
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        col_idx = 21
        if header:
            for i, h in enumerate(header):
                if (h or "").strip().upper() == "MERCHANT_ID":
                    col_idx = i
                    break
        for row in reader:
            if len(row) <= col_idx:
                continue
            merchant = (row[col_idx] or "").strip()
            if merchant:
                key = merchant_column_to_key(merchant)
                if key:
                    return key
    return None


def pull_commercehub_order_csvs(page: Page, *, order_date: date | None = None) -> list[Path]:
    """Download Depot / Lowe's / Special Order CSV order files (.neworders → .csv)."""
    _log("Opening order files page…")
    _goto_order_files(page)
    saved: list[Path] = []
    for key, download_btn in _iter_retailer_rows(page):
        cfg = RETAILERS[key]
        _log(f"Downloading order CSV for {cfg.label}")
        try:
            with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as dl_info:
                download_btn.click()
            download = dl_info.value
            with tempfile.TemporaryDirectory() as tmp:
                raw_name = download.suggested_filename or "order.neworders"
                raw_path = Path(tmp) / raw_name
                download.save_as(str(raw_path))
                csv_path = _neworders_to_csv(raw_path)
                detected = _read_csv_merchant_key(csv_path)
                if detected and detected != key:
                    _log(
                        f"WARN: row partner={key!r} but CSV merchant maps to {detected!r}; "
                        f"using table partner {key!r}."
                    )
                elif cfg.csv_merchant_id:
                    with csv_path.open(newline="", encoding="utf-8-sig", errors="replace") as f:
                        sample = f.read(4096)
                    if cfg.csv_merchant_id not in sample.lower():
                        _log(
                            f"WARN: expected merchant {cfg.csv_merchant_id!r} "
                            f"not found in {csv_path.name}; saving anyway."
                        )
                dest = cfg.csv_dir / csv_filename(cfg.label, order_date)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(csv_path.read_bytes())
                saved.append(dest)
                _log(f"Saved {dest.name}")
        except PlaywrightTimeout:
            _log(f"WARN: no download started for {cfg.label} CSV; skipping.")
        except Exception as exc:
            _log(f"WARN: {cfg.label} CSV failed: {exc}")
        page.wait_for_timeout(500)
    if not saved:
        _log("WARN: no CommerceHub order CSVs were downloaded.")
    return saved


def pull_commercehub_all(page: Page, *, order_date: date | None = None) -> tuple[list[Path], list[Path]]:
    """Packing slips first, then order CSV files."""
    pdfs = pull_commercehub_packing_slips(page, order_date=order_date)
    page.goto(COMMERCEHUB_HOME_URL, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(400)
    csvs = pull_commercehub_order_csvs(page, order_date=order_date)
    return pdfs, csvs
