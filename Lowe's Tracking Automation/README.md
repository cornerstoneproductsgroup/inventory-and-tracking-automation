# Lowe's Tracking Automation (Rithum + FedEx CSV)

This project automates tracking updates in Rithum for Lowe's orders:

1. Logs in to Rithum
2. Opens the orders page and collects order links
3. Opens each order and reads the PO number
4. Matches that PO against a FedEx CSV file
5. Fills tracking number, shipment type, and quantity
6. Optionally submits the order

It supports Lowe's workflows:

- `ship_to_store`
- `ship_to_customer`
- `invoice` (Needs Invoicing: Select All, Auto Fill per order, Submit; repeats while more orders load)

## 1) Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
```

## 2) Configure

1. Edit `config.example.json` (or copy it to another file and pass `--config`)
2. Update URLs and CSS selectors for your Rithum pages
3. FedEx source is preconfigured for Lowe's:
   - Folder: `\\rygarcorp.com\shares\Cornerstone\Dot Com Packing Slips\1-Orders Before Extraction\Order Splitter Output\z - Lowe's Tracking`
   - Base file name: `Lowe's Fedex Master`
   - PO column letter: `C`
   - Tracking column letter: `E`
4. If needed, set `sheet_name` (Excel tab). Leave blank to use the first sheet.
5. Update URLs and CSS selectors for your Rithum pages.
   - `rithum.orders_url` can be the Lowe's orders hub (`gotoOpenOrders.do?PID=lowes`)
   - `rithum.workflows.ship_to_store.orders_url` can be the GS1 list (`action=web_quickshipgs1`)
   - `rithum.workflows.ship_to_customer.orders_url` can be the available-to-ship list (`action=web_quickship`)
   - Use `rithum.selectors` for shared selectors, and workflow-specific overrides in each workflow `selectors` object if needed.
6. Set credentials either directly in your config file or with env vars:

```powershell
$env:RITHUM_USERNAME="your_username"
$env:RITHUM_PASSWORD="your_password"
```

Credential fields in config:

- `rithum.username`
- `rithum.password`

## 3) Dry run first (no submit)

```powershell
python lowes_tracking_automation.py --config config.example.json
```

Run only one workflow:

```powershell
python lowes_tracking_automation.py --config config.example.json --workflow ship_to_store
python lowes_tracking_automation.py --config config.example.json --workflow ship_to_customer
python lowes_tracking_automation.py --config config.example.json --workflow invoice
```

Run **store, then customer, then invoicing** in one browser session:

```powershell
python lowes_tracking_automation.py --config config.example.json --workflow all --submit
```

With `--workflow all`, ship-to-customer uses **queue-empty** mode (processes each batch until no matching rows remain) so invoicing can run afterward in the same run. Standalone `--workflow ship_to_customer` still follows `run_until_stopped` in your config.

## 4) Live run (with submit)

```powershell
python lowes_tracking_automation.py --config config.example.json --submit
```

## Notes

- The default selectors in `config.example.json` are placeholders and must be updated.
- Ship-to-store often uses clickable table rows (`tr[onclick=...]`) rather than anchor tags; the script supports both `href` and `onclick` link extraction.
- Ship-to-store supports `shipment_type_value_override` (set to `FEDX` for FedEx Ground in the provided template).
- Ship-to-store supports `use_quantity_remaining`; when enabled, ship quantity is read from `quantity_remaining` text (e.g. `1 EA`) with fallback to the quantity input `max` attribute.
- Ship-to-customer is configured as an in-list workflow (`process_in_list: true`) and fills order rows directly on the 50-order page.
- For ship-to-customer multi-line orders, each `...shipped` input is matched to its own `...remaining` cell so line quantities stay aligned.
- Ship-to-customer uses `input#confirmbtn` for submit and can run continuously with `run_until_stopped: true`.
- In continuous mode, after each submit/reload it reopens the ship-to-customer list and keeps processing until you stop/close the run.
- Login supports two-step auth flow: email `Continue`, password `Continue`, then optional profile selector click.
- You can tune login timing with `rithum.login_delays_ms` (`after_email_continue`, `after_password_continue`, `before_profile_selector`).
- The script auto-detects `Lowe's Fedex Master` as `.xlsx`, `.xlsm`, or `.csv` in the configured folder.
- `skip_orders_without_csv_match` controls whether unmatched POs are skipped or treated as errors.
- Quantity defaults to `1` using `fedex_csv.quantity_value` unless you map a quantity column.
- `pause_for_manual_review_before_submit` can pause before each submit for manual validation.
- Relative Rithum paths like `gotoOrderRealmForm.do?...` are supported and auto-resolved to the DSM host.
- For safety, always validate in dry-run mode before using `--submit`.
- Dry runs keep the browser open for a few seconds after work finishes (`automation.dry_run_browser_hold_seconds_ship_to_store`, `dry_run_browser_hold_seconds_ship_to_customer`, `dry_run_browser_hold_seconds_invoice`, `dry_run_browser_hold_seconds` for other filters). Increase these if you need more time to verify fields.
- Invoicing timing: tune `rithum.workflows.invoice.idle_after_submit_ms` and `rithum.workflows.invoice.delay_between_invoice_autofill_ms` if the UI needs more time between clicks.
