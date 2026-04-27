
import time
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

EMAIL = "rfetzer@cornerstoneproductsgroup.com"
PASSWORD = "Lowesdepotdepotso1106!"
INVOICE_URL = "https://dsm.commercehub.com/dsm/gotoOrderRealmForm.do?action=web_quickinvoice&tabContext=web_quickinvoice&merchant=thehomedepot"
MAX_INVOICE_PAGES = 200


def process_invoice_page(driver):
    try:
        WebDriverWait(driver, 30).until(
            EC.presence_of_all_elements_located((By.XPATH, "//input[contains(@name, '.invoicenumber.autofill')]"))
        )
    except Exception:
        print("❌ No invoice rows found or page failed to load.")
        return False

    time.sleep(2)
    invoice_buttons = driver.find_elements(By.XPATH, "//input[contains(@name, '.invoicenumber.autofill')]")
    for btn in invoice_buttons:
        try:
            btn.click()
            time.sleep(0.2)
        except Exception:
            continue

    net_due = driver.find_elements(By.XPATH, "//input[contains(@name, '.termsnetdaysdue')]")
    discount_pct = driver.find_elements(By.XPATH, "//input[contains(@name, '.termsdiscountpercent')]")
    discount_due = driver.find_elements(By.XPATH, "//input[contains(@name, '.termsdiscountdaysdue')]")

    for field in net_due:
        field.clear()
        field.send_keys("30")
    for field in discount_pct:
        field.clear()
        field.send_keys("1")
    for field in discount_due:
        field.clear()
        field.send_keys("30")

    invoiceable_cells = driver.find_elements(By.XPATH, "//td[contains(@id, '.invoiceable')]")
    for cell in invoiceable_cells:
        try:
            qty = cell.text.strip()
            if not qty.isdigit():
                continue
            cell_id = cell.get_attribute("id")
            input_id = cell_id.replace("cell.line.", "").replace(".invoiceable", ".invoiced")
            input_box = driver.find_element(By.ID, input_id)
            input_box.clear()
            input_box.send_keys(qty)
        except Exception:
            continue

    print("✅ Invoices filled for this page. Submitting...")
    try:
        submit = driver.find_element(By.ID, "confirmbtn")
        submit.click()
        return True
    except Exception:
        print("❌ Submit button not found.")
        return False


def main():
    options = Options()
    options.add_experimental_option("detach", False)
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)

    try:
        driver.get(INVOICE_URL)

        WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.ID, "username"))).send_keys(
            EMAIL + Keys.RETURN
        )
        WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.ID, "password"))).send_keys(
            PASSWORD + Keys.RETURN
        )

        time.sleep(5)
        WebDriverWait(driver, 30).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "a.application-identity-item"))
        ).click()

        for page_num in range(1, MAX_INVOICE_PAGES + 1):
            result = process_invoice_page(driver)
            if not result:
                print("✅ Home Depot invoicing: no further invoice pages (or submit failed).")
                break
            print(f"Submitted invoice batch {page_num}; waiting for next page...")
            time.sleep(5)
        else:
            print(f"⚠️ Stopped after {MAX_INVOICE_PAGES} invoice batches (safety limit). Check Rithum if more remain.")

    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    main()
