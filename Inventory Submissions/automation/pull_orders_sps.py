"""SPS Commerce order pull (PDF print + combined CSV) for Tractor Supply and Grainger."""

from __future__ import annotations

import base64
import os
import re
import time
from datetime import date
from pathlib import Path

from playwright.sync_api import BrowserContext, Page, TimeoutError as PlaywrightTimeout

from automation.pull_orders_config import (
    RETAILERS,
    SPS_TRANSACTIONS_URL,
    csv_filename,
    pdf_filename,
)

# Reuse hardened SPS helpers from tracking automation.
from run_sps_tracking import (  # noqa: E402
    clear_click_blockers,
    click_advanced_search_button,
    click_first_visible,
    clear_document_type_filter,
    ensure_document_type_order,
    wait_for_transactions_page_ready,
    _contexts,
    _raise_if_cookie_or_auth_wall,
)

DOWNLOAD_TIMEOUT_MS = 180_000
COMBINE_CSV_RE = re.compile(r"combine\s+documents\s+into\s+one\s+csv\s+file", re.I)


def _log(msg: str) -> None:
    print(f"[pull-orders/sps] {msg}", flush=True)


def ensure_status_new(page: Page) -> None:
    """Set Advanced Search Status filter to New."""
    for _ in range(6):
        removed = False
        for sel in (
            "xpath=//*[contains(normalize-space(.), 'Status')]/following::button[contains(@aria-label,'Remove') or contains(@title,'Remove')][1]",
            "xpath=//*[contains(normalize-space(.), 'Status')]/following::*[contains(@class,'close') or contains(@class,'sps-icon-close')][1]",
        ):
            for ctx in _contexts(page):
                try:
                    loc = ctx.locator(sel)
                    if loc.count() == 0 or not loc.first.is_visible():
                        continue
                    loc.first.click(timeout=700, force=True)
                    removed = True
                    page.wait_for_timeout(80)
                    break
                except Exception:
                    continue
            if removed:
                break
        if not removed:
            break

    for attempt in range(1, 6):
        clear_click_blockers(page)
        field = None
        for ctx in _contexts(page):
            for sel in (
                "input[data-testid='advancedSearchStatusesMultiselect__option-list-input']",
                "xpath=//*[contains(normalize-space(.), 'Status')]/following::input[1]",
            ):
                try:
                    loc = ctx.locator(sel)
                    if loc.count() == 0:
                        continue
                    cand = loc.first
                    cand.wait_for(state="visible", timeout=1500)
                    field = cand
                    break
                except Exception:
                    continue
            if field is not None:
                break
        if field is None:
            page.wait_for_timeout(180)
            continue
        try:
            field.click(timeout=900, force=True)
        except Exception:
            pass
        try:
            field.fill("", timeout=700)
        except Exception:
            pass
        field.type("New", delay=20)
        if click_first_visible(
            page,
            [
                "xpath=//*[@role='option' and contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'new')]",
                "xpath=//*[contains(@class,'option') and contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'new')]",
            ],
            timeout_ms=2500,
        ):
            page.wait_for_timeout(200)
            return
        page.wait_for_timeout(180)
    raise RuntimeError("Could not set Status filter to 'New'.")


def open_order_new_search(page: Page) -> None:
    _log("Opening transactions and applying Order + New filters…")
    page.goto(SPS_TRANSACTIONS_URL, wait_until="domcontentloaded", timeout=120_000)
    _raise_if_cookie_or_auth_wall(page)
    wait_for_transactions_page_ready(page, timeout_ms=90_000)
    clear_click_blockers(page)

    if not click_first_visible(
        page,
        [
            "xpath=//button[normalize-space()='Advanced Search']",
            "button:has-text('Advanced Search')",
        ],
        timeout_ms=3000,
    ):
        _log("Advanced Search may already be open.")

    clear_document_type_filter(page)
    ensure_document_type_order(page)
    ensure_status_new(page)
    click_advanced_search_button(page)
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(1200)
    _log("Search complete.")


def _result_rows(page: Page):
    for ctx in _contexts(page):
        rows = ctx.locator("table tbody tr, [role='row']")
        n = rows.count()
        for i in range(n):
            row = rows.nth(i)
            try:
                if not row.is_visible():
                    continue
                text = row.inner_text()
            except Exception:
                continue
            if not text or "Document Type" in text:
                continue
            yield row, text


def _row_matches_tractor(text: str) -> bool:
    return "tractor supply dropship" in text.lower()


def _row_matches_grainger(text: str) -> bool:
    t = text.lower()
    return bool(re.search(r"\bgrainger\b", t))


def _row_checkbox(row) -> object | None:
    for sel in (
        "label.sps-checkable__label",
        "input[type='checkbox']",
        "[role='checkbox']",
    ):
        loc = row.locator(sel)
        if loc.count() > 0:
            try:
                if loc.first.is_visible():
                    return loc.first
            except Exception:
                continue
    return None


def _uncheck_all_rows(page: Page) -> None:
    for row, _ in _result_rows(page):
        cb = row.locator("input[type='checkbox']")
        if cb.count() == 0:
            continue
        try:
            if cb.first.is_checked():
                lbl = row.locator("label.sps-checkable__label")
                if lbl.count() > 0:
                    lbl.first.click(force=True)
                else:
                    cb.first.click(force=True)
        except Exception:
            continue


def _select_retailer_rows(page: Page, *, retailer_key: str) -> int:
    matcher = _row_matches_tractor if retailer_key == "tractor" else _row_matches_grainger
    _uncheck_all_rows(page)
    selected = 0
    for row, text in _result_rows(page):
        if not matcher(text):
            continue
        cb = _row_checkbox(row)
        if cb is None:
            continue
        try:
            cb.click(force=True)
            selected += 1
        except Exception:
            continue
    return selected


def _print_preview_wait_ms() -> int:
    raw = (os.environ.get("SPS_PRINT_PREVIEW_WAIT_MS") or "10000").strip()
    try:
        return max(3000, int(raw))
    except ValueError:
        return 10_000


def _click_print_from_ellipsis(page: Page) -> None:
    clear_click_blockers(page)
    if not click_first_visible(
        page,
        [
            "i.sps-icon.sps-icon-ellipses",
            "button:has(i.sps-icon-ellipses)",
            "[data-testid='bottomPanelEllipsisBtn']",
        ],
        timeout_ms=5000,
    ):
        raise RuntimeError("Could not click bottom ellipsis menu.")
    page.wait_for_timeout(500)
    if not click_first_visible(
        page,
        ["span:has-text('Print')", "button:has-text('Print')", "text=Print"],
        timeout_ms=5000,
    ):
        raise RuntimeError("Could not click Print in ellipsis menu.")


def _page_order_score(p: Page) -> int:
    try:
        text = p.inner_text("body")[:12000].lower()
    except Exception:
        return 0
    score = 0
    for needle, pts in (
        ("order #", 3),
        ("po date", 2),
        ("ship to", 2),
        ("bill to", 2),
        ("tractor supply", 2),
        ("grainger", 2),
        ("customer order", 2),
    ):
        if needle in text:
            score += pts
    return score


def _order_document_pages(context: BrowserContext, main_page: Page) -> list[Page]:
    pages = list(context.pages) or [main_page]
    scored = [( _page_order_score(p), p) for p in pages]
    scored.sort(key=lambda t: t[0], reverse=True)
    if scored and scored[0][0] > 0:
        return [p for s, p in scored if s > 0]
    return pages


def _cdp_print_page_to_pdf(page: Page, context: BrowserContext, dest: Path) -> bool:
    """CDP printToPDF — same output as Chrome 'Save as PDF' on the page content."""
    try:
        page.bring_to_front()
        page.wait_for_load_state("domcontentloaded", timeout=15_000)
    except Exception:
        pass
    try:
        cdp = context.new_cdp_session(page)
        try:
            cdp.send("Emulation.setEmulatedMedia", {"media": "print", "features": []})
        except Exception:
            pass
        result = cdp.send(
            "Page.printToPDF",
            {"printBackground": True, "preferCSSPageSize": True},
        )
        data = base64.b64decode(result["data"])
        if len(data) < 800 or not data.startswith(b"%PDF"):
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return True
    except Exception:
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            page.pdf(path=str(dest), print_background=True)
            return dest.is_file() and dest.stat().st_size > 800
        except Exception:
            return False


def _dismiss_print_preview(page: Page) -> None:
    for _ in range(3):
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(250)
        except Exception:
            break


def _find_visible_window(title_part: str) -> int:
    try:
        import win32gui
    except ImportError:
        return 0

    found: list[int] = []

    def _cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd) or ""
            if title_part.lower() in title.lower():
                found.append(hwnd)
        return True

    win32gui.EnumWindows(_cb, None)
    return found[0] if found else 0


def _find_save_as_dialog_hwnd() -> int:
    try:
        import win32gui
    except ImportError:
        return 0

    found: list[int] = []

    def _cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        cls = win32gui.GetClassName(hwnd)
        title = win32gui.GetWindowText(hwnd) or ""
        if cls == "#32770" and any(t in title for t in ("Save As", "Save Print Output As", "Save")):
            found.append(hwnd)
        return True

    win32gui.EnumWindows(_cb, None)
    return found[0] if found else 0


def _set_clipboard(text: str) -> None:
    import win32clipboard
    import win32con

    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
    finally:
        win32clipboard.CloseClipboard()


def _send_vk(vk: int) -> None:
    import win32api
    import win32con

    win32api.keybd_event(vk, 0, 0, 0)
    win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)


def _send_ctrl_v() -> None:
    import win32api
    import win32con

    win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
    win32api.keybd_event(ord("V"), 0, 0, 0)
    win32api.keybd_event(ord("V"), 0, win32con.KEYEVENTF_KEYUP, 0)
    win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)


def _type_ascii(text: str) -> None:
    import win32api
    import win32con

    for ch in text:
        vk_scan = win32api.VkKeyScan(ch)
        vk = vk_scan & 0xFF
        shift = (vk_scan >> 8) & 1
        if shift:
            win32api.keybd_event(win32con.VK_SHIFT, 0, 0, 0)
        win32api.keybd_event(vk, 0, 0, 0)
        win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)
        if shift:
            win32api.keybd_event(win32con.VK_SHIFT, 0, win32con.KEYEVENTF_KEYUP, 0)
        time.sleep(0.04)


def _fill_save_as_dialog(dest: Path, *, timeout_s: float = 20) -> bool:
    """Fill the Windows Save As dialog with the full destination path."""
    try:
        import win32con
        import win32gui
    except ImportError:
        return False

    dest.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        hwnd = _find_save_as_dialog_hwnd()
        if hwnd:
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                pass
            time.sleep(0.35)
            edit = win32gui.FindWindowEx(hwnd, 0, "ComboBoxEx32", None)
            if edit:
                edit = win32gui.FindWindowEx(edit, 0, "ComboBox", None)
            if not edit:
                edit = win32gui.FindWindowEx(hwnd, 0, "Edit", None)
            if edit:
                try:
                    _set_clipboard(str(dest))
                    win32gui.SetForegroundWindow(hwnd)
                    time.sleep(0.15)
                    _send_ctrl_v()
                    time.sleep(0.2)
                    save_btn = win32gui.GetDlgItem(hwnd, 1)
                    if save_btn:
                        win32gui.PostMessage(save_btn, win32con.BM_CLICK, 0, 0)
                    else:
                        _send_vk(win32con.VK_RETURN)
                    for _ in range(40):
                        if dest.is_file() and dest.stat().st_size > 800:
                            return True
                        time.sleep(0.25)
                except Exception:
                    pass
        time.sleep(0.25)
    return dest.is_file() and dest.stat().st_size > 800


def _native_chrome_save_as_pdf(dest: Path) -> bool:
    """
    Drive Chrome's print preview UI: Destination → Save as PDF → Save → Windows Save As.

    Requires a visible browser (COMMERCEHUB_HEADLESS=false). Tab counts can be tuned via env.
    """
    try:
        import win32con
        import win32gui
    except ImportError:
        _log("WARN: pywin32 not available for native print UI fallback.")
        return False

    hwnd = 0
    for part in ("Print", "Order", "Tractor", "Grainger", "Chrome"):
        hwnd = _find_visible_window(part)
        if hwnd:
            break
    if not hwnd:
        return False

    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass
    time.sleep(0.5)

    tab_to_dest = int(os.environ.get("SPS_PRINT_TAB_TO_DESTINATION", "1"))
    for _ in range(max(0, tab_to_dest)):
        _send_vk(win32con.VK_TAB)
        time.sleep(0.12)

    _send_vk(win32con.VK_RETURN)
    time.sleep(0.35)
    _type_ascii("pdf")
    time.sleep(0.25)
    _send_vk(win32con.VK_RETURN)
    time.sleep(0.5)

    tab_to_save = int(os.environ.get("SPS_PRINT_TAB_TO_SAVE", "0"))
    for _ in range(max(0, tab_to_save)):
        _send_vk(win32con.VK_TAB)
        time.sleep(0.12)
    _send_vk(win32con.VK_RETURN)

    return _fill_save_as_dialog(dest)


def _save_sps_print_pdf(page: Page, context: BrowserContext, dest: Path) -> Path:
    """
    After SPS Print is clicked, wait for Chrome's print preview, then save the PDF.

    Primary path uses CDP printToPDF (equivalent to Save as PDF). If that fails, falls back
    to driving the native print preview (Save as PDF → Save → Windows Save As dialog).
    """
    _click_print_from_ellipsis(page)
    preview_ms = _print_preview_wait_ms()
    _log(f"Waiting {preview_ms / 1000:.0f}s for browser print preview…")
    page.wait_for_timeout(preview_ms)

    for candidate in _order_document_pages(context, page):
        url_hint = (candidate.url or "")[:90]
        _log(f"Trying Save-as-PDF (CDP) on page: {url_hint or '(current)'}")
        if _cdp_print_page_to_pdf(candidate, context, dest):
            _log(f"Saved PDF via print-to-PDF ({dest.stat().st_size:,} bytes)")
            _dismiss_print_preview(page)
            return dest

    _log("CDP print-to-PDF did not produce a valid PDF; trying native print preview UI…")
    if _native_chrome_save_as_pdf(dest):
        _log(f"Saved PDF via native Save as PDF dialog ({dest.name})")
        _dismiss_print_preview(page)
        return dest

    _dismiss_print_preview(page)
    raise RuntimeError(
        "Could not save SPS print preview as PDF. "
        "Run with COMMERCEHUB_HEADLESS=false and ensure the print preview opens."
    )


def _click_combine_csv_and_download(page: Page) -> None:
    clear_click_blockers(page)
    if not click_first_visible(
        page,
        [
            "button[data-testid='bottomPanelDownloadnBtn']",
            "button[aria-label='download']",
            "i.sps-icon-download-cloud",
        ],
        timeout_ms=8000,
    ):
        raise RuntimeError("Could not click bottom-panel download button.")

    page.wait_for_timeout(600)
    clicked_combine = click_first_visible(
        page,
        [
            "label.sps-checkable__label:has-text('Combine documents into one CSV file')",
            "xpath=//label[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'combine documents into one csv file')]",
        ],
        timeout_ms=8000,
    )
    if not clicked_combine:
        for ctx in _contexts(page):
            try:
                lbl = ctx.get_by_text(COMBINE_CSV_RE).first
                if lbl.count() and lbl.is_visible():
                    lbl.click(force=True)
                    clicked_combine = True
                    break
            except Exception:
                continue
    if not clicked_combine:
        raise RuntimeError("Could not select 'Combine documents into one CSV file'.")

    if not click_first_visible(
        page,
        [
            "button[data-testid='modalOkBtn'][title='Download']",
            "button[data-testid='modalOkBtn']:has-text('Download')",
        ],
        timeout_ms=8000,
    ):
        raise RuntimeError("Could not click modal Download button.")


def _open_downloads_tray_and_save(page: Page, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    list_icon_sel = "i.sps-icon.sps-icon-list, i.sps-icon-list"
    cloud_icon_sel = "i.sps-icon.sps-icon-download-cloud, i.sps-icon-download-cloud"

    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        clear_click_blockers(page)
        opened = click_first_visible(
            page,
            [
                f"button:has({list_icon_sel})",
                f"[role='button']:has({list_icon_sel})",
                list_icon_sel,
            ],
            timeout_ms=4000,
        )
        if opened:
            page.wait_for_timeout(900)
            for ctx in _contexts(page):
                try:
                    rows = ctx.locator("div, li, tr").filter(
                        has_text=re.compile(r"Document\s+Download", re.I)
                    )
                    for i in range(min(rows.count(), 10)):
                        row = rows.nth(i)
                        if row.locator(cloud_icon_sel).count() == 0:
                            continue
                        if not row.is_visible():
                            continue
                        with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as dl_info:
                            cloud = row.locator(cloud_icon_sel).first
                            wrap = cloud.locator("xpath=ancestor::button[1]")
                            if wrap.count() > 0:
                                wrap.first.click(force=True)
                            else:
                                cloud.click(force=True)
                        download = dl_info.value
                        download.save_as(str(dest))
                        return dest
                except PlaywrightTimeout:
                    continue
                except Exception:
                    continue
        page.wait_for_timeout(2000)
    raise RuntimeError("Timed out waiting for SPS combined CSV download in notifications tray.")


def pull_sps_retailer(
    page: Page,
    context,
    *,
    retailer_key: str,
    order_date: date | None = None,
) -> tuple[Path | None, Path | None]:
    """Select rows, print PDF, then download combined CSV for one SPS retailer."""
    cfg = RETAILERS[retailer_key]
    count = _select_retailer_rows(page, retailer_key=retailer_key)
    if count == 0:
        _log(f"No {cfg.label} orders in search results; skipping.")
        return None, None
    _log(f"Selected {count} {cfg.label} order row(s).")

    pdf_dest = cfg.pdf_dir / pdf_filename(cfg.label, order_date)
    try:
        _save_sps_print_pdf(page, context, pdf_dest)
        _log(f"Saved PDF {pdf_dest.name}")
    except Exception as exc:
        _log(f"WARN: PDF print/save failed for {cfg.label}: {exc}")
        pdf_dest = None

    csv_dest = cfg.csv_dir / csv_filename(cfg.label, order_date)
    try:
        _click_combine_csv_and_download(page)
        _open_downloads_tray_and_save(page, csv_dest)
        _log(f"Saved CSV {csv_dest.name}")
    except Exception as exc:
        _log(f"WARN: CSV download failed for {cfg.label}: {exc}")
        csv_dest = None
    return pdf_dest, csv_dest


# Grainger first; Tractor Supply last (Order Splitter / warehouse PDFs follow SPS saves).
_SPS_RETAILER_ORDER = ("grainger", "tractor")


def pull_sps_all(page: Page, context, *, order_date: date | None = None) -> dict[str, tuple[Path | None, Path | None]]:
    out: dict[str, tuple[Path | None, Path | None]] = {}
    for i, retailer_key in enumerate(_SPS_RETAILER_ORDER):
        cfg = RETAILERS[retailer_key]
        try:
            open_order_new_search(page)
            out[retailer_key] = pull_sps_retailer(
                page, context, retailer_key=retailer_key, order_date=order_date
            )
        except Exception as exc:
            _log(f"WARN: {cfg.label} pull failed ({exc}); continuing.")
            out[retailer_key] = (None, None)
        if i < len(_SPS_RETAILER_ORDER) - 1:
            page.wait_for_timeout(800)
    return out
