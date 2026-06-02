"""
Home Depot quickship / quickinvoice on CommerceHub using an existing Playwright page
(already logged into Rithum). Mirrors depot_tracking1.py / home_depot_invoice.py logic.
"""
from __future__ import annotations

import os
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path

from playwright.sync_api import Frame, Page

_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from depot_tracking1 import (  # noqa: E402
    MAX_SHIP_PAGES,
    TRACKING_CSV,
    load_tracking_csv,
    lookup_po_tracking,
)
from home_depot_invoice import MAX_INVOICE_PAGES  # noqa: E402
from automation.commercehub_timeouts import (  # noqa: E402
    chain_fast as _chain_fast,
    depot_invoice_ready_timeout_ms,
    depot_queue_probe_timeout_ms,
    depot_ship_list_timeout_ms,
    navigation_timeout_ms,
    poll_interval_ms,
)
from automation.rithum_empty_queue import (  # noqa: E402
    log_rithum_empty_skip as _log_rithum_empty_skip,
    rithum_criteria_empty as _rithum_criteria_empty,
    rithum_empty_queue as _rithum_empty_queue,
    skip_if_rithum_empty as _skip_if_rithum_empty,
)


def _record_skip(step: str, reason: str) -> None:
    try:
        from automation.workflow_run_report import log_and_record_skip

        log_and_record_skip(step, reason)
    except ImportError:
        print(f"{step}: Skipped — {reason}", flush=True)


# thdso summary queue links (relative hrefs on gotoOpenOrders.do?PID=thdso)
_THDSO_SUMMARY_QUEUES: dict[str, dict[str, str | re.Pattern[str]]] = {
    "unacknowledged": {
        "href": (
            "a[href*='merchant=thdso'][href*='web_quickack'][href*='substatus=unacknowledged'], "
            "a[href*='merchant=thdso'][href*='substatus=unacknowledged']"
        ),
        "text": re.compile(r"open\s*/\s*unacknowledged", re.I),
    },
    "accepted": {
        "href": (
            "a[href*='merchant=thdso'][href*='web_quickship'][href*='substatus=accepted'], "
            "a[href*='merchant=thdso'][href*='substatus=accepted']"
        ),
        "text": re.compile(r"open\s*/\s*accepted", re.I),
    },
    "invoicing": {
        "href": (
            "a[href*='merchant=thdso'][href*='web_quickinvoice'], "
            "a[href*='gotoOrderRealmForm.do?action=web_quickinvoice'][href*='merchant=thdso']"
        ),
        "text": re.compile(r"needs\s+invoicing", re.I),
    },
}


def _thdso_summary_queue_link(page: Page, queue: str):
    """Locator for a summary-table queue link (Open / Accepted, etc.)."""
    spec = _THDSO_SUMMARY_QUEUES.get(queue)
    if not spec:
        raise ValueError(f"Unknown thdso summary queue: {queue!r}")
    loc = page.locator(str(spec["href"])).filter(has_text=spec["text"])
    if loc.count() == 0:
        loc = page.locator("a").filter(has_text=spec["text"])
    return loc.first


def _thdso_summary_queue_count(page: Page, queue: str) -> int | None:
    """
    Read the # Orders count from the thdso merchant summary table for a status row.
    ``queue`` is one of: unacknowledged, accepted, invoicing.
    """
    try:
        link = _thdso_summary_queue_link(page, queue)
        if link.count() == 0:
            return None
        row = link.locator("xpath=ancestor::tr[1]")
        if row.count() == 0:
            return None
        for cell in row.locator("td").all():
            txt = (cell.inner_text() or "").strip()
            if txt.isdigit():
                return int(txt)
        for token in re.findall(r"\b(\d+)\b", row.inner_text() or ""):
            return int(token)
    except Exception:
        pass
    return None


def _skip_if_special_order_empty(
    page: Page, step: str, queue: str
) -> bool:
    """
    Skip only when the criteria notification is shown AND the summary row count is 0/missing.
    Avoids false skips on the merchant summary page (orders listed, no form fields yet).
    """
    if not _rithum_criteria_empty(page):
        return False
    on_summary = "gotoopenorders.do?pid=thdso" in (page.url or "").lower()
    if not on_summary:
        _goto(page, SPECIAL_ORDER_SUMMARY_URL)
        page.wait_for_timeout(300 if _chain_fast() else 600)
    count = _thdso_summary_queue_count(page, queue)
    if count is not None and count > 0:
        print(
            f"{step}: criteria notification on direct URL but summary shows {count} order(s); "
            "will open queue from summary."
        )
        return False
    _log_rithum_empty_skip(step)
    return True

ORDER_URL = (
    "https://dsm.commercehub.com/dsm/gotoOrderRealmForm.do?action=web_quickship"
    "&tabContext=web_quickship&status=open&substatus=no-activity&merchant=thehomedepot"
)
INVOICE_URL = (
    "https://dsm.commercehub.com/dsm/gotoOrderRealmForm.do?action=web_quickinvoice"
    "&tabContext=web_quickinvoice&merchant=thehomedepot"
)
SPECIAL_ORDER_SUMMARY_URL = "https://dsm.commercehub.com/dsm/gotoOpenOrders.do?PID=thdso"
SPECIAL_ORDER_QUICKACK_URL = (
    "https://dsm.commercehub.com/dsm/gotoOrderRealmForm.do?action=web_quickack"
    "&tabContext=web_quickack&status=open&substatus=unacknowledged&merchant=thdso"
)
SPECIAL_ORDER_QUICKSHIP_URL = (
    "https://dsm.commercehub.com/dsm/gotoOrderRealmForm.do?action=web_quickship"
    "&tabContext=web_quickship&status=open&substatus=accepted&merchant=thdso"
)
SPECIAL_ORDER_QUICKINVOICE_URL = (
    "https://dsm.commercehub.com/dsm/gotoOrderRealmForm.do?action=web_quickinvoice"
    "&tabContext=web_quickinvoice&merchant=thdso"
)
SPECIAL_ORDER_CONTACT_NAME = "Joey"
SPECIAL_ORDER_SHIPPING_VALUE = "UG"  # UPS Ground


POST_SUBMIT_MS = 500 if _chain_fast() else 1200
SCROLL_WAIT_MS = 250 if _chain_fast() else 600
_POLL_MS = poll_interval_ms()
_NAV_TIMEOUT_MS = navigation_timeout_ms()
# Poll until ship list / invoice UI is ready — returns as soon as controls appear.
_SHIP_LIST_TIMEOUT_MS = depot_ship_list_timeout_ms()
_INVOICE_AUTOFILL_TIMEOUT_MS = depot_invoice_ready_timeout_ms()
_QUEUE_PROBE_TIMEOUT_MS = depot_queue_probe_timeout_ms()


def _goto(page: Page, url: str) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)

# Selectors for invoice Auto Fill (CommerceHub / Home Depot; Lowe's-style variant included).
_INVOICE_AUTOFILL_DISCOVERY = (
    "input[name*='.invoicenumber.autofill']",
    "input[type='button'][name$='.invoicenumber.autofill']",
)
_INVOICE_AUTOFILL_CLICKABLE = (
    "input[name*='.invoicenumber.autofill'], input[type='button'][name$='.invoicenumber.autofill']"
)


def _wait_for_invoice_page_ready(page: Page, timeout_ms: int) -> bool:
    """
    Wait until quickinvoice form controls are attached in any frame.
    This avoids false "queue empty" when Rithum is still rendering next page.
    """
    return _wait_invoice_form_frame(page, timeout_ms) is not None


def _filled_input(page: Page, selector: str) -> bool:
    loc = page.locator(selector).first
    if loc.count() == 0:
        return False
    try:
        return bool((loc.input_value() or "").strip())
    except Exception:
        return False


def _filled_select(page: Page, selector: str) -> bool:
    loc = page.locator(selector).first
    if loc.count() == 0:
        return False
    try:
        v = loc.input_value()
        return bool((v or "").strip())
    except Exception:
        return False


def _wait_for_tracking_page_ready(page: Page, timeout_ms: int) -> bool:
    """
    Wait until tracking form rows are attached or timeout expires.
    Uses short polling so transient slow loads don't look like empty queues.
    """
    deadline = time.monotonic() + (max(1000, timeout_ms) / 1000.0)
    ship_selector = "select[name*='.shippingmethod']"
    po_selector = "a[href*='gotoOrderDetail']"

    while time.monotonic() < deadline:
        try:
            page.wait_for_load_state("domcontentloaded")
        except Exception:
            pass

        if _rithum_empty_queue(page):
            return False

        try:
            if page.locator(ship_selector).count() > 0 or page.locator(po_selector).count() > 0:
                return True
        except Exception:
            # Page may be navigating; keep polling until timeout.
            pass

        page.wait_for_timeout(_POLL_MS)
    return False


def _process_depot_tracking_page(page: Page, tracking_dict: dict) -> bool:
    if _rithum_empty_queue(page):
        _log_rithum_empty_skip("Depot tracking")
        return False

    if not _wait_for_tracking_page_ready(page, _SHIP_LIST_TIMEOUT_MS):
        if _rithum_empty_queue(page):
            _log_rithum_empty_skip("Depot tracking")
            return False
        print(
            f"Depot tracking: timed out waiting {int(_SHIP_LIST_TIMEOUT_MS / 1000)}s; "
            "retrying page load once..."
        )
        try:
            page.reload(wait_until="domcontentloaded")
        except Exception:
            try:
                page.goto(ORDER_URL, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
            except Exception:
                pass

        if _rithum_empty_queue(page):
            _log_rithum_empty_skip("Depot tracking")
            return False
        if not _wait_for_tracking_page_ready(page, _SHIP_LIST_TIMEOUT_MS):
            if _rithum_empty_queue(page):
                _log_rithum_empty_skip("Depot tracking")
            else:
                _record_skip(
                    "Depot tracking",
                    f"No ship list after {int(_SHIP_LIST_TIMEOUT_MS / 1000)}s wait",
                )
            return False

    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(SCROLL_WAIT_MS)

    po_links = page.locator("a[href*='gotoOrderDetail']")
    n = po_links.count()
    if n == 0:
        _record_skip("Depot tracking", "No PO rows on page")
        return False

    touched = False
    matched_po_count = 0
    for i in range(n):
        po_elem = po_links.nth(i)
        try:
            po = po_elem.inner_text().strip().zfill(9)
            if po not in tracking_dict:
                continue
            matched_po_count += 1
            href = po_elem.get_attribute("href") or ""
            if "Hub_PO=" not in href:
                continue
            order_id = href.split("Hub_PO=")[-1]

            ship_sel = f"[id='order({order_id}).box(1).shippingmethod']"
            track_sel = f"[id='order({order_id}).box(1).trackingnumber']"
            qty_inputs = page.locator(
                f"input[name^='order({order_id}).box(1).item'][name$='.shipped']"
            )
            count_qty = qty_inputs.count()
            if count_qty == 0:
                qty_and_ship_done = _filled_select(page, ship_sel)
            else:
                qty_and_ship_done = True
                for j in range(count_qty):
                    try:
                        if not (qty_inputs.nth(j).input_value() or "").strip():
                            qty_and_ship_done = False
                            break
                    except Exception:
                        qty_and_ship_done = False
                        break
                qty_and_ship_done = qty_and_ship_done and _filled_select(page, ship_sel)
            # Do not skip when CSV has tracking but the tracking field is still empty —
            # CommerceHub can show qty + ship method already filled while tracking is blank.
            if qty_and_ship_done and _filled_input(page, track_sel):
                continue

            remaining = page.locator(
                "xpath=//td[contains(@id, 'order("
                + order_id
                + ").box(1).item') and contains(@id, '.remaining')]"
            )
            for j in range(remaining.count()):
                cell = remaining.nth(j)
                qty = (cell.inner_text() or "").strip()
                if not qty.isdigit():
                    continue
                cid = cell.get_attribute("id") or ""
                if not cid.startswith("cell.line."):
                    continue
                shipped_id = cid.replace("cell.line.", "").replace(".remaining", ".shipped")
                ship_box = page.locator(f"[id='{shipped_id}']")
                if ship_box.count() == 0:
                    continue
                try:
                    has_val = bool((ship_box.input_value() or "").strip())
                except Exception:
                    has_val = False
                if not has_val:
                    ship_box.fill("")
                    ship_box.fill(qty)
                    touched = True

            if not _filled_select(page, ship_sel):
                try:
                    page.locator(ship_sel).select_option(label="UPS Ground")
                except Exception:
                    page.locator(ship_sel).fill("UPS Ground")
                touched = True

            if not _filled_input(page, track_sel):
                page.locator(track_sel).fill("")
                page.locator(track_sel).fill(tracking_dict[po])
                touched = True
        except Exception as exc:
            print(f"Depot tracking: error on PO row: {exc}")

    if matched_po_count == 0:
        _record_skip("Depot tracking", "No PO matches in tracking CSV")
        return False

    if not touched:
        print("Depot tracking: matched PO(s) found, values already present; submitting batch...")
    else:
        print("Depot tracking: submitting batch...")
    try:
        # Rithum often keeps long-polling / background requests alive; waiting for
        # "navigation" after click can hit Playwright's default timeout even when
        # the submit actually succeeded. Do not auto-wait for navigation on click.
        page.locator("#confirmbtn").click(no_wait_after=True, timeout=120000)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=120000)
        except Exception:
            pass
        # Explicitly wait until the quickship list is rendered again before
        # returning to the caller; the caller then re-scans PO rows.
        if not _wait_for_tracking_page_ready(page, _SHIP_LIST_TIMEOUT_MS):
            if _rithum_empty_queue(page):
                return False
            print(
                f"Depot tracking: submit returned but ship list not ready after "
                f"{int(_SHIP_LIST_TIMEOUT_MS / 1000)}s."
            )
            return False
        page.wait_for_timeout(500 if _chain_fast() else 900)
        return True
    except Exception as exc:
        print(f"Depot tracking: submit failed: {exc}")
        return False


def run_depot_tracking_with_page(page: Page, tracking_csv_path: str | None = None) -> None:
    path = tracking_csv_path or TRACKING_CSV
    tracking_dict = load_tracking_csv(str(path))
    page.goto(ORDER_URL, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
    page.wait_for_timeout(250 if _chain_fast() else 500)
    if _rithum_empty_queue(page):
        _log_rithum_empty_skip("Depot tracking")
        return

    for batch in range(1, MAX_SHIP_PAGES + 1):
        if not _process_depot_tracking_page(page, tracking_dict):
            print("Depot tracking: finished.")
            break
        print(f"Depot tracking: submitted batch {batch}.")
        page.wait_for_timeout(POST_SUBMIT_MS)
    else:
        print(f"Depot tracking: stopped after {MAX_SHIP_PAGES} batches (safety cap).")


def _special_order_estimated_delivery_date() -> str:
    """Today + 5 calendar days, MM/DD/YYYY (e.g. May 29 -> June 3)."""
    return (date.today() + timedelta(days=5)).strftime("%m/%d/%Y")


_PO_NUMBER_RE = re.compile(
    r"PO\s*(?:Number|#)?\s*:?\s*([0-9]{1,4}[_\-\s]+[0-9]{5,}|[0-9]{8,})",
    re.I,
)


def _lookup_special_order_tracking(tracking_dict: dict[str, str], po_raw: str) -> str | None:
    return lookup_po_tracking(tracking_dict, po_raw)


def _po_text_from_order_link(page: Page, po_elem) -> str:
    """PO as shown on thdso quickship (link text or PO Number: line in the order block)."""
    po = (po_elem.inner_text() or "").strip()
    if po and po.lower() not in ("detail", "view", "order"):
        return po
    for xpath in (
        "xpath=ancestor::table[1]",
        "xpath=ancestor::tr[1]",
        "xpath=ancestor::div[contains(@class,'order')][1]",
    ):
        try:
            block = po_elem.locator(xpath)
            if block.count() == 0:
                continue
            text = block.first.inner_text(timeout=3_000) or ""
            match = _PO_NUMBER_RE.search(text)
            if match:
                return match.group(1).strip()
        except Exception:
            continue
    return po


def _special_order_tracking_input(page: Page, order_id: str):
    """thdso may use box(1) or a single order-level tracking field."""
    for sel in (
        f"input[name='order({order_id}).box(1).trackingnumber']",
        f"input[id='order({order_id}).box(1).trackingnumber']",
        f"input[name*='order({order_id})'][name*='trackingnumber']",
        f"input[id*='order({order_id})'][id*='trackingnumber']",
    ):
        loc = page.locator(sel)
        if loc.count() > 0:
            return loc.first
    return None


def _wait_for_special_order_ack_page_ready(page: Page, timeout_ms: int) -> bool:
    deadline = time.monotonic() + max(1000, timeout_ms) / 1000.0
    while time.monotonic() < deadline:
        try:
            page.wait_for_load_state("domcontentloaded")
        except Exception:
            pass
        try:
            if page.locator("td.or_sku[id*='vendorSku'], td[id*='vendorSku']").count() > 0:
                return True
            if page.locator("input[name*='.contactInfo']").count() > 0:
                return True
            body = (page.inner_text("body") or "").lower()
            if "special orders" in body and "no orders" in body:
                return False
            if _rithum_criteria_empty(page):
                return False
        except Exception:
            pass
        page.wait_for_timeout(_POLL_MS)
    return False


def _open_depot_special_order_quickack(page: Page) -> bool:
    """Navigate to thdso Open/Unacknowledged quickack. Returns False when queue is empty."""
    print("Depot Special Orders: opening Open/Unacknowledged acknowledgment queue...")
    _goto(page, SPECIAL_ORDER_QUICKACK_URL)
    page.wait_for_timeout(250 if _chain_fast() else 500)
    if _wait_for_special_order_ack_page_ready(page, _QUEUE_PROBE_TIMEOUT_MS):
        return True

    _goto(page, SPECIAL_ORDER_SUMMARY_URL)
    page.wait_for_timeout(300 if _chain_fast() else 600)
    unack_count = _thdso_summary_queue_count(page, "unacknowledged")
    if unack_count == 0:
        _record_skip("Depot Special Orders ack", "Summary shows 0 unacknowledged orders")
        return False

    open_unack = _thdso_summary_queue_link(page, "unacknowledged")
    if open_unack.count() == 0:
        _record_skip("Depot Special Orders ack", "No Open/Unacknowledged link")
        return False
    try:
        open_unack.click()
        page.wait_for_load_state("domcontentloaded")
    except Exception as exc:
        print(f"Depot Special Orders ack: could not open queue ({exc}); skipping.")
        return False

    if not _wait_for_special_order_ack_page_ready(page, _QUEUE_PROBE_TIMEOUT_MS):
        if _skip_if_special_order_empty(page, "Depot Special Orders ack", "unacknowledged"):
            return False
        _record_skip("Depot Special Orders ack", "Queue not ready")
        return False
    return True


def _parse_order_id_from_token(token: str) -> str | None:
    match = re.search(r"order\((\d+)\)", token or "")
    return match.group(1) if match else None


def _group_vendor_skus_by_order(page: Page) -> dict[str, list[str]]:
    by_order: dict[str, list[str]] = {}
    cells = page.locator("td.or_sku[id*='vendorSku'], td[id*='.vendorSku']")
    for i in range(cells.count()):
        cell = cells.nth(i)
        cell_id = cell.get_attribute("id") or ""
        order_id = _parse_order_id_from_token(cell_id)
        if not order_id:
            continue
        sku = (cell.inner_text() or "").strip()
        if not sku:
            continue
        bucket = by_order.setdefault(order_id, [])
        if sku not in bucket:
            bucket.append(sku)
    return by_order


def _special_order_ack_order_ids(page: Page) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for sku_order in _group_vendor_skus_by_order(page):
        if sku_order not in seen:
            seen.add(sku_order)
            ids.append(sku_order)
    for sel in ("input[name*='.contactInfo']", "input[id*='contactInfo']"):
        loc = page.locator(sel)
        for i in range(loc.count()):
            token = (loc.nth(i).get_attribute("name") or loc.nth(i).get_attribute("id") or "").strip()
            order_id = _parse_order_id_from_token(token)
            if order_id and order_id not in seen:
                seen.add(order_id)
                ids.append(order_id)
    return ids


def _process_depot_special_order_ack_page(page: Page, vendor_map: dict[str, str]) -> bool:
    from automation.worldship_vendor_map import is_sku_in_vendor_map

    if _skip_if_special_order_empty(page, "Depot Special Orders ack", "unacknowledged"):
        return False
    if not _wait_for_special_order_ack_page_ready(page, _SHIP_LIST_TIMEOUT_MS):
        if _skip_if_special_order_empty(page, "Depot Special Orders ack", "unacknowledged"):
            return False
        _record_skip("Depot Special Orders ack", "Timed out waiting for order list")
        return False

    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(SCROLL_WAIT_MS)

    skus_by_order = _group_vendor_skus_by_order(page)
    order_ids = _special_order_ack_order_ids(page)
    if not order_ids:
        _record_skip("Depot Special Orders ack", "No orders on page")
        return False

    touched = False
    ack_count = 0
    skip_count = 0

    for order_id in order_ids:
        skus = skus_by_order.get(order_id, [])
        if not skus:
            print(f"Depot Special Orders ack: order {order_id} has no vendor SKU cells; skipping.")
            skip_count += 1
            continue
        if not any(is_sku_in_vendor_map(sku, vendor_map) for sku in skus):
            print(
                f"Depot Special Orders ack: order {order_id} SKU(s) {skus!r} not in vendor map; skipping."
            )
            skip_count += 1
            continue

        contact_sel = (
            f"input[name='order({order_id}).contactInfo'], "
            f"input[id*='order({order_id}).contactInfo']"
        )
        contact = page.locator(contact_sel).first
        if contact.count() == 0:
            print(f"Depot Special Orders ack: no Supplier Contact field for order {order_id}; skipping.")
            skip_count += 1
            continue

        if not _filled_input(page, contact_sel):
            contact.fill("")
            contact.fill(SPECIAL_ORDER_CONTACT_NAME)
            touched = True
        ack_count += 1

        checkbox = page.locator(
            f"input[type='checkbox'][name='order({order_id})'], "
            f"input[type='checkbox'][id*='order({order_id})']"
        ).first
        try:
            if checkbox.count() > 0 and not checkbox.is_checked():
                checkbox.check()
                touched = True
        except Exception:
            pass

    if ack_count == 0:
        print("Depot Special Orders ack: no orders matched vendor SKUs on this page.")
        return False

    if not touched:
        print("Depot Special Orders ack: matched orders already filled; submitting batch...")
    else:
        print(f"Depot Special Orders ack: submitting {ack_count} order(s) ({skip_count} skipped)...")

    try:
        page.locator("#confirmbtn, input#confirmbtn[name='confirmbtn']").first.click(
            no_wait_after=True, timeout=120_000
        )
        try:
            page.wait_for_load_state("domcontentloaded", timeout=120_000)
        except Exception:
            pass
        if not _wait_for_special_order_ack_page_ready(page, _SHIP_LIST_TIMEOUT_MS):
            if _rithum_criteria_empty(page):
                return False
        page.wait_for_timeout(500 if _chain_fast() else 900)
        return True
    except Exception as exc:
        print(f"Depot Special Orders ack: submit failed: {exc}")
        return False


def run_depot_special_order_acknowledgment_with_page(page: Page) -> None:
    """Acknowledge thdso orders whose vendor SKU is in our vendor map (contact name Joey)."""
    try:
        from automation.worldship_vendor_map import load_vendor_map

        vendor_map = load_vendor_map(retailer_key="thdso")
    except Exception as exc:
        print(f"Depot Special Orders ack: could not load vendor map ({exc}); skipping.")
        return

    if not _open_depot_special_order_quickack(page):
        return

    for batch in range(1, MAX_SHIP_PAGES + 1):
        if not _process_depot_special_order_ack_page(page, vendor_map):
            print("Depot Special Orders ack: finished.")
            break
        print(f"Depot Special Orders ack: submitted batch {batch}.")
        page.wait_for_timeout(POST_SUBMIT_MS)
    else:
        print(f"Depot Special Orders ack: stopped after {MAX_SHIP_PAGES} batches (safety cap).")


def _wait_for_special_order_page_ready(page: Page, timeout_ms: int) -> bool:
    deadline = time.monotonic() + max(1000, timeout_ms) / 1000.0
    while time.monotonic() < deadline:
        try:
            page.wait_for_load_state("domcontentloaded")
        except Exception:
            pass
        try:
            if page.locator("a[href*='gotoOrderDetail']").count() > 0:
                return True
            if page.locator("select[name*='.shippingmethod']").count() > 0:
                return True
            body = (page.inner_text("body") or "").lower()
            if "special orders" in body and "no orders" in body:
                return False
            if _rithum_criteria_empty(page):
                return False
        except Exception:
            pass
        page.wait_for_timeout(_POLL_MS)
    return False


def _open_depot_special_order_quickship(page: Page) -> bool:
    """Navigate to thdso Open/Accepted quickship. Returns False when queue is empty."""
    print("Depot Special Orders: opening Open/Accepted quickship queue...")
    _goto(page, SPECIAL_ORDER_QUICKSHIP_URL)
    page.wait_for_timeout(250 if _chain_fast() else 500)
    if _wait_for_special_order_page_ready(page, _QUEUE_PROBE_TIMEOUT_MS):
        return True

    _goto(page, SPECIAL_ORDER_SUMMARY_URL)
    page.wait_for_timeout(300 if _chain_fast() else 600)
    accepted_count = _thdso_summary_queue_count(page, "accepted")
    if accepted_count == 0:
        _record_skip("Depot Special Orders tracking", "Summary shows 0 accepted orders")
        return False

    open_accepted = _thdso_summary_queue_link(page, "accepted")
    if open_accepted.count() == 0:
        _record_skip("Depot Special Orders tracking", "No Open/Accepted link")
        return False
    try:
        open_accepted.click()
        page.wait_for_load_state("domcontentloaded")
    except Exception as exc:
        print(f"Depot Special Orders: could not open Accepted queue ({exc}); skipping.")
        return False

    if not _wait_for_special_order_page_ready(page, _QUEUE_PROBE_TIMEOUT_MS):
        if _skip_if_special_order_empty(page, "Depot Special Orders tracking", "accepted"):
            return False
        _record_skip("Depot Special Orders tracking", "Queue not ready")
        return False
    return True


def _process_depot_special_order_page(page: Page, tracking_dict: dict[str, str]) -> bool:
    if _skip_if_special_order_empty(page, "Depot Special Orders tracking", "accepted"):
        return False
    if not _wait_for_special_order_page_ready(page, _SHIP_LIST_TIMEOUT_MS):
        if _skip_if_special_order_empty(page, "Depot Special Orders tracking", "accepted"):
            return False
        _record_skip("Depot Special Orders tracking", "Timed out waiting for order list")
        return False

    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(SCROLL_WAIT_MS)

    po_links = page.locator("a.simple_link[href*='gotoOrderDetail'], a[href*='gotoOrderDetail']")
    n = po_links.count()
    if n == 0:
        _record_skip("Depot Special Orders tracking", "No PO rows on page")
        return False

    est_date = _special_order_estimated_delivery_date()
    touched = False
    matched_po_count = 0

    for i in range(n):
        po_elem = po_links.nth(i)
        try:
            po = _po_text_from_order_link(page, po_elem)
            tracking = _lookup_special_order_tracking(tracking_dict, po)
            if not tracking:
                print(f"Depot Special Orders: no CSV match for PO {po!r}; skipping.")
                continue
            matched_po_count += 1
            href = po_elem.get_attribute("href") or ""
            if "Hub_PO=" not in href:
                print(f"Depot Special Orders: PO {po!r} link has no Hub_PO; skipping.")
                continue
            order_id = href.split("Hub_PO=")[-1].split("&")[0]

            track_input = _special_order_tracking_input(page, order_id)
            bol_sel = f"input[name='order({order_id}).box(1).billOfLading'], input[id*='order({order_id})'][id*='billOfLading']"
            contact_sel = f"input[name='order({order_id}).contactInfo'], input[id*='order({order_id}).contactInfo']"
            ship_sel = f"select[name='order({order_id}).box(1).shippingmethod'], select[id*='order({order_id})'][id*='shippingmethod']"

            qty_inputs = page.locator(
                f"input[name^='order({order_id}).box(1).item'][name$='.shipped']"
            )
            for j in range(qty_inputs.count()):
                qty_box = qty_inputs.nth(j)
                try:
                    if (qty_box.input_value() or "").strip():
                        continue
                except Exception:
                    pass
                remaining = page.locator(
                    f"xpath=//td[contains(@id, 'order({order_id}).box(1).item') "
                    f"and contains(@id, '.remaining')]"
                ).nth(j)
                try:
                    qty = (remaining.inner_text() or "").strip()
                    if qty.replace(".", "", 1).isdigit():
                        qty_box.fill("")
                        qty_box.fill(qty.split(".")[0] if "." in qty else qty)
                        touched = True
                except Exception:
                    continue

            if not _filled_select(page, ship_sel):
                try:
                    page.locator(ship_sel).select_option(value=SPECIAL_ORDER_SHIPPING_VALUE)
                except Exception:
                    try:
                        page.locator(ship_sel).select_option(label="UPS Ground")
                    except Exception:
                        page.locator(ship_sel).fill("UPS Ground")
                touched = True

            contact = page.locator(contact_sel).first
            if contact.count() > 0 and not _filled_input(page, contact_sel):
                contact.fill("")
                contact.fill(SPECIAL_ORDER_CONTACT_NAME)
                touched = True

            if track_input is not None:
                try:
                    current = (track_input.input_value() or "").strip()
                except Exception:
                    current = ""
                if not current:
                    track_input.fill("")
                    track_input.fill(tracking)
                    touched = True
                    print(f"Depot Special Orders: filled tracking for PO {po!r} → {tracking}")
            else:
                print(
                    f"Depot Special Orders: WARN: no tracking field found for PO {po!r} "
                    f"(order {order_id}); CSV had {tracking!r}."
                )

            bol = page.locator(bol_sel).first
            if bol.count() > 0:
                try:
                    if not (bol.input_value() or "").strip():
                        bol.fill("")
                        bol.fill(tracking)
                        touched = True
                except Exception:
                    pass

            est_inputs = page.locator(
                f"input[name^='order({order_id}).box(1).item'][name$='.estimatedDeliveryDate']"
            )
            for j in range(est_inputs.count()):
                est = est_inputs.nth(j)
                try:
                    if (est.input_value() or "").strip():
                        continue
                except Exception:
                    pass
                est.fill("")
                est.fill(est_date)
                touched = True
        except Exception as exc:
            print(f"Depot Special Orders: error on PO {po!r}: {exc}")

    if matched_po_count == 0:
        _record_skip("Depot Special Orders tracking", "No PO matches in tracking CSV")
        return False

    if not touched:
        print("Depot Special Orders: matched PO(s) already filled; submitting batch...")
    else:
        print("Depot Special Orders: submitting batch...")

    try:
        page.locator("#confirmbtn").click(no_wait_after=True, timeout=120000)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=120000)
        except Exception:
            pass
        if not _wait_for_special_order_page_ready(page, _SHIP_LIST_TIMEOUT_MS):
            if _rithum_criteria_empty(page):
                return False
            return False
        page.wait_for_timeout(500 if _chain_fast() else 900)
        return True
    except Exception as exc:
        print(f"Depot Special Orders tracking: submit failed: {exc}")
        return False


def run_depot_special_order_tracking_with_page(
    page: Page, tracking_csv_path: str | None = None
) -> None:
    """Home Depot Special Orders (thdso): acknowledge unacknowledged, then Open/Accepted tracking."""
    run_depot_special_order_acknowledgment_with_page(page)

    path = tracking_csv_path or TRACKING_CSV
    tracking_dict = load_tracking_csv(str(path))
    if not tracking_dict:
        print(f"Depot Special Orders: no tracking rows loaded from {path}; skipping.")
        return
    print(f"Depot Special Orders: loaded {len(tracking_dict)} tracking lookup key(s) from {path}")

    if not _open_depot_special_order_quickship(page):
        return

    for batch in range(1, MAX_SHIP_PAGES + 1):
        if not _process_depot_special_order_page(page, tracking_dict):
            print("Depot Special Orders: finished.")
            break
        print(f"Depot Special Orders: submitted batch {batch}.")
        page.wait_for_timeout(POST_SUBMIT_MS)
    else:
        print(f"Depot Special Orders: stopped after {MAX_SHIP_PAGES} batches (safety cap).")


def _wait_for_special_order_invoice_page_ready(page: Page, timeout_ms: int) -> bool:
    deadline = time.monotonic() + max(1000, timeout_ms) / 1000.0
    while time.monotonic() < deadline:
        try:
            page.wait_for_load_state("domcontentloaded")
        except Exception:
            pass
        if _rithum_criteria_empty(page):
            return False
        try:
            if page.locator("input[name$='.invoicenumber.autofill']").count() > 0:
                return True
            if page.locator("select[name$='.termstypecode']").count() > 0:
                return True
        except Exception:
            pass
        page.wait_for_timeout(_POLL_MS)
    return False


def _open_depot_special_order_quickinvoice(page: Page) -> bool:
    """Navigate to thdso Needs Invoicing queue. Returns False when queue is empty."""
    print("Depot Special Orders: opening Needs Invoicing queue...")
    _goto(page, SPECIAL_ORDER_QUICKINVOICE_URL)
    page.wait_for_timeout(250 if _chain_fast() else 500)
    if _wait_for_special_order_invoice_page_ready(page, _QUEUE_PROBE_TIMEOUT_MS):
        return True

    _goto(page, SPECIAL_ORDER_SUMMARY_URL)
    page.wait_for_timeout(300 if _chain_fast() else 600)
    inv_count = _thdso_summary_queue_count(page, "invoicing")
    if inv_count == 0:
        _record_skip("Depot Special Orders invoicing", "Summary shows 0 needs invoicing")
        return False
    needs_inv = _thdso_summary_queue_link(page, "invoicing")
    if needs_inv.count() == 0:
        _record_skip("Depot Special Orders invoicing", "No Needs Invoicing link")
        return False
    try:
        needs_inv.click()
        page.wait_for_load_state("domcontentloaded")
    except Exception as exc:
        print(f"Depot Special Orders invoicing: could not open queue ({exc}); skipping.")
        return False

    if not _wait_for_special_order_invoice_page_ready(page, _QUEUE_PROBE_TIMEOUT_MS):
        if _skip_if_special_order_empty(page, "Depot Special Orders invoicing", "invoicing"):
            return False
        _record_skip("Depot Special Orders invoicing", "Queue not ready")
        return False
    return True


def _special_order_invoice_order_ids(page: Page) -> list[str]:
    """Hub order ids present on the current Needs Invoicing page."""
    ids: list[str] = []
    seen: set[str] = set()
    for sel in (
        "input[name$='.invoicenumber.autofill']",
        "select[name$='.termstypecode']",
    ):
        loc = page.locator(sel)
        for i in range(loc.count()):
            node = loc.nth(i)
            token = (node.get_attribute("name") or node.get_attribute("id") or "").strip()
            match = re.search(r"order\((\d+)\)", token)
            if not match:
                continue
            oid = match.group(1)
            if oid in seen:
                continue
            seen.add(oid)
            ids.append(oid)
    return ids


def _fill_special_order_invoice_order(page: Page, order_id: str) -> bool:
    """Fill terms + Auto Fill invoice number for one Special Order on the invoice page."""
    touched = False

    terms_type = page.locator(f"[id='order({order_id}).termstypecode']")
    if terms_type.count() > 0 and not _filled_select(page, f"[id='order({order_id}).termstypecode']"):
        try:
            terms_type.select_option(value="01")
        except Exception:
            terms_type.select_option(label="01: Basic")
        touched = True

    disc_pct = page.locator(f"[id='order({order_id}).termsdiscountpercent']")
    if disc_pct.count() > 0 and not _filled_input(page, f"[id='order({order_id}).termsdiscountpercent']"):
        disc_pct.fill("")
        disc_pct.fill("1")
        touched = True

    net_days = page.locator(f"[id='order({order_id}).termsnetdaysdue']")
    if net_days.count() > 0 and not _filled_input(page, f"[id='order({order_id}).termsnetdaysdue']"):
        net_days.fill("")
        net_days.fill("30")
        touched = True

    autofill = page.locator(f"[id='order({order_id}).invoicenumber.autofill']")
    if autofill.count() > 0:
        try:
            autofill.click()
            touched = True
        except Exception:
            pass

    basis = page.locator(f"[id='order({order_id}).termsBasisDateCode']")
    if basis.count() > 0 and not _filled_select(page, f"[id='order({order_id}).termsBasisDateCode']"):
        try:
            basis.select_option(value="3")
        except Exception:
            basis.select_option(label="3: Invoice Date")
        touched = True

    disc_days = page.locator(f"[id='order({order_id}).termsdiscountdaysdue']")
    if disc_days.count() > 0 and not _filled_input(page, f"[id='order({order_id}).termsdiscountdaysdue']"):
        disc_days.fill("")
        disc_days.fill("29")
        touched = True

    return touched


def _fill_special_order_invoice_quantities(page: Page) -> int:
    """Copy invoiceable qty into invoiced fields for all orders on the page."""
    filled = 0
    cells = page.locator("td[id$='.invoiceable']")
    for i in range(cells.count()):
        try:
            cell = cells.nth(i)
            qty = (cell.inner_text() or "").strip()
            if not qty.isdigit():
                continue
            cid = cell.get_attribute("id") or ""
            input_id = cid.replace("cell.line.", "").replace(".invoiceable", ".invoiced")
            box = page.locator(f"[id='{input_id}']")
            if box.count() == 0:
                continue
            try:
                if (box.input_value() or "").strip():
                    continue
            except Exception:
                pass
            box.fill("")
            box.fill(qty)
            filled += 1
        except Exception:
            continue
    return filled


def _process_depot_special_order_invoice_page(page: Page) -> bool:
    if _skip_if_special_order_empty(page, "Depot Special Orders invoicing", "invoicing"):
        return False
    if not _wait_for_special_order_invoice_page_ready(page, _INVOICE_AUTOFILL_TIMEOUT_MS):
        if _skip_if_special_order_empty(page, "Depot Special Orders invoicing", "invoicing"):
            return False
        _record_skip("Depot Special Orders invoicing", "Timed out waiting for invoice page")
        return False

    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(SCROLL_WAIT_MS)

    order_ids = _special_order_invoice_order_ids(page)
    if not order_ids:
        _record_skip("Depot Special Orders invoicing", "No orders on page")
        return False

    touched = False
    for order_id in order_ids:
        if _fill_special_order_invoice_order(page, order_id):
            touched = True
            print(f"Depot Special Orders invoicing: prepared order {order_id}.")

    qty_filled = _fill_special_order_invoice_quantities(page)
    if qty_filled > 0:
        touched = True
        print(f"Depot Special Orders invoicing: filled invoice qty on {qty_filled} line(s).")

    if not touched:
        print("Depot Special Orders invoicing: orders already filled; submitting batch...")

    print("Depot Special Orders invoicing: submitting...")
    try:
        page.locator("#confirmbtn").click(no_wait_after=True, timeout=120000)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=120000)
        except Exception:
            pass
        if not _wait_for_special_order_invoice_page_ready(page, _INVOICE_AUTOFILL_TIMEOUT_MS):
            if _rithum_criteria_empty(page):
                return False
            return False
        page.wait_for_timeout(500 if _chain_fast() else 900)
        return True
    except Exception as exc:
        print(f"Depot Special Orders invoicing: submit failed: {exc}")
        return False


def run_depot_special_order_invoicing_with_page(page: Page) -> None:
    """Home Depot Special Orders (thdso): Needs Invoicing; skips quietly when queue is empty."""
    if not _open_depot_special_order_quickinvoice(page):
        return

    for batch in range(1, MAX_INVOICE_PAGES + 1):
        if not _process_depot_special_order_invoice_page(page):
            print("Depot Special Orders invoicing: finished.")
            break
        print(f"Depot Special Orders invoicing: submitted batch {batch}.")
        page.wait_for_timeout(POST_SUBMIT_MS)
    else:
        print(f"Depot Special Orders invoicing: stopped after {MAX_INVOICE_PAGES} batches (safety cap).")


def _wait_invoice_form_frame(page: Page, timeout_ms: int) -> Frame | None:
    """Return the frame (main or iframe) that contains quickinvoice Auto Fill inputs."""
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        if _rithum_empty_queue(page):
            return None
        for frame in page.frames:
            try:
                if frame.is_detached():
                    continue
            except Exception:
                continue
            for sel in _INVOICE_AUTOFILL_DISCOVERY:
                loc = frame.locator(sel).first
                try:
                    loc.wait_for(state="attached", timeout=600)
                    return frame
                except Exception:
                    continue
        page.wait_for_timeout(150)
    return None


def _process_depot_invoice_page(page: Page) -> bool:
    if _rithum_empty_queue(page):
        _log_rithum_empty_skip("Depot invoicing")
        return False

    if not _wait_for_invoice_page_ready(page, _QUEUE_PROBE_TIMEOUT_MS):
        if _rithum_empty_queue(page):
            _log_rithum_empty_skip("Depot invoicing")
            return False

    if not _wait_for_invoice_page_ready(page, _INVOICE_AUTOFILL_TIMEOUT_MS):
        if _rithum_empty_queue(page):
            _log_rithum_empty_skip("Depot invoicing")
            return False
        print(
            f"Depot invoicing: timed out waiting {int(_INVOICE_AUTOFILL_TIMEOUT_MS / 1000)}s; "
            "retrying page load once..."
        )
        try:
            page.reload(wait_until="domcontentloaded")
        except Exception:
            try:
                _goto(page, INVOICE_URL)
            except Exception:
                pass
        if _rithum_empty_queue(page):
            _log_rithum_empty_skip("Depot invoicing")
            return False
        if not _wait_for_invoice_page_ready(page, _INVOICE_AUTOFILL_TIMEOUT_MS):
            if _rithum_empty_queue(page):
                _log_rithum_empty_skip("Depot invoicing")
            else:
                _record_skip("Depot invoicing", "No invoice rows or queue empty")
            return False

    invoice_frame = _wait_invoice_form_frame(page, _INVOICE_AUTOFILL_TIMEOUT_MS)
    if invoice_frame is None:
        _record_skip("Depot invoicing", "No invoice form controls found")
        return False

    page.wait_for_timeout(200 if _chain_fast() else 400)
    buttons = invoice_frame.locator(_INVOICE_AUTOFILL_CLICKABLE)
    for i in range(buttons.count()):
        try:
            buttons.nth(i).click()
            page.wait_for_timeout(100)
        except Exception:
            continue

    for sel in (
        "input[name*='.termsnetdaysdue']",
        "input[name*='.termsdiscountpercent']",
        "input[name*='.termsdiscountdaysdue']",
    ):
        locs = invoice_frame.locator(sel)
        for j in range(locs.count()):
            node = locs.nth(j)
            if "netdays" in sel or "discountdays" in sel:
                node.fill("")
                node.fill("30")
            else:
                node.fill("")
                node.fill("1")

    cells = invoice_frame.locator("td[id$='.invoiceable']")
    for i in range(cells.count()):
        try:
            cell = cells.nth(i)
            qty = (cell.inner_text() or "").strip()
            if not qty.isdigit():
                continue
            cid = cell.get_attribute("id") or ""
            input_id = cid.replace("cell.line.", "").replace(".invoiceable", ".invoiced")
            box = invoice_frame.locator(f"[id='{input_id}']")
            box.fill("")
            box.fill(qty)
        except Exception:
            continue

    print("Depot invoicing: submitting...")
    try:
        confirm = invoice_frame.locator("#confirmbtn").first
        if confirm.count() == 0:
            confirm = page.locator("#confirmbtn").first
        confirm.click(no_wait_after=True, timeout=120000)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=120000)
        except Exception:
            pass
        # Ensure the next invoice page is actually rendered before next loop cycle.
        if not _wait_for_invoice_page_ready(page, _INVOICE_AUTOFILL_TIMEOUT_MS):
            print(
                f"Depot invoicing: submit returned but next page not ready after "
                f"{int(_INVOICE_AUTOFILL_TIMEOUT_MS / 1000)}s."
            )
            return False
        page.wait_for_timeout(500 if _chain_fast() else 900)
        return True
    except Exception:
        print("Depot invoicing: submit not found.")
        return False


def run_depot_invoicing_with_page(page: Page) -> None:
    _goto(page, INVOICE_URL)
    page.wait_for_timeout(250 if _chain_fast() else 500)
    if _rithum_empty_queue(page):
        _log_rithum_empty_skip("Depot invoicing")
        return

    for batch in range(1, MAX_INVOICE_PAGES + 1):
        if not _process_depot_invoice_page(page):
            print("Depot invoicing: finished.")
            break
        print(f"Depot invoicing: submitted batch {batch}.")
        page.wait_for_timeout(POST_SUBMIT_MS)
    else:
        print(f"Depot invoicing: stopped after {MAX_INVOICE_PAGES} batches (safety cap).")
