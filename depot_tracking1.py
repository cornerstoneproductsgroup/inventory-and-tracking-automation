
import re
import time
import csv
import os
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager

UPS_TRACKING_CSV_DEFAULT = (
    r"\\rygarcorp.com\shares\Cornerstone\Dot Com Packing Slips\1-Orders Before Extraction"
    r"\Order Splitter Output\z - UPS Tracking\UPS_CSV_EXPORT.csv"
)
TRACKING_CSV = (os.environ.get("UPS_TRACKING_CSV_PATH") or "").strip() or UPS_TRACKING_CSV_DEFAULT
ORDER_URL = "https://dsm.commercehub.com/dsm/gotoOrderRealmForm.do?action=web_quickship&tabContext=web_quickship&status=open&substatus=no-activity&merchant=thehomedepot"
EMAIL = "rfetzer@cornerstoneproductsgroup.com"
PASSWORD = "Lowesdepotdepotso1106!"
# Safety cap so the script always exits (CommerceHub should need far fewer iterations).
MAX_SHIP_PAGES = 200


def _norm_header(s):
    return "".join(c.lower() for c in s.strip() if c not in " #._-")


def _looks_like_ups_tracking(s):
    t = s.strip().upper().replace(" ", "")
    return len(t) >= 10 and t.startswith("1Z")


def po_tracking_aliases(raw: str) -> set[str]:
    """
    Keys for matching CommerceHub PO text to CSV rows.
    Handles 41_21549259 vs 41-21549259 vs 21549259 and spaces in store/PO.
    """
    text = (raw or "").strip()
    out: set[str] = set()
    if not text:
        return out
    out.add(text)
    out.add(text.upper())
    compact = re.sub(r"\s+", "", text)
    if compact:
        out.add(compact)
    normalized = re.sub(r"[\s\-]+", "_", text.strip())
    if normalized:
        out.add(normalized)
        out.add(normalized.replace("_", "-"))
    match = re.match(r"^(\d{1,4})[_\-\s]+(\d{5,})$", text)
    if match:
        store, num = match.group(1), match.group(2)
        out.add(f"{store}_{num}")
        out.add(f"{store}-{num}")
        out.add(num)
        if num.isdigit():
            out.add(num.zfill(9))
    elif text.isdigit():
        out.add(text.zfill(9))
    digits = re.sub(r"\D", "", text)
    if digits:
        out.add(digits)
        if len(digits) >= 9:
            out.add(digits[-9:])
    return {k for k in out if k}


def register_po_tracking(tracking_dict: dict[str, str], po_raw: str, tracking: str) -> None:
    track = (tracking or "").strip().split()[0]
    if not track:
        return
    for key in po_tracking_aliases(po_raw):
        tracking_dict[key] = track


def lookup_po_tracking(tracking_dict: dict[str, str], po_raw: str) -> str | None:
    for key in po_tracking_aliases(po_raw):
        hit = tracking_dict.get(key)
        if hit:
            return hit
    return None


def _pick_po_field(fieldnames):
    if not fieldnames:
        return None
    priority = (
        "po#",
        "po",
        "ponumber",
        "ponum",
        "purchaseorder",
        "referencenumber1",
        "reference1",
        "orderreference",
        "hubpo",
    )
    norm_map = {f: _norm_header(f) for f in fieldnames}
    for want in priority:
        w = _norm_header(want)
        for f, n in norm_map.items():
            if n == w or (w == "po" and n.startswith("po") and "date" not in n):
                return f
    for f, n in norm_map.items():
        if n == "po" or (n.startswith("po") and "date" not in n and len(n) <= 12):
            return f
    return fieldnames[0]


def _pick_tracking_field(fieldnames, po_field):
    priority = (
        "tracking#",
        "trackingnumber",
        "tracking",
        "shipmenttracking#",
        "shipmenttrackingnumber",
        "upstracking",
        "track",
    )
    norm_map = {f: _norm_header(f) for f in fieldnames}
    po_norm = _norm_header(po_field) if po_field else ""
    for want in priority:
        w = _norm_header(want)
        for f, n in norm_map.items():
            if f == po_field:
                continue
            if n == w or (w == "tracking" and "track" in n):
                return f
    for f, n in norm_map.items():
        if f == po_field or n == po_norm:
            continue
        if "track" in n:
            return f
    for f in fieldnames:
        if f != po_field:
            return f
    return None


def load_tracking_csv(path):
    tracking_dict = {}
    if not os.path.exists(path):
        return tracking_dict
    rows = None
    for enc in ("utf-8-sig", "latin1"):
        try:
            with open(path, mode="r", newline="", encoding=enc) as file:
                reader = csv.reader(file)
                rows = list(reader)
            break
        except UnicodeDecodeError:
            continue
    if not rows:
        return tracking_dict

    header = [c.strip() for c in rows[0]]
    non_empty = sum(1 for c in header if c)
    if any(_looks_like_ups_tracking(c) for c in header):
        looks_like_header = False
    else:
        joined = " ".join(header).lower()
        looks_like_header = non_empty >= 2 and any(
            kw in joined
            for kw in (
                "tracking",
                "track #",
                "po#",
                "purchase order",
                "reference",
                "ship to",
            )
        )

    if looks_like_header:
        po_field = _pick_po_field(header)
        track_field = _pick_tracking_field(header, po_field)
        po_i = header.index(po_field) if po_field in header else 0
        track_i = header.index(track_field) if track_field and track_field in header else 1
        data_rows = rows[1:]
    else:
        po_i, track_i = 0, 1
        data_rows = rows

    for row in data_rows:
        if len(row) <= max(po_i, track_i):
            continue
        po_raw = row[po_i].strip()
        track_raw = row[track_i].strip()
        if not po_raw or not track_raw:
            continue
        # Worldship often puts PO# and product text in one field, e.g. "13895885 Coarse 10"
        po_token = po_raw.split()[0]
        if not po_token:
            continue
        register_po_tracking(tracking_dict, po_token, track_raw)
    return tracking_dict


def is_field_filled(element):
    try:
        return element.get_attribute("value").strip() != ""
    except Exception:
        return False


def process_page(driver, tracking_dict):
    try:
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.XPATH, "//select[contains(@name, '.shippingmethod')]"))
        )
    except Exception:
        print("No orders left to process or page failed to load.")
        return False

    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    time.sleep(2)

    po_elements = driver.find_elements(By.XPATH, "//a[contains(@href, 'gotoOrderDetail')]")
    for po_elem in po_elements:
        try:
            po = po_elem.text.strip().zfill(9)
            order_id = po_elem.get_attribute("href").split("Hub_PO=")[-1]

            ship_method_field = driver.find_element(By.ID, f"order({order_id}).box(1).shippingmethod")
            tracking_field = driver.find_element(By.ID, f"order({order_id}).box(1).trackingnumber")

            qty_inputs = driver.find_elements(
                By.XPATH,
                f"//input[starts-with(@name,'order({order_id}).box(1).item') and contains(@name,'.shipped')]",
            )
            skip_order = all(is_field_filled(q) for q in qty_inputs) and is_field_filled(ship_method_field)
            # Still process when CSV has tracking but tracking field is empty (qty/ship can already be set).
            if skip_order and is_field_filled(tracking_field):
                continue

            qty_cells = driver.find_elements(
                By.XPATH,
                f"//td[contains(@id, 'order({order_id}).box(1).item') and contains(@id, '.remaining')]",
            )
            for cell in qty_cells:
                qty = cell.text.strip()
                if not qty.isdigit():
                    continue
                cell_id = cell.get_attribute("id")
                shipped_id = cell_id.replace("cell.line.", "").replace(".remaining", ".shipped")
                try:
                    ship_box = driver.find_element(By.ID, shipped_id)
                    if not is_field_filled(ship_box):
                        ship_box.clear()
                        ship_box.send_keys(qty)
                except Exception:
                    continue

            if not is_field_filled(ship_method_field):
                ship_method_field.send_keys("UPS Ground")

            if po in tracking_dict and not is_field_filled(tracking_field):
                tracking_field.clear()
                tracking_field.send_keys(tracking_dict[po])

        except Exception as e:
            print(f"Error processing PO element: {e}")

    print("✅ Autofill complete for current page. Submitting now...")
    try:
        submit_btn = driver.find_element(By.ID, "confirmbtn")
        submit_btn.click()
        return True
    except Exception as e:
        print("❌ Submit button not found or click failed:", e)
        return False


def main():
    tracking_dict = load_tracking_csv(TRACKING_CSV)

    options = Options()
    options.add_experimental_option("detach", False)
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)

    try:
        driver.get(ORDER_URL)
        WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.ID, "username"))).send_keys(
            EMAIL + Keys.RETURN
        )
        WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.ID, "password"))).send_keys(
            PASSWORD + Keys.RETURN
        )
        WebDriverWait(driver, 30).until(EC.element_to_be_clickable((By.CLASS_NAME, "application-identity-item"))).click()

        for page_num in range(1, MAX_SHIP_PAGES + 1):
            result = process_page(driver, tracking_dict)
            if not result:
                print("✅ Home Depot tracking: no further pages to process (or submit failed).")
                break
            print(f"Submitted batch {page_num}; waiting for next page...")
            time.sleep(5)
        else:
            print(f"⚠️ Stopped after {MAX_SHIP_PAGES} submit cycles (safety limit). Check Rithum if more orders remain.")

    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    main()
