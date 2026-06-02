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
    partner_text_to_key,
    pdf_filename,
)

# Row-scoped controls (avoid hidden dialog templates that also contain "Download").
ROW_DOWNLOAD_SELECTORS = (
    "a:has-text('Download')",
    "span.download-dialog-info__element:has-text('Download')",
    "span.ch-icon-export",
    "span.chub-chui-chicon.ch-icon-export",
    "input[type='button'][value*='ownload' i]",
    "input[type='submit'][value*='ownload' i]",
)

# thdso before depot so "Home Depot Special Order" is not matched as depot.
RETAILER_PULL_ORDER = ("thdso", "depot", "lowes")

DOWNLOAD_TIMEOUT_MS = 120_000
FILE_RESPONSE_TIMEOUT_MS = 90_000


def _log(msg: str) -> None:
    print(f"[pull-orders/ch] {msg}", flush=True)


def _all_frames(page: Page) -> list[Frame | Page]:
    frames: list[Frame | Page] = [page]
    for fr in page.frames:
        if fr not in frames:
            frames.append(fr)
    return frames


def _frame_score(fr: Frame | Page) -> int:
    """Prefer the frame that has visible retailer table rows, not hidden dialog templates."""
    score = 0
    try:
        rows = fr.locator("table tbody tr")
        n = rows.count()
    except Exception:
        return 0
    for i in range(n):
        row = rows.nth(i)
        try:
            if not row.is_visible():
                continue
        except Exception:
            continue
        try:
            text = row.inner_text(timeout=2_000)
        except Exception:
            text = ""
        if partner_text_to_key(text):
            score += 20
        for sel in ROW_DOWNLOAD_SELECTORS:
            try:
                loc = row.locator(sel)
                if loc.count() and loc.first.is_visible():
                    score += 5
                    break
            except Exception:
                continue
    return score


def _resolve_table_frame(page: Page) -> Frame | Page:
    """CommerceHub tables often live in an iframe — pick the frame with real retailer rows."""
    best: Frame | Page = page
    best_score = _frame_score(page)
    for fr in _all_frames(page):
        if fr is page:
            continue
        try:
            score = _frame_score(fr)
        except Exception:
            continue
        if score > best_score:
            best_score = score
            best = fr
    name = getattr(best, "url", "main")
    if best_score > 0:
        _log(f"Using CommerceHub table frame (score={best_score}): {name}")
    else:
        _log(f"WARN: no retailer rows scored; using main frame ({name}).")
    return best


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


def _retailer_row_locator(frame: Frame | Page, key: str):
    if key == "thdso":
        return frame.locator("tr").filter(has_text=re.compile(r"special\s+order", re.I))
    if key == "lowes":
        return frame.locator("tr").filter(has_text=re.compile(r"lowe", re.I))
    return frame.locator("tr").filter(has_text=re.compile(r"home\s+depot", re.I)).filter(
        has_not_text=re.compile(r"special", re.I)
    )


def _row_click_target(row):
    """Visible download/export control inside a retailer table row."""
    try:
        link = row.get_by_role("link", name=re.compile(r"download", re.I))
        if link.count() and link.first.is_visible():
            return link.first
    except Exception:
        pass
    for sel in ROW_DOWNLOAD_SELECTORS:
        loc = row.locator(sel)
        if loc.count() == 0:
            continue
        btn = loc.first
        try:
            if btn.is_visible():
                return btn
        except Exception:
            return btn
    return None


def _visible_dialog_download(page: Page, frame: Frame | Page | None = None):
    """Visible Download / export-confirm in page or table frame (not hidden templates)."""
    roots: list[Frame | Page] = []
    if frame is not None:
        roots.append(frame)
    roots.extend(fr for fr in _all_frames(page) if fr not in roots)
    for fr in roots:
        for sel in (
            'input[data-test="form-export-button"]',
            "span.download-dialog-info__element:visible",
            "a:visible:has-text('Download')",
            "button:visible:has-text('Download')",
        ):
            try:
                loc = fr.locator(sel)
                n = loc.count()
            except Exception:
                continue
            for i in range(n):
                cand = loc.nth(i)
                try:
                    if cand.is_visible():
                        return cand
                except Exception:
                    continue
    return None


def _log_table_scan(frame: Frame | Page) -> None:
    try:
        rows = frame.locator("table tbody tr")
        n = rows.count()
    except Exception:
        n = 0
    _log(f"Table scan: {n} tbody row(s) in selected frame.")
    for i in range(min(n, 8)):
        row = rows.nth(i)
        try:
            vis = row.is_visible()
            text = (row.inner_text(timeout=2_000) or "").replace("\n", " ")[:100]
            key = partner_text_to_key(text)
            has_btn = _row_click_target(row) is not None
            _log(f"  row {i}: visible={vis} partner={key!r} control={has_btn} text={text!r}")
        except Exception as exc:
            _log(f"  row {i}: could not read ({exc})")


def _iter_retailer_downloads(frame: Frame | Page):
    """Yield (retailer_key, row, click_target) from visible table rows."""
    seen: set[str] = set()
    _log_table_scan(frame)

    try:
        body_rows = frame.locator("table tbody tr")
        n = body_rows.count()
    except Exception:
        n = 0

    for i in range(n):
        row = body_rows.nth(i)
        try:
            if not row.is_visible():
                continue
        except Exception:
            continue
        try:
            row_text = row.inner_text(timeout=5_000)
        except Exception:
            continue
        key = partner_text_to_key(row_text)
        if not key or key not in RETAILER_PULL_ORDER or key in seen:
            continue
        target = _row_click_target(row)
        if target is None:
            _log(f"WARN: row for {RETAILERS[key].label} has no visible download control.")
            continue
        seen.add(key)
        _log(f"Ready to download for {RETAILERS[key].label}.")
        yield key, row, target

    for key in RETAILER_PULL_ORDER:
        if key in seen:
            continue
        rows = _retailer_row_locator(frame, key)
        for i in range(rows.count()):
            row = rows.nth(i)
            try:
                if not row.is_visible():
                    continue
            except Exception:
                continue
            target = _row_click_target(row)
            if target is None:
                continue
            seen.add(key)
            _log(f"Ready to download for {RETAILERS[key].label} (fallback row filter).")
            yield key, row, target
            break
        if key not in seen:
            _log(f"No Download control found for {RETAILERS[key].label}.")


def _is_file_response(response) -> bool:
    if response.status not in (200, 206):
        return False
    cd = (response.headers.get("content-disposition") or "").lower()
    if "attachment" in cd or "filename=" in cd:
        return True
    ct = (response.headers.get("content-type") or "").lower()
    if any(token in ct for token in ("pdf", "octet-stream", "csv", "text/plain")):
        return True
    url = (response.url or "").lower()
    return any(url.endswith(ext) for ext in (".pdf", ".csv", ".neworders", ".zip"))


def _filename_from_response(response) -> str:
    cd = response.headers.get("content-disposition") or ""
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";\n]+)', cd, re.I)
    if match:
        return match.group(1).strip().strip('"')
    url = response.url or ""
    name = url.rsplit("/", 1)[-1].split("?", 1)[0]
    return name or "download.bin"


def _save_response_body(response, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(response.body())
    if not dest.is_file() or dest.stat().st_size < 100:
        raise RuntimeError(f"HTTP response did not save a valid file: {dest}")
    return dest


def _save_download_object(download, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    download.save_as(str(dest))
    if not dest.is_file() or dest.stat().st_size < 100:
        raise RuntimeError(f"Download did not save a valid file: {dest}")
    return dest


def _try_native_save_as(dest: Path) -> bool:
    try:
        from automation.windows_save_as import fill_save_as_dialog
    except ImportError:
        return False
    return bool(fill_save_as_dialog(dest, timeout_s=25))


def _perform_click(target) -> None:
    target.scroll_into_view_if_needed(timeout=10_000)
    try:
        target.click(timeout=15_000)
        return
    except Exception:
        pass
    try:
        target.click(timeout=15_000, force=True)
        return
    except Exception:
        pass
    target.evaluate(
        """el => {
            el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
            if (typeof el.click === 'function') el.click();
        }"""
    )


def _click_row_and_capture(page: Page, frame: Frame | Page, row, target, dest: Path):
    """Click row download control; return Playwright Download or save via HTTP / Save As."""

    def _click_target() -> None:
        _perform_click(target)

    def _click_dialog_if_open() -> None:
        dialog = _visible_dialog_download(page, frame)
        if dialog is not None:
            _perform_click(dialog)

    # 1) Direct click → browser download event
    try:
        with page.expect_download(timeout=45_000) as dl_info:
            _click_target()
        return _save_download_object(dl_info.value, dest)
    except PlaywrightTimeout:
        pass

    # 2) Click opens dialog → confirm Download
    _click_target()
    page.wait_for_timeout(900)
    dialog = _visible_dialog_download(page, frame)
    if dialog is not None:
        _log("Confirming export dialog Download…")
        try:
            with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as dl_info:
                _perform_click(dialog)
            return _save_download_object(dl_info.value, dest)
        except PlaywrightTimeout:
            pass

    # 3) HTTP response with file body (common on older CommerceHub pages)
    try:
        with page.expect_response(_is_file_response, timeout=FILE_RESPONSE_TIMEOUT_MS) as resp_info:
            _click_target()
            _click_dialog_if_open()
        resp = resp_info.value
        suggested = _filename_from_response(resp)
        out = dest
        if suggested and "." in suggested:
            out = dest.with_name(suggested)
        _log(f"Saved from HTTP response ({resp.status}): {out.name}")
        return _save_response_body(resp, out)
    except PlaywrightTimeout:
        pass

    # 4) Popup window (invoice-export style)
    try:
        with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as dl_info:
            with page.expect_popup(timeout=12_000) as pop_info:
                _click_target()
            popup = pop_info.value
            popup.wait_for_load_state("domcontentloaded", timeout=60_000)
            for sel in (
                'input[data-test="form-export-button"]',
                "span.download-dialog-info__element:has-text('Download')",
                "a:has-text('Download')",
            ):
                btn = popup.locator(sel).first
                if btn.count() and btn.is_visible():
                    btn.click(timeout=30_000)
                    break
        return _save_download_object(dl_info.value, dest)
    except PlaywrightTimeout:
        pass

    # 5) Native Windows Save As
    _click_target()
    page.wait_for_timeout(600)
    _click_dialog_if_open()
    page.wait_for_timeout(800)
    if _try_native_save_as(dest):
        _log(f"Saved via Windows Save As dialog: {dest.name}")
        return dest

    raise PlaywrightTimeout(
        "CommerceHub file did not download (tried browser download, dialog, HTTP response, popup, Save As)."
    )


def _download_retailer_file(page: Page, frame: Frame | Page, row, target, dest: Path) -> Path:
    return _click_row_and_capture(page, frame, row, target, dest)


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

    for key, row, click_target in _iter_retailer_downloads(frame):
        found_keys.add(key)
        cfg = RETAILERS[key]
        dest = cfg.pdf_dir / pdf_filename(cfg.label, order_date)
        _log(f"Downloading packing slip for {cfg.label} → {dest}")
        try:
            path = _download_retailer_file(page, frame, row, click_target, dest)
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

    for key, row, click_target in _iter_retailer_downloads(frame):
        found_keys.add(key)
        cfg = RETAILERS[key]
        _log(f"Downloading order CSV for {cfg.label}")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                raw_path = Path(tmp) / "order.neworders"
                saved_path = _download_retailer_file(page, frame, row, click_target, raw_path)
                raw_path = saved_path
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


def _accept_js_dialogs(page: Page) -> None:
    def _handler(dialog) -> None:
        try:
            dialog.accept()
        except Exception:
            pass

    page.on("dialog", _handler)


def pull_commercehub_all(page: Page, *, order_date: date | None = None) -> tuple[list[Path], list[Path]]:
    """Packing slips first, then order CSV files."""
    _accept_js_dialogs(page)
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
