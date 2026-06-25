# Amazon Seller Central download (Deferred Transaction CSV → Input share; watcher formats + prints)

Downloads **Deferred Transaction** CSV from Payments → Reports Repository and saves to:

`\\rygarcorp.com\shares\Cornerstone\Invoice Reports\Amazon\Input`

Filename: **`Amazon Invoice M-D-YYYY.csv`** (today’s date).

## Security — no remote debugging

Amazon automation uses **Playwright direct control** of Chrome only. It does **not**:

- Open a Chrome remote debugging port (`--remote-debugging-port`)
- Use `Run Amazon Chrome Debug.bat` (removed — triggers Huntress / IT alerts)
- Honor `AMAZON_CHROME_CDP_URL`, `AMAZON_CHROME_CDP_PORT`, or similar `.env` keys (they are ignored)

## Use your normal Chrome login (default)

The script uses **your installed Chrome profile** (same cookies as daily Chrome).

Add to `Inventory Submissions\.env`:

```
AMAZON_KILL_CHROME=1
```

(`AMAZON_KILL_CHROME=1` closes Chrome before reopening under automation. Amazon uses **Google Chrome only** — not Edge.)

### Run

1. Close Chrome (or set `AMAZON_KILL_CHROME=1`)
2. Run `Run Amazon Seller Download.bat` or menu **R → 6**

## Credentials (fallback)

If sign-in is required:

```
AMAZON_USERNAME=you@company.com
AMAZON_PASSWORD=...
```

## Isolated profile instead of system Chrome

```
AMAZON_CHROME_USE_SYSTEM_PROFILE=false
```

Session is saved under `invoice report\.amazon-chrome-profile`.

## Post-process

Post-process after download is **off** by default — use `Run Amazon Invoice Watcher.bat`.
