"""CommerceHub packing slip (PDF) and order file (CSV) downloads for pull-orders."""

from __future__ import annotations

import csv
import re
import tempfile
from datetime import date
from pathlib import Path

from playwright.sync_api import Frame, Page, TimeoutError as PlaywrightTimeout

from automation.pull_orders_config import (
    COMMERCEHUB_HOME_URL,
    COMMERCEHUB_ORDER_FILES_URL,
    COMMERCEHUB_PACKSLIPS_URL,
    RETAILERS,
    csv_filename,
    merchant_column_to_key,
    pdf_filename,
)

# Exact CommerceHub control (see packing slips / order files table).
DOWNLOAD_SPAN_SELECTOR = (
    "span.download-dialog-info__element:has-text('Download'), "
    "span.ch-icon-export.download-dialog-info__element:has-text('Download'), "
    "span.chub-chui-chicon.ch-icon-export.download-dialog-info__element"
)

# thdso before depot so "Home Depot Special Order" is not matched as depot.
RETAILER_PULL_ORDER = ("thdso", "depot", "lowes")

DOWNLOAD_TIMEOUT_MS = 120_000


def _log(msg: str) -> None:
    print(f"[pull-orders/ch] {msg}", flush=True)


def _all_frames(page: Page) -> list[Frame | Page]:
    frames: list[Frame | Page] = [page]
    for fr in page.frames:
        if fr not in frames:
            frames.append(fr)
    return frames


def _resolve_table_frame(page: Page) -> Frame | Page:
    """CommerceHub tables often live in an iframe — find the frame with Download controls."""
    for fr in _all_frames(page):
        try:
            count = fr.locator(DOWNLOAD_SPAN_SELECTOR).count()
        except Exception:
            continue
        if count > 0:
            name = fr.url if hasattr(fr, "url") else "main"
            _log(f"Using CommerceHub table frame ({count} Download control(s)): {name}")
            return fr
    _log("WARN: Download controls not found in any frame; using main page.")
    return page


def _goto_packslips(page: Page) -> Frame | Page:
    page.goto(COMMERCEHUB_PACKSLIPS_URL, wait_until="domcontentloaded", timeout=120_000)
    page.wait_for_timeout(1000)
    try:
        page.locator("text=/packing slip/i").first.wait_for(state="visible", timeout=60_000)
    except PlaywrightTimeout:
        pass
    frame = _resolve_table_frame(page)
    _wait_for_download_table(frame)
    return frame


def _goto_order_files(page: Page) -> Frame | Page:
    page.goto(COMMERCEHUB_ORDER_FILES_URL, wait_until="domcontentloaded", timeout=120_000)
    page.wait_for_timeout(1000)
    try:
        page.locator("text=/order file/i").first.wait_for(state="visible", timeout=60_000)
    except PlaywrightTimeout:
        pass
    frame = _resolve_table_frame(page)
    _wait_for_download_table(frame)
    return frame


def _wait_for_download_table(frame: Frame | Page) -> None:
    try:
        frame.locator("table tbody tr").first.wait_for(state="visible", timeout=60_000)
    except PlaywrightTimeout:
        frame.locator("table tr").first.wait_for(state="visible", timeout=30_000)
    frame.wait_for_timeout(1500)


def _row_matches_retailer(key: str, row_text: str) -> bool:
    t = (row_text or "").lower()
    if key == "thdso":
        return "special" in t and "home depot" in t
    if key == "lowes":
        return "lowe" in t
    if key == "depot":
        return "home depot" in t and "special" not in t
    return False


def _retailer_row_locator(frame: Frame | Page, key: str):
    if key == "thdso":
        return frame.locator("tr").filter(has_text=re.compile(r"special\s+order", re.I))
    if key == "lowes":
        return frame.locator("tr").filter(has_text=re.compile(r"lowe", re.I))
    return frame.locator("tr").filter(has_text=re.compile(r"home\s+depot", re.I)).filter(
        has_not_text=re.compile(r"special", re.I)
    )


def _download_button_in_row(row):
    for sel in (
        "span.download-dialog-info__element:has-text('Download')",
        "span.ch-icon-export.download-dialog-info__element",
        "span.chub-chui-chicon.ch-icon-export.download-dialog-info__element",
    ):
        loc = row.locator(sel)
        if loc.count() > 0:
            btn = loc.first
            try:
                if btn.is_visible():
                    return btn
            except Exception:
                return btn
    return None


def _find_retailer_download(frame: Frame | Page, key: str):
    rows = _retailer_row_locator(frame, key)
    n = rows.count()
    if n == 0:
        return None, None
    for i in range(n):
        row = rows.nth(i)
        try:
            if not row.is_visible():
                continue
        except Exception:
            continue
        btn = _download_button_in_row(row)
        if btn is not None:
            return row, btn
    return None, None


def _iter_retailer_downloads(frame: Frame | Page):
    for key in RETAILER_PULL_ORDER:
        row, btn = _find_retailer_download(frame, key)
        if btn is None:
            _log(f"No Download button found for {RETAILERS[key].label}.")
            continue
        _log(f"Ready to download for {RETAILERS[key].label}.")
        yield key, row, btn


def _click_download(page: Page, download_btn) -> object:
    download_btn.scroll_into_view_if_needed(timeout=10_000)
    page.wait_for_timeout(200)
    with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as dl_info:
        download_btn.click(timeout=15_000, force=True)
    return dl_info.value


def _save_download(download, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    download.save_as(str(dest))
    if not dest.is_file() or dest.stat().st_size < 100:
        raise RuntimeError(f"Download did not save a valid file: {dest}")
    return dest


def _log_missing_retailers(found_keys: set[str], *, kind: str) -> None:
    for key in RETAILER_PULL_ORDER:
        if key not in found_keys:
            _log(f"No {RETAILERS[key].label} {kind} on CommerceHub today; skipping.")


def pull_commercehub_packing_slips(page: Page, *, order_date: date | None = None) -> list[Path]:
    """Download Depot / Lowe's / Special Order packing slip PDFs."""
    _log("Opening packing slips page…")
    frame = _goto_packslips(page)
    saved: list[Path] = []
    found_keys: set[str] = set()

    for key, _row, download_btn in _iter_retailer_downloads(frame):
        found_keys.add(key)
        cfg = RETAILERS[key]
        dest = cfg.pdf_dir / pdf_filename(cfg.label, order_date)
        _log(f"Downloading packing slip for {cfg.label} → {dest}")
        try:
            download = _click_download(page, download_btn)
            path = _save_download(download, dest)
            saved.append(path)
            _log(f"Saved {path.name} ({path.stat().st_size:,} bytes)")
        except PlaywrightTimeout:
            _log(f"WARN: no download started for {cfg.label}; skipping.")
        except Exception as exc:
            _log(f"WARN: {cfg.label} packing slip failed: {exc}")
        page.wait_for_timeout(800)

    _log_missing_retailers(found_keys, kind="packing slip")
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
    frame = _goto_order_files(page)
    saved: list[Path] = []
    found_keys: set[str] = set()

    for key, _row, download_btn in _iter_retailer_downloads(frame):
        found_keys.add(key)
        cfg = RETAILERS[key]
        _log(f"Downloading order CSV for {cfg.label}")
        try:
            download = _click_download(page, download_btn)
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
                _log(f"Saved {dest.name} ({dest.stat().st_size:,} bytes)")
        except PlaywrightTimeout:
            _log(f"WARN: no download started for {cfg.label} CSV; skipping.")
        except Exception as exc:
            _log(f"WARN: {cfg.label} CSV failed: {exc}")
        page.wait_for_timeout(800)

    _log_missing_retailers(found_keys, kind="order file")
    if not saved:
        _log("WARN: no CommerceHub order CSVs were downloaded.")
    return saved


def pull_commercehub_all(page: Page, *, order_date: date | None = None) -> tuple[list[Path], list[Path]]:
    """Packing slips first, then order CSV files."""
    pdfs = pull_commercehub_packing_slips(page, order_date=order_date)
    page.goto(COMMERCEHUB_HOME_URL, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(500)
    csvs = pull_commercehub_order_csvs(page, order_date=order_date)

    if not pdfs and not csvs:
        raise RuntimeError(
            "CommerceHub: no packing slips or order CSV files were saved. "
            "Check console for 'No Download button found' or download timeout messages."
        )
    _log(
        f"CommerceHub complete: {len(pdfs)} packing slip PDF(s), {len(csvs)} order CSV(s)."
    )
    return pdfs, csvs
