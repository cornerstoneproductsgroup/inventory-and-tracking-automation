# Amazon Seller Central download

**ON HOLD** — Seller Central sign-in requires phone 2FA on every automated browser session.  
The Playwright download code remains in this folder but is **not** in the morning schedule or workflow menu.

**Still works:** drop a raw CSV in the Amazon **Input** share → `Run Amazon Invoice Watcher.bat` (format + print).

**To re-enable later:**

1. Set `AMAZON_SELLER_DOWNLOAD_ENABLED=true` in `invoice report/.env`
2. Move `amazon_seller_download` from `_on_hold_steps` into `steps[]` in `scheduled_workflow.json`
3. Restore menu option in `Run Full Workflow.bat` if desired

---

# Amazon Seller Central download (implementation notes)

Automates **Payments → Reports Repository → Deferred Transaction → Request Report → Download CSV**.

## Output

Saves to:

`\\rygarcorp.com\shares\Cornerstone\Invoice Reports\Amazon\Input`

Filename uses **yesterday's date** (no leading zeros):

`Amazon Invoice Report 5-31-2026 Input.csv`

When run on **6-1-2026**, the file is named for **5-31-2026**.

## Setup

1. Copy `invoice report\.env.example` → `invoice report\.env` if needed.
2. Add credentials:
   ```
   AMAZON_SELLER_EMAIL=your@email.com
   AMAZON_SELLER_PASSWORD=your_password
   ```
3. Optional: copy `amazon_seller.example.json` → `amazon_seller.json` for selector tweaks.  
   Login fields default to `#ap_email` and `#ap_password` (Amazon sign-in page).
4. First run: visible browser (`headless: false`) — complete MFA if prompted; session saves to `amazon_seller_storage_state.json`.

## Commands

```bat
Run Amazon Seller Download.bat
cd "invoice report"
python run_amazon_seller_download.py --check-credentials
python run_amazon_seller_download.py
```

After download, `amazon_invoice_postprocess` runs by default (format + print). Disable with `AMAZON_DOWNLOAD_AUTO_POSTPROCESS=false` if you only want the watcher to handle it.

## Morning schedule

Included in `scheduled_workflow.json` as step `amazon_seller_download` (after CommerceHub/SPS invoice reports).

## Env

| Variable | Default |
|----------|---------|
| `AMAZON_INVOICE_INPUT_DIR` | `...\Amazon\Input` |
| `AMAZON_REPORT_POLL_INTERVAL_S` | 8 |
| `AMAZON_REPORT_MAX_REFRESH` | 20 |
| `AMAZON_DOWNLOAD_AUTO_POSTPROCESS` | true |
