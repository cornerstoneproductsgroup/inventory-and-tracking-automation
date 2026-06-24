# Amazon Seller Central download

Downloads **Deferred Transaction** CSV from Payments → Reports Repository and saves to:

`\\rygarcorp.com\shares\Cornerstone\Invoice Reports\Amazon\Input`

Filename: **`Amazon Invoice M-D-YYYY.csv`** (today’s date, e.g. `Amazon Invoice 6-24-2026.csv`).

Your **Amazon Invoice Watcher** picks up the file from Input, formats, and prints.

## Chrome session (recommended)

By default Playwright launches **Google Chrome** with a persistent profile at:

`invoice report/.amazon-chrome-profile`

1. First run: Chrome opens → sign in to Seller Central once (including 2FA if prompted).
2. Later runs: session is reused — no login each time.

### Reuse employee Chrome already open

Start Chrome with remote debugging, then set in `invoice report/.env`:

```
AMAZON_CHROME_CDP_URL=http://127.0.0.1:9222
```

Example shortcut target:

```
"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222
```

The script attaches to the open browser (employee must already be on Seller Central).

### Disable Chrome profile (fallback)

Use Playwright Chromium + credentials in `.env`:

```
AMAZON_CHROME_USER_DATA_DIR=disabled
AMAZON_SELLER_EMAIL=...
AMAZON_SELLER_PASSWORD=...
```

## Run

```
Run Amazon Seller Download.bat
```

or

```
python run_amazon_seller_download.py
```

Post-process after download is **off** by default (`AMAZON_DOWNLOAD_AUTO_POSTPROCESS=false`) — use the watcher.

## Env

| Variable | Default |
|----------|---------|
| `AMAZON_CHROME_USER_DATA_DIR` | `invoice report/.amazon-chrome-profile` |
| `AMAZON_CHROME_CDP_URL` | (empty — attach to running Chrome) |
| `AMAZON_REQUEST_REPORT_SETTLE_S` | 10 (wait after Request Report before Refresh) |
| `AMAZON_REPORT_POLL_INTERVAL_S` | 8 |
| `AMAZON_REPORT_MAX_REFRESH` | 20 |
| `AMAZON_DOWNLOAD_AUTO_POSTPROCESS` | false |
| `AMAZON_INVOICE_INPUT_DIR` | `...\Amazon\Input` |
