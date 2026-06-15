"""CommerceHub packing slip (PDF) and order file (CSV) downloads for pull-orders."""

from __future__ import annotations

import csv
import os
import re
import shutil
import tempfile
import time
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

# Row export icon / "Download" label on packing-slip & order-file tables (not the modal confirm).
ROW_EXPORT_SELECTORS = (
    "span.download-dialog-info__element:has-text('Download')",
    "span.ch-icon-export.download-dialog-info__element",
    "span.chub-chui-chicon.ch-icon-export.download-dialog-info__element",
    "span.ch-icon-export",
    "span.chub-chui-chicon.ch-icon-export",
)

ROW_DOWNLOAD_SELECTORS = ROW_EXPORT_SELECTORS + (
    "a:has-text('Download')",
    "a[href*='download' i]",
    "a[href*='export' i]",
    "a[href*='packslip' i]",
    "input[type='button'][value*='ownload' i]",
    "input[type='submit'][value*='ownload' i]",
)

# Modal confirm — same control invoice reports use after opening Export Search.
FORM_EXPORT_BUTTON = 'input[data-test="form-export-button"], button[data-test="form-export-button"]'

_READY_TO_DOWNLOAD = re.compile(r"ready\s+to\s+download", re.I)

# Regular Home Depot before Special Orders — CSV master sheet order matters (depot then thdso).
# Row matching still excludes "special" from the depot row locator.
RETAILER_PULL_ORDER = ("depot", "thdso", "lowes")

DOWNLOAD_TIMEOUT_MS = 120_000
FILE_RESPONSE_TIMEOUT_MS = 90_000
DIALOG_WAIT_MS = 20_000
SAVE_AS_WAIT_S = 55.0
def _retailer_settle_ms() -> int:
    raw = (os.environ.get("PULL_ORDERS_RETAILER_SETTLE_MS") or "5000").strip()
    try:
        return max(1_000, int(raw))
    except ValueError:
        return 5_000


RETAILER_SETTLE_MS = 5_000  # default; _settle_between_retailers uses _retailer_settle_ms()
MERCHANT_VERIFY_RETRIES = 3

# Export modal confirm (do not use row-level span.download-dialog-info__element here).
DIALOG_DOWNLOAD_SELECTORS = (
    FORM_EXPORT_BUTTON,
    "button.ch-button-primary:has-text('Download')",
    "button:has-text('Download')",
    "input[type='button'][value*='Download' i]",
    "input[type='submit'][value*='Download' i]",
)


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
        if row.locator("td.characterdata").count():
            score += 5
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


def _wait_page_table_hint(page: Page, *, hint: str) -> None:
    """Wait for retailer table cells without invalid mixed CSS/text selectors."""
    try:
        page.locator("td.characterdata").first.wait_for(state="visible", timeout=45_000)
        return
    except PlaywrightTimeout:
        pass
    except Exception:
        pass
    try:
        page.get_by_text(re.compile(hint, re.I)).first.wait_for(state="visible", timeout=15_000)
    except Exception:
        pass


def _goto_packslips(page: Page) -> Frame | Page:
    page.goto(COMMERCEHUB_PACKSLIPS_URL, wait_until="domcontentloaded", timeout=120_000)
    page.wait_for_timeout(400)
    _wait_page_table_hint(page, hint=r"packing\s+slip|ready\s+to\s+download")
    frame = _resolve_table_frame(page)
    _wait_for_download_table(frame)
    return frame


def _goto_order_files(page: Page) -> Frame | Page:
    page.goto(COMMERCEHUB_ORDER_FILES_URL, wait_until="domcontentloaded", timeout=120_000)
    page.wait_for_timeout(400)
    _wait_page_table_hint(page, hint=r"order\s+file|ready\s+to\s+download")
    frame = _resolve_table_frame(page)
    _wait_for_download_table(frame)
    return frame


def _wait_for_download_table(frame: Frame | Page) -> None:
    try:
        frame.locator("table tbody tr td.characterdata").first.wait_for(
            state="visible", timeout=45_000
        )
    except PlaywrightTimeout:
        try:
            frame.locator("table tbody tr").first.wait_for(state="visible", timeout=30_000)
        except PlaywrightTimeout:
            frame.locator("table tr").first.wait_for(state="visible", timeout=15_000)
    frame.wait_for_timeout(500)


def _characterdata_label_pattern(key: str) -> re.Pattern | None:
    if key == "lowes":
        return re.compile(r"Lowe'?s?", re.I)
    if key == "thdso":
        return re.compile(r"special\s+order", re.I)
    if key == "depot":
        return re.compile(r"Home\s+Depot", re.I)
    return None


def _retailer_row_locator(frame: Frame | Page, key: str):
    """Rows identified by td.characterdata partner name (CommerceHub packing slip table)."""
    pat = _characterdata_label_pattern(key)
    if pat is not None:
        if key == "depot":
            rows = frame.locator("tr").filter(
                has=frame.locator("td.characterdata", has_text=pat)
            ).filter(has_not=frame.locator("td.characterdata", has_text=re.compile(r"special", re.I)))
        else:
            rows = frame.locator("tr").filter(
                has=frame.locator("td.characterdata", has_text=pat)
            )
        if rows.count():
            return rows
    if key == "thdso":
        return frame.locator("tr").filter(has_text=re.compile(r"special\s+order", re.I))
    if key == "lowes":
        return frame.locator("tr").filter(
            has=frame.locator(
                "td.characterdata",
                has_text=re.compile(r"^Lowe'?s?\s*$", re.I),
            )
        )
    return frame.locator("tr").filter(has_text=re.compile(r"home\s+depot", re.I)).filter(
        has_not_text=re.compile(r"special", re.I)
    )


def _partner_key_from_row(row) -> str | None:
    """Read partner from td.characterdata cells only (never whole-row text)."""
    try:
        cells = row.locator("td.characterdata")
        for i in range(cells.count()):
            text = (cells.nth(i).inner_text(timeout=2_000) or "").strip()
            key = partner_text_to_key(text)
            if key:
                return key
    except Exception:
        pass
    return None


def _partner_label_from_row(row) -> str:
    """Human-readable partner name from td.characterdata (for logs and verification)."""
    try:
        cells = row.locator("td.characterdata")
        for i in range(cells.count()):
            text = (cells.nth(i).inner_text(timeout=2_000) or "").strip()
            if partner_text_to_key(text):
                return text
    except Exception:
        pass
    return ""


def _find_retailer_row_for_key(
    frame: Frame | Page, key: str
) -> tuple[object, object, str] | None:
    """
    Locate exactly one table row for ``key`` and verify partner text maps to that key.
    Returns (row, click_target, partner_label) or None.
    """
    label = RETAILERS[key].label
    rows = _retailer_row_locator(frame, key)
    for i in range(rows.count()):
        row = rows.nth(i)
        try:
            if not row.is_visible():
                continue
        except Exception:
            continue
        row_key = _partner_key_from_row(row)
        partner_label = _partner_label_from_row(row)
        if row_key != key:
            _log(
                f"WARN: skipping row for {label}: partner cell {partner_label!r} "
                f"maps to {row_key!r}, expected {key!r}."
            )
            continue
        target = _row_click_target(row, partner_key=key)
        if target is None:
            _log(f"WARN: {label} row ({partner_label!r}) has no download control.")
            continue
        _log(f"Matched {label} row: partner={partner_label!r}")
        return row, target, partner_label
    return None


def _clickable_in_cell(cell):
    for sel in ROW_EXPORT_SELECTORS:
        loc = cell.locator(sel)
        if loc.count() == 0:
            continue
        btn = loc.first
        try:
            if btn.is_visible():
                return btn
        except Exception:
            return btn
    return None


def _row_download_control(row):
    """CommerceHub row export control (packing slips / order files) — not the partner name cell."""
    for sel in ROW_EXPORT_SELECTORS:
        loc = row.locator(sel)
        n = loc.count()
        for i in range(n):
            btn = loc.nth(i)
            try:
                if btn.is_visible():
                    return btn
            except Exception:
                return btn
    try:
        dl = row.get_by_text(re.compile(r"^\s*download\s*$", re.I))
        if dl.count() and dl.first.is_visible():
            return dl.first
    except Exception:
        pass
    return None


def _find_form_export_button(page: Page, timeout_ms: int = 12_000):
    """Visible export confirm — same data-test button as commercehub_invoice_export."""
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        for fr in _all_frames(page):
            loc = fr.locator(FORM_EXPORT_BUTTON)
            for i in range(loc.count()):
                btn = loc.nth(i)
                try:
                    if btn.is_visible():
                        return btn
                except Exception:
                    return btn
        page.wait_for_timeout(300)
    return None


def _row_click_target(row, partner_key: str | None = None):
    """Download/export control — row icon/Download span, then td.characterdata action column."""
    hit = _row_download_control(row)
    if hit is not None:
        return hit
    try:
        cells = row.locator("td.characterdata")
        n = cells.count()
        for i in range(n):
            cell = cells.nth(i)
            try:
                cell_text = (cell.inner_text(timeout=2_000) or "").strip()
            except Exception:
                cell_text = ""
            if partner_key and partner_text_to_key(cell_text) == partner_key:
                continue
            if partner_text_to_key(cell_text) and not partner_key:
                continue
            if _READY_TO_DOWNLOAD.search(cell_text):
                hit = _clickable_in_cell(cell)
                if hit is not None:
                    return hit
            try:
                link = cell.get_by_role("link", name=re.compile(r"download", re.I))
                if link.count() and link.first.is_visible():
                    return link.first
            except Exception:
                pass
            hit = _clickable_in_cell(cell)
            if hit is not None:
                return hit
        if n > 0:
            last = cells.nth(n - 1)
            hit = _clickable_in_cell(last)
            if hit is not None:
                return hit
    except Exception:
        pass
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
    """Visible Download / export-confirm in page or any frame (not hidden templates)."""
    roots: list[Frame | Page] = []
    if frame is not None:
        roots.append(frame)
    roots.extend(fr for fr in _all_frames(page) if fr not in roots)
    for fr in roots:
        for sel in DIALOG_DOWNLOAD_SELECTORS:
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


def _wait_for_export_dialog(page: Page, frame: Frame | Page, timeout_ms: int = DIALOG_WAIT_MS):
    """Row click often opens an export modal (e.g. batch count) before any file is offered."""
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        dlg = _visible_dialog_download(page, frame)
        if dlg is not None:
            return dlg
        for fr in _all_frames(page):
            try:
                shell = fr.locator(
                    ".modal, [role='dialog'], .ch-modal, .download-dialog"
                ).filter(has_text=re.compile(r"download|export", re.I))
                if shell.count() and shell.first.is_visible():
                    inner = _visible_dialog_download(page, frame)
                    if inner is not None:
                        return inner
            except Exception:
                continue
        page.wait_for_timeout(350)
    return None


def _failure_screenshot(page: Page, label: str) -> Path | None:
    try:
        snap = Path(__file__).resolve().parent.parent / f"pull_orders_ch_{label.replace(' ', '_')}.png"
        page.screenshot(path=str(snap), full_page=True)
        _log(f"Debug screenshot: {snap}")
        return snap
    except Exception:
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
            key = _partner_key_from_row(row)
            has_btn = _row_download_control(row) is not None or _row_click_target(row, key) is not None
            partner_cell = ""
            try:
                for j in range(row.locator("td.characterdata").count()):
                    t = (row.locator("td.characterdata").nth(j).inner_text(timeout=1_000) or "").strip()
                    if partner_text_to_key(t):
                        partner_cell = t
                        break
            except Exception:
                pass
            _log(
                f"  row {i}: visible={vis} partner={key!r} cell={partner_cell!r} "
                f"control={has_btn} text={text!r}"
            )
        except Exception as exc:
            _log(f"  row {i}: could not read ({exc})")


def _iter_retailer_downloads(frame: Frame | Page):
    """Yield (retailer_key, row, click_target) from td.characterdata partner rows."""
    seen: set[str] = set()
    _log_table_scan(frame)

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
            row_key = _partner_key_from_row(row) or key
            if row_key != key:
                continue
            target = _row_click_target(row, partner_key=key)
            if target is None:
                _log(f"WARN: {RETAILERS[key].label} row has no download control in characterdata cells.")
                continue
            seen.add(key)
            _log(f"Ready to download for {RETAILERS[key].label} (td.characterdata).")
            yield key, row, target
            break
        if key not in seen:
            _log(f"No row with td.characterdata for {RETAILERS[key].label}.")

    try:
        body_rows = frame.locator("table tbody tr:has(td.characterdata)")
        n = body_rows.count()
    except Exception:
        n = 0

    for i in range(n):
        if len(seen) >= len(RETAILER_PULL_ORDER):
            break
        row = body_rows.nth(i)
        try:
            if not row.is_visible():
                continue
        except Exception:
            continue
        key = _partner_key_from_row(row)
        if not key or key not in RETAILER_PULL_ORDER or key in seen:
            continue
        target = _row_click_target(row, partner_key=key)
        if target is None:
            continue
        seen.add(key)
        _log(f"Ready to download for {RETAILERS[key].label}.")
        yield key, row, target


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


def _suggested_filename_matches_key(name: str, key: str) -> bool:
    """Reject only when the browser filename clearly names a different retailer."""
    n = (name or "").strip().lower()
    if not n:
        return True

    def _markers(k: str) -> tuple[str, ...]:
        if k == "lowes":
            return ("lowe",)
        if k == "depot":
            return ("depot", "home depot")
        if k == "thdso":
            return ("special", "thdso")
        return ()

    for other_key in RETAILER_PULL_ORDER:
        if other_key == key:
            continue
        for marker in _markers(other_key):
            if marker in n:
                return False
    return True


def _file_content_matches_retailer(path: Path, key: str) -> bool:
    """Best-effort check that a saved PDF/CSV body matches the row we clicked."""
    try:
        blob = path.read_bytes()[:160_000]
    except Exception:
        return False
    low = blob.lower()

    def _has(fragment: bytes) -> bool:
        return fragment in low

    if key == "lowes":
        return _has(b"lowe") and not (_has(b"home depot") and not _has(b"lowe"))
    if key == "depot":
        return _has(b"home depot") and not _has(b"special order")
    if key == "thdso":
        return _has(b"special order") or _has(b"thdso")
    return True


def _verify_saved_file_for_retailer(
    path: Path, key: str, *, suggested_name: str = ""
) -> None:
    label = RETAILERS[key].label
    if suggested_name and not _suggested_filename_matches_key(suggested_name, key):
        raise RuntimeError(
            f"Download filename {suggested_name!r} does not match {label} ({key!r})."
        )
    if path.suffix.lower() == ".csv":
        detected = _read_csv_merchant_key(path)
        if detected and detected != key:
            raise RuntimeError(
                f"CSV merchant {detected!r} does not match {label} ({key!r})."
            )
    elif path.suffix.lower() == ".pdf":
        if not _file_content_matches_retailer(path, key):
            raise RuntimeError(
                f"PDF content does not look like {label} ({key!r}). "
                "Wrong row may have been exported."
            )


def _filename_from_response(response) -> str:
    cd = response.headers.get("content-disposition") or ""
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";\n]+)', cd, re.I)
    if match:
        return match.group(1).strip().strip('"')
    url = response.url or ""
    name = url.rsplit("/", 1)[-1].split("?", 1)[0]
    return name or "download.bin"


def _save_response_body(
    response, dest: Path, *, expected_key: str | None = None
) -> Path:
    suggested = _filename_from_response(response)
    if expected_key and suggested and not _suggested_filename_matches_key(
        suggested, expected_key
    ):
        raise RuntimeError(
            f"HTTP filename {suggested!r} does not match retailer {expected_key!r}."
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    part.write_bytes(response.body())
    if not part.is_file() or part.stat().st_size < 100:
        raise RuntimeError(f"HTTP response did not save a valid file: {dest}")
    if expected_key:
        _verify_saved_file_for_retailer(part, expected_key, suggested_name=suggested)
    part.replace(dest)
    return dest


def _save_download_object(
    download, dest: Path, *, expected_key: str | None = None
) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    suggested = ""
    try:
        suggested = (download.suggested_filename or "").strip()
        if suggested:
            _log(f"Browser download filename: {suggested}")
    except Exception:
        pass
    if expected_key and suggested and not _suggested_filename_matches_key(
        suggested, expected_key
    ):
        raise RuntimeError(
            f"Browser offered {suggested!r} but expected {RETAILERS[expected_key].label}."
        )
    part = dest.with_suffix(dest.suffix + ".part")
    download.save_as(str(part))
    if not part.is_file() or part.stat().st_size < 100:
        raise RuntimeError(f"Download did not save a valid file: {dest}")
    if expected_key:
        _verify_saved_file_for_retailer(part, expected_key, suggested_name=suggested)
    if dest.exists():
        dest.unlink()
    part.replace(dest)
    return dest


def _settle_between_retailers(page: Page) -> None:
    """Let the prior download finish and close modals before the next retailer row."""
    _dismiss_commercehub_overlays(page)
    page.wait_for_timeout(_retailer_settle_ms())


def _try_native_save_as(dest: Path, *, expected_key: str | None = None) -> bool:
    try:
        from automation.windows_save_as import fill_save_as_dialog, wait_for_save_as_dialog
    except ImportError:
        return False
    deadline = time.monotonic() + SAVE_AS_WAIT_S
    while time.monotonic() < deadline:
        if wait_for_save_as_dialog(timeout_s=2.0):
            remaining = max(10.0, deadline - time.monotonic())
            part = dest.with_suffix(dest.suffix + ".part")
            if fill_save_as_dialog(part, timeout_s=remaining):
                if not part.is_file() or part.stat().st_size < 100:
                    continue
                try:
                    if expected_key:
                        _verify_saved_file_for_retailer(part, expected_key)
                except Exception:
                    try:
                        part.unlink(missing_ok=True)
                    except Exception:
                        pass
                    raise
                if dest.exists():
                    dest.unlink()
                part.replace(dest)
                return True
        time.sleep(0.35)
    return False


def _dismiss_commercehub_overlays(page: Page) -> None:
    """Close export modals / dialogs so the next retailer row click starts clean."""
    for _ in range(3):
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        page.wait_for_timeout(250)
    for sel in (
        'button[data-test="form-cancel-button"]',
        "button:has-text('Cancel')",
        "button:has-text('Close')",
        ".modal button.close",
        "[role='dialog'] button:has-text('Close')",
    ):
        for fr in _all_frames(page):
            try:
                loc = fr.locator(sel)
                for i in range(min(loc.count(), 3)):
                    btn = loc.nth(i)
                    if btn.is_visible():
                        btn.click(timeout=2_000)
                        page.wait_for_timeout(300)
            except Exception:
                continue
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        if _find_form_export_button(page, timeout_ms=400) is None and _visible_dialog_download(page) is None:
            break
        page.wait_for_timeout(300)
    page.wait_for_timeout(400)


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


def _click_row_and_capture(
    page: Page,
    frame: Frame | Page,
    row,
    target,
    dest: Path,
    *,
    retailer_label: str = "",
    expected_key: str | None = None,
) -> Path:
    """
    Click row export, then confirm the export dialog Download, then save to dest.

    CommerceHub usually shows a modal after the row click (batch / count) — the file
    only starts after the modal Download button, not from the row click alone.
    """
    started = time.monotonic()
    label = retailer_label or "retailer"
    _dismiss_commercehub_overlays(page)
    page.wait_for_timeout(600)

    def _click_target() -> None:
        _perform_click(target)

    def _click_dialog_if_open() -> bool:
        dialog = _visible_dialog_download(page, frame)
        if dialog is None:
            return False
        _perform_click(dialog)
        return True

    # 1) Same two-step pattern as invoice reports: row export → form-export-button (or Save As for PDF)
    _log(f"{label}: clicking row export control…")
    _click_target()
    page.wait_for_timeout(800)

    export_btn = _find_form_export_button(page, timeout_ms=12_000)
    if export_btn is not None:
        _log(f"{label}: export modal open; clicking form-export-button (invoice-report control)…")
        try:
            with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as dl_info:
                _perform_click(export_btn)
            return _save_download_object(
                dl_info.value, dest, expected_key=expected_key
            )
        except PlaywrightTimeout:
            _log(f"{label}: form-export-button did not start browser download; trying Save As…")
            if _try_native_save_as(dest, expected_key=expected_key):
                _log(f"{label}: saved via Save As after form-export-button.")
                return dest

    for attempt in range(1, 5):
        dialog = _wait_for_export_dialog(page, frame, timeout_ms=6_000)
        if dialog is not None:
            _log(f"{label}: export dialog open (attempt {attempt}); clicking Download…")
            try:
                with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as dl_info:
                    _perform_click(dialog)
                return _save_download_object(
                    dl_info.value, dest, expected_key=expected_key
                )
            except PlaywrightTimeout:
                _log(
                    f"{label}: Download in modal did not start a browser download "
                    f"(attempt {attempt}); trying Enter key…"
                )
                try:
                    with page.expect_download(timeout=25_000) as dl_info:
                        page.keyboard.press("Enter")
                    return _save_download_object(
                        dl_info.value, dest, expected_key=expected_key
                    )
                except PlaywrightTimeout:
                    pass
            if _try_native_save_as(dest, expected_key=expected_key):
                _log(f"{label}: saved via Save As after modal Download.")
                return dest
        else:
            _log(f"{label}: no export dialog yet (attempt {attempt}); re-clicking row…")
        _click_target()
        page.wait_for_timeout(700)

    # 2) Row + dialog together while listening for HTTP file body
    _log(f"{label}: trying HTTP file response…")
    try:
        with page.expect_response(_is_file_response, timeout=FILE_RESPONSE_TIMEOUT_MS) as resp_info:
            _click_target()
            page.wait_for_timeout(800)
            _click_dialog_if_open()
        resp = resp_info.value
        _log(f"Saved from HTTP response ({resp.status}): {dest.name}")
        return _save_response_body(resp, dest, expected_key=expected_key)
    except PlaywrightTimeout:
        pass

    # 3) Direct download on row click (some tenants)
    try:
        with page.expect_download(timeout=25_000) as dl_info:
            _click_target()
        return _save_download_object(
            dl_info.value, dest, expected_key=expected_key
        )
    except PlaywrightTimeout:
        pass

    # 4) Popup export window
    try:
        with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as dl_info:
            with page.expect_popup(timeout=15_000) as pop_info:
                _click_target()
                page.wait_for_timeout(600)
                _click_dialog_if_open()
            popup = pop_info.value
            popup.wait_for_load_state("domcontentloaded", timeout=60_000)
            for sel in DIALOG_DOWNLOAD_SELECTORS:
                btn = popup.locator(sel).first
                if btn.count():
                    try:
                        if btn.is_visible():
                            btn.click(timeout=30_000)
                            break
                    except Exception:
                        btn.click(timeout=30_000, force=True)
                        break
        return _save_download_object(
            dl_info.value, dest, expected_key=expected_key
        )
    except PlaywrightTimeout:
        pass

    # 5) Native Save As (packing slips often use "Save Print Output As")
    _log(f"{label}: trying Windows Save As → {dest}")
    _click_target()
    page.wait_for_timeout(700)
    _click_dialog_if_open()
    page.wait_for_timeout(500)
    if _try_native_save_as(dest, expected_key=expected_key):
        _log(f"{label}: saved via Save As dialog.")
        return dest

    _failure_screenshot(page, label)
    raise PlaywrightTimeout(
        f"{label}: file was not saved to {dest}. "
        "Row export was found but Download/Save As did not complete. "
        "See debug screenshot in Inventory Submissions folder."
    )


def _download_retailer_file(
    page: Page,
    frame: Frame | Page,
    row,
    target,
    dest: Path,
    *,
    retailer_label: str,
    expected_key: str,
) -> Path:
    return _click_row_and_capture(
        page,
        frame,
        row,
        target,
        dest,
        retailer_label=retailer_label,
        expected_key=expected_key,
    )


def _log_missing_retailers(found_keys: set[str], *, kind: str) -> None:
    for key in RETAILER_PULL_ORDER:
        if key not in found_keys:
            _log(f"No {RETAILERS[key].label} {kind} on CommerceHub today; skipping.")


def _download_one_retailer_from_table(
    page: Page,
    *,
    key: str,
    goto_table,
    dest: Path,
    retailer_label: str,
    verify_csv_key: bool = False,
) -> Path | None:
    """Re-open the CommerceHub table page and download one retailer (fresh frame each time)."""
    last_err: Exception | None = None
    for attempt in range(1, MERCHANT_VERIFY_RETRIES + 1):
        _settle_between_retailers(page)
        frame = goto_table(page)
        match = _find_retailer_row_for_key(frame, key)
        if match is None:
            return None
        row, click_target, partner_label = match
        _log(
            f"Downloading for {retailer_label} (partner {partner_label!r}, "
            f"attempt {attempt}/{MERCHANT_VERIFY_RETRIES}) → {dest}"
        )
        try:
            path = _download_retailer_file(
                page,
                frame,
                row,
                click_target,
                dest,
                retailer_label=retailer_label,
                expected_key=key,
            )
            if not path.is_file() or path.stat().st_size < 100:
                raise RuntimeError(f"Download did not produce a valid file at {path}")
            if verify_csv_key:
                csv_check = path
                if path.suffix.lower() == ".neworders":
                    csv_check = _neworders_to_csv(path)
                detected = _read_csv_merchant_key(csv_check)
                if detected and detected != key:
                    raise RuntimeError(
                        f"Downloaded file merchant {detected!r} does not match "
                        f"expected {key!r} (row partner {partner_label!r})."
                    )
            if path.resolve() != dest.resolve() and dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest)
                path = dest
            _settle_between_retailers(page)
            return path
        except Exception as exc:
            last_err = exc
            _log(f"WARN: {retailer_label} download attempt {attempt} failed: {exc}")
            for stray in (dest, dest.with_suffix(dest.suffix + ".part")):
                try:
                    if stray.is_file():
                        stray.unlink()
                except Exception:
                    pass
            _settle_between_retailers(page)
    if last_err is not None:
        raise last_err
    return None


def pull_commercehub_packing_slips(page: Page, *, order_date: date | None = None) -> list[Path]:
    """Download Depot / Lowe's / Special Order packing slip PDFs."""
    _log("Opening packing slips page…")
    saved: list[Path] = []
    found_keys: set[str] = set()

    for key in RETAILER_PULL_ORDER:
        cfg = RETAILERS[key]
        dest = cfg.pdf_dir / pdf_filename(cfg.label, order_date)
        try:
            path = _download_one_retailer_from_table(
                page,
                key=key,
                goto_table=_goto_packslips,
                dest=dest,
                retailer_label=cfg.label,
            )
            if path is None:
                _log(f"No row with td.characterdata for {cfg.label}.")
                continue
            found_keys.add(key)
            saved.append(path)
            _log(f"Saved {path.name} ({path.stat().st_size:,} bytes)")
        except PlaywrightTimeout:
            _log(f"WARN: download did not complete for {cfg.label}; skipping.")
        except Exception as exc:
            _log(f"WARN: {cfg.label} packing slip failed: {exc}")
        finally:
            _settle_between_retailers(page)

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
    saved: list[Path] = []
    found_keys: set[str] = set()

    for key in RETAILER_PULL_ORDER:
        cfg = RETAILERS[key]
        _log(f"Downloading order CSV for {cfg.label}")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                raw_path = Path(tmp) / "order.neworders"
                saved_path = _download_one_retailer_from_table(
                    page,
                    key=key,
                    goto_table=_goto_order_files,
                    dest=raw_path,
                    retailer_label=cfg.label,
                    verify_csv_key=True,
                )
                if saved_path is None:
                    _log(f"No row with td.characterdata for {cfg.label}.")
                    continue
                found_keys.add(key)
                raw_path = saved_path
                csv_path = _neworders_to_csv(raw_path)
                detected = _read_csv_merchant_key(csv_path)
                if detected and detected != key:
                    raise RuntimeError(
                        f"CSV merchant {detected!r} does not match {cfg.label} ({key!r})."
                    )
                if cfg.csv_merchant_id:
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
        finally:
            _settle_between_retailers(page)

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


def _wait_commercehub_ready_fast(page: Page, selectors: dict) -> None:
    """After profile click — short waits only (pull-orders; avoids long selector polling)."""
    quick_selectors = [
        (selectors.get("logged_in_ready") or "").strip(),
        "a[href*='gotoHome.do']",
        "a[href*='gotoViewPackslips.do']",
        "a[href*='gotoViewOrders.do']",
        "a[href*='gotoOpenOrders.do']",
    ]
    for sel in quick_selectors:
        if not sel:
            continue
        try:
            page.locator(sel).first.wait_for(state="visible", timeout=5_000)
            _log("CommerceHub home ready.")
            return
        except Exception:
            continue
    url = (page.url or "").lower()
    if "dsm.commercehub.com" in url and not any(
        x in url for x in ("login", "signin", "okta", "auth0", "microsoftonline")
    ):
        _log("CommerceHub ready (by URL).")
        return
    raise TimeoutError("CommerceHub login not confirmed within fast pull-orders timeout.")


def login_commercehub_for_pull(page: Page, automation) -> None:
    """
    Log in for pull-orders with shorter delays and a fast post-profile ready check.
    """
    os.environ["COMMERCEHUB_CHAIN_FAST"] = "1"
    rithum = automation.config.setdefault("rithum", {})
    delays = dict(rithum.get("login_delays_ms") or {})
    delays.update(
        {
            "after_email_continue": 350,
            "after_password_continue": 450,
            "before_profile_selector": 150,
        }
    )
    rithum["login_delays_ms"] = delays

    orig_wait = automation._wait_for_commercehub_logged_in
    try:
        automation._wait_for_commercehub_logged_in = (
            lambda p, s: _wait_commercehub_ready_fast(p, s)
        )
        _log("Logging into CommerceHub (pull-orders fast login)…")
        automation.login(page)
    finally:
        automation._wait_for_commercehub_logged_in = orig_wait


def pull_commercehub_all(page: Page, *, order_date: date | None = None) -> tuple[list[Path], list[Path]]:
    """Packing slips first, then order CSV files."""
    _accept_js_dialogs(page)
    pdfs = pull_commercehub_packing_slips(page, order_date=order_date)
    page.goto(COMMERCEHUB_HOME_URL, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(300)
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
