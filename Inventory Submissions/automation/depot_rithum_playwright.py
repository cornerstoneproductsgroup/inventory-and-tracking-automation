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

from depot_tracking1 import MAX_SHIP_PAGES, TRACKING_CSV, load_tracking_csv  # noqa: E402
from home_depot_invoice import MAX_INVOICE_PAGES  # noqa: E402

ORDER_URL = (
    "https://dsm.commercehub.com/dsm/gotoOrderRealmForm.do?action=web_quickship"
    "&tabContext=web_quickship&status=open&substatus=no-activity&merchant=thehomedepot"
)
INVOICE_URL = (
    "https://dsm.commercehub.com/dsm/gotoOrderRealmForm.do?action=web_quickinvoice"
    "&tabContext=web_quickinvoice&merchant=thehomedepot"
)
SPECIAL_ORDER_SUMMARY_URL = "https://dsm.commercehub.com/dsm/gotoOpenOrders.do?PID=thdso"
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


def _chain_fast() -> bool:
    return os.environ.get("COMMERCEHUB_CHAIN_FAST") == "1"


POST_SUBMIT_MS = 500 if _chain_fast() else 1200
SCROLL_WAIT_MS = 250 if _chain_fast() else 600
# Max wait for ship list UI before treating queue as empty.
# Rithum can intermittently take 30s+ after submit/navigation.
_SHIP_LIST_TIMEOUT_MS = int(
    os.environ.get(
        "DEPOT_SHIP_LIST_TIMEOUT_MS",
        "90000" if _chain_fast() else "240000",
    )
)
# Quickinvoice often loads slower than quickship (large table after navigation).
# commercehub_chain.py sets COMMERCEHUB_CHAIN_FAST=1; a 5.5s cap caused false
# "queue empty" when rows existed — align closer to home_depot_invoice.py (30s wait).
_INVOICE_AUTOFILL_TIMEOUT_MS = 30000 if _chain_fast() else 22000

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

        try:
            if page.locator(ship_selector).count() > 0 or page.locator(po_selector).count() > 0:
                return True
        except Exception:
            # Page may be navigating; keep polling until timeout.
            pass

        page.wait_for_timeout(400 if _chain_fast() else 700)
    return False


def _process_depot_tracking_page(page: Page, tracking_dict: dict) -> bool:
    if not _wait_for_tracking_page_ready(page, _SHIP_LIST_TIMEOUT_MS):
        print(
            f"Depot tracking: timed out waiting {int(_SHIP_LIST_TIMEOUT_MS / 1000)}s; "
            "retrying page load once..."
        )
        try:
            page.reload(wait_until="domcontentloaded")
        except Exception:
            # If reload fails due to navigation state, do a direct goto fallback.
            try:
                page.goto(ORDER_URL, wait_until="domcontentloaded")
            except Exception:
                pass

        if not _wait_for_tracking_page_ready(page, _SHIP_LIST_TIMEOUT_MS):
            print(
                f"Depot tracking: no ship list after retry and {int(_SHIP_LIST_TIMEOUT_MS / 1000)}s wait; "
                "moving on."
            )
            return False

    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(SCROLL_WAIT_MS)

    po_links = page.locator("a[href*='gotoOrderDetail']")
    n = po_links.count()
    if n == 0:
        print("Depot tracking: no PO rows on page; moving on.")
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
        print("Depot tracking: loaded page has no PO matches in CSV; moving on to invoicing.")
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
    page.goto(ORDER_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(250 if _chain_fast() else 500)

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


def _lookup_special_order_tracking(tracking_dict: dict[str, str], po_raw: str) -> str | None:
    po = (po_raw or "").strip()
    if not po:
        return None
    candidates = {po, po.upper(), po.lower(), po.replace(" ", "")}
    if po.isdigit():
        candidates.add(po.zfill(9))
    for key in candidates:
        hit = tracking_dict.get(key)
        if hit:
            return hit
    return None


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
        except Exception:
            pass
        page.wait_for_timeout(400 if _chain_fast() else 700)
    return False


def _open_depot_special_order_quickship(page: Page) -> bool:
    """Navigate to thdso Open/Accepted quickship. Returns False when queue is empty."""
    print("Depot Special Orders: opening Open/Accepted quickship queue...")
    page.goto(SPECIAL_ORDER_QUICKSHIP_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(250 if _chain_fast() else 500)
    if _wait_for_special_order_page_ready(page, 15_000):
        return True

    page.goto(SPECIAL_ORDER_SUMMARY_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(300 if _chain_fast() else 600)
    summary_link = page.locator("a[href*='gotoOpenOrders.do?PID=thdso']").first
    try:
        if summary_link.count() > 0:
            txt = (summary_link.inner_text() or "").strip()
            if txt.isdigit() and int(txt) == 0:
                print("Depot Special Orders: summary shows 0 orders; skipping.")
                return False
    except Exception:
        pass

    open_accepted = page.locator(
        "a[href*='merchant=thdso'][href*='web_quickship'], "
        "a[href*='merchant=thdso'][href*='substatus=accepted']"
    ).filter(has_text=re.compile(r"open\s*/\s*accepted", re.I)).first
    if open_accepted.count() == 0:
        open_accepted = page.get_by_role("link", name=re.compile(r"open\s*/\s*accepted", re.I)).first
    if open_accepted.count() == 0:
        print("Depot Special Orders: no Open/Accepted link; skipping.")
        return False
    try:
        open_accepted.click()
        page.wait_for_load_state("domcontentloaded")
    except Exception as exc:
        print(f"Depot Special Orders: could not open Accepted queue ({exc}); skipping.")
        return False

    if not _wait_for_special_order_page_ready(page, _SHIP_LIST_TIMEOUT_MS):
        print("Depot Special Orders: queue empty or not ready; skipping.")
        return False
    return True


def _process_depot_special_order_page(page: Page, tracking_dict: dict[str, str]) -> bool:
    if not _wait_for_special_order_page_ready(page, _SHIP_LIST_TIMEOUT_MS):
        print("Depot Special Orders: timed out waiting for order list; moving on.")
        return False

    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(SCROLL_WAIT_MS)

    po_links = page.locator("a.simple_link[href*='gotoOrderDetail'], a[href*='gotoOrderDetail']")
    n = po_links.count()
    if n == 0:
        print("Depot Special Orders: no PO rows on page; moving on.")
        return False

    est_date = _special_order_estimated_delivery_date()
    touched = False
    matched_po_count = 0

    for i in range(n):
        po_elem = po_links.nth(i)
        try:
            po = (po_elem.inner_text() or "").strip()
            tracking = _lookup_special_order_tracking(tracking_dict, po)
            if not tracking:
                continue
            matched_po_count += 1
            href = po_elem.get_attribute("href") or ""
            if "Hub_PO=" not in href:
                continue
            order_id = href.split("Hub_PO=")[-1].split("&")[0]

            track_sel = f"[id='order({order_id}).box(1).trackingnumber']"
            bol_sel = f"[id='order({order_id}).box(1).billOfLading']"
            contact_sel = f"[id='order({order_id}).contactInfo']"
            ship_sel = f"[id='order({order_id}).box(1).shippingmethod']"

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

            if not _filled_input(page, track_sel):
                page.locator(track_sel).fill("")
                page.locator(track_sel).fill(tracking)
                touched = True

            if not _filled_input(page, bol_sel):
                page.locator(bol_sel).fill("")
                page.locator(bol_sel).fill(tracking)
                touched = True

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
        print("Depot Special Orders: no PO matches in CSV on this page; moving on.")
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
            return False
        page.wait_for_timeout(500 if _chain_fast() else 900)
        return True
    except Exception as exc:
        print(f"Depot Special Orders: submit failed: {exc}")
        return False


def run_depot_special_order_tracking_with_page(
    page: Page, tracking_csv_path: str | None = None
) -> None:
    """Home Depot Special Orders (thdso): tracking only; skips quietly when queue is empty."""
    path = tracking_csv_path or TRACKING_CSV
    tracking_dict = load_tracking_csv(str(path))
    if not tracking_dict:
        print(f"Depot Special Orders: no tracking rows loaded from {path}; skipping.")
        return

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
        try:
            if page.locator("input[name$='.invoicenumber.autofill']").count() > 0:
                return True
            if page.locator("select[name$='.termstypecode']").count() > 0:
                return True
        except Exception:
            pass
        page.wait_for_timeout(400 if _chain_fast() else 700)
    return False


def _open_depot_special_order_quickinvoice(page: Page) -> bool:
    """Navigate to thdso Needs Invoicing queue. Returns False when queue is empty."""
    print("Depot Special Orders: opening Needs Invoicing queue...")
    page.goto(SPECIAL_ORDER_QUICKINVOICE_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(250 if _chain_fast() else 500)
    if _wait_for_special_order_invoice_page_ready(page, 15_000):
        return True

    page.goto(SPECIAL_ORDER_SUMMARY_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(300 if _chain_fast() else 600)
    needs_inv = page.locator(
        "a[href*='merchant=thdso'][href*='web_quickinvoice'], "
        "a[href*='gotoOrderRealmForm.do?action=web_quickinvoice'][href*='merchant=thdso']"
    ).filter(has_text=re.compile(r"needs\s+invoicing", re.I)).first
    if needs_inv.count() == 0:
        needs_inv = page.get_by_role("link", name=re.compile(r"needs\s+invoicing", re.I)).first
    if needs_inv.count() == 0:
        print("Depot Special Orders invoicing: no Needs Invoicing link; skipping.")
        return False
    try:
        needs_inv.click()
        page.wait_for_load_state("domcontentloaded")
    except Exception as exc:
        print(f"Depot Special Orders invoicing: could not open queue ({exc}); skipping.")
        return False

    if not _wait_for_special_order_invoice_page_ready(page, _INVOICE_AUTOFILL_TIMEOUT_MS):
        print("Depot Special Orders invoicing: queue empty or not ready; skipping.")
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
    if not _wait_for_special_order_invoice_page_ready(page, _INVOICE_AUTOFILL_TIMEOUT_MS):
        print("Depot Special Orders invoicing: timed out waiting for invoice page; moving on.")
        return False

    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(SCROLL_WAIT_MS)

    order_ids = _special_order_invoice_order_ids(page)
    if not order_ids:
        print("Depot Special Orders invoicing: no orders on page; moving on.")
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
    if not _wait_for_invoice_page_ready(page, _INVOICE_AUTOFILL_TIMEOUT_MS):
        print(
            f"Depot invoicing: timed out waiting {int(_INVOICE_AUTOFILL_TIMEOUT_MS / 1000)}s; "
            "retrying page load once..."
        )
        try:
            page.reload(wait_until="domcontentloaded")
        except Exception:
            try:
                page.goto(INVOICE_URL, wait_until="domcontentloaded")
            except Exception:
                pass
        if not _wait_for_invoice_page_ready(page, _INVOICE_AUTOFILL_TIMEOUT_MS):
            print("Depot invoicing: no invoice rows or queue empty; moving on.")
            return False

    invoice_frame = _wait_invoice_form_frame(page, _INVOICE_AUTOFILL_TIMEOUT_MS)
    if invoice_frame is None:
        print("Depot invoicing: no invoice rows or queue empty; moving on.")
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
    page.goto(INVOICE_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(250 if _chain_fast() else 500)

    for batch in range(1, MAX_INVOICE_PAGES + 1):
        if not _process_depot_invoice_page(page):
            print("Depot invoicing: finished.")
            break
        print(f"Depot invoicing: submitted batch {batch}.")
        page.wait_for_timeout(POST_SUBMIT_MS)
    else:
        print(f"Depot invoicing: stopped after {MAX_INVOICE_PAGES} batches (safety cap).")
