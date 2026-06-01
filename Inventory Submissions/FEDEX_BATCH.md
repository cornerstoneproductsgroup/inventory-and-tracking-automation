# FedEx batch shipping (Lowe's Output CSV)

Automates [FedEx Shipping Plus batch uploads](https://www.fedex.com/shippingplus/en-us/shipments-import):

1. Upload today's **Order Splitter** file: `Lowe's M-D-YYYY Output.csv`
2. Wait until **Ready to finalize** shows the shipment count
3. Open the batch and process **REFERENCE** rows (`PO` + `SKU`)
4. Select consecutive rows per **vendor** (from `vendor_map_lowes.xlsx`)
5. **Finalize and print manually** → save one PDF per vendor:  
   `...\3-Lowe's\1-Fedex Shipping Labels\<Vendor>\Lowe's <Vendor> M-D-YYYY.pdf`
6. Skip rows that already have a **Tracking ID** / **Shipment created & printed**
7. After all vendor labels are saved: **select all** shipments → **DOWNLOAD** → **Shipment report (.xlsx file)** → overwrite **`Lowe's Fedex Master.xlsx`** for Lowe's tracking automation

## File locations

| Item | Default path |
|------|----------------|
| Upload CSV | `\\rygarcorp.com\shares\Cornerstone\Dot Com Packing Slips\1-Orders Before Extraction\Order Splitter Output\CSV File Output\Lowe's\Lowe's 6-1-2026 Output.csv` |
| Label PDFs | `\\rygarcorp.com\shares\Cornerstone\Dot Com Packing Slips\3-Lowe's\1-Fedex Shipping Labels\<Vendor>\` |
| Shipment report (tracking) | `\\rygarcorp.com\shares\Cornerstone\Dot Com Packing Slips\1-Orders Before Extraction\Order Splitter Output\z - Lowe's Tracking\Lowe's Fedex Master.xlsx` (overwritten each run) |
| Used-file log | `Inventory Submissions\fedex_upload_state.json` |

## Skip logic (no Lowe's orders)

If the **newest** CSV in the folder is **not** today's date:

- Logs the filename (e.g. last week's file on top)
- If that name is already in `fedex_upload_state.json`, notes it was used before
- **Exits successfully** without opening FedEx (safe for full daily workflow)

## Setup (login credentials)

1. Copy the example env file if you do not have one yet:

   ```powershell
   cd "Inventory Submissions"
   copy .env.example .env
   ```

2. Edit **`Inventory Submissions\.env`** and set your FedEx login (same account you use in the browser):

   ```ini
   FEDEX_USERNAME=your-fedex-login@example.com
   FEDEX_PASSWORD=your-fedex-password
   ```

3. Verify credentials load (no browser):

   ```powershell
   python run_fedex_batch.py --check-credentials
   ```

4. Optional: copy `fedex_batch.example.json` → `fedex_batch.json`.  
   You can put credentials in JSON instead of `.env`, but **`.env` is recommended** (keeps passwords out of git).  
   JSON supports `${FEDEX_USERNAME}` / `${FEDEX_PASSWORD}` placeholders.

5. First full run uses a visible browser (`headless: false` in config) so login can save **`fedex_storage_state.json`** for later runs.

6. On first visit, FedEx may show a OneTrust **Accept all cookies** banner. The automation clicks it when present (`#onetrust-accept-btn-handler`). Override with `selectors.cookie_accept` in `fedex_batch.json` if your banner differs.

7. Login uses [FedEx secure login](https://www.fedex.com/secure-login/en-us/) (not the marketing home page). If login fields flash and disappear, set `fedex.login_url` to that URL in `fedex_batch.json`.

## Commands

```powershell
cd "Inventory Submissions"
python run_fedex_batch.py --plan-only
python run_fedex_batch.py
python run_fedex_batch.py --skip-upload
```

Menu **F** in `Run Full Workflow.bat` or `Run FedEx Batch.bat`.

## Env overrides

- `FEDEX_BATCH_URL` — batch page (default: shipments-import URL above)
- `FEDEX_LOWES_CSV_PATH` — force a specific CSV
- `FEDEX_LOWES_CSV_DIR` / `FEDEX_LOWES_LABELS_DIR` — path overrides
- `FEDEX_UPLOAD_POLL_TIMEOUT_S` — wait for batch ready (default 600)
- `FEDEX_LABEL_SAVE_TIMEOUT_S` — Save PDF dialog timeout (default 120)
- `FEDEX_LOWES_TRACKING_DIR` — folder for `Lowe's Fedex Master.xlsx` (tracking input)
- `FEDEX_SHIPMENT_REPORT_TIMEOUT_MS` — shipment report download timeout (default 180000)
