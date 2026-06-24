# Amazon Seller Central download (Deferred Transaction CSV → Input share; watcher formats + prints)

Downloads **Deferred Transaction** CSV from Payments → Reports Repository and saves to:

`\\rygarcorp.com\shares\Cornerstone\Invoice Reports\Amazon\Input`

Filename: **`Amazon Invoice M-D-YYYY.csv`** (today’s date).

## Use your normal Chrome login (default)

By default the script uses **your installed Chrome profile** (same cookies as when you open Chrome yourself). It does **not** use the empty `.amazon-chrome-profile` folder anymore.

**First run:** Chrome closes briefly, reopens with Seller Central, and automation continues while you stay logged in.

Add to `Inventory Submissions\.env` (optional):

```
UPS_KILL_CHROME=1
UPS_BROWSER_CHANNEL=chrome
```

`UPS_KILL_CHROME=1` lets automation close Chrome before reopening with your profile. Without it, close all Chrome windows manually before running.

### Option A — Let the script launch Chrome (easiest)

1. Close Chrome (or set `UPS_KILL_CHROME=1`)
2. Run `Run Amazon Seller Download.bat` or menu **R → 6**
3. Chrome reopens already logged in if you use it daily for Seller Central

### Option B — Keep Chrome open yourself

1. Run **`Run Amazon Chrome Debug.bat`** once (starts Chrome with debug port 9222)
2. Sign in to Seller Central in that window if needed
3. Add to `.env`:
   ```
   AMAZON_CHROME_CDP_URL=http://127.0.0.1:9222
   ```
4. Run the download — attaches to that Chrome without closing it

## Credentials (fallback)

If sign-in is required:

```
AMAZON_USERNAME=you@company.com
AMAZON_PASSWORD=...
```

(Read from `Inventory Submissions\.env` or `invoice report\.env`.)

## Disable system Chrome profile

Use an isolated automation profile instead:

```
AMAZON_CHROME_USE_SYSTEM_PROFILE=false
```

## Run

```
Run Amazon Seller Download.bat
```

Post-process after download is **off** by default — use `Run Amazon Invoice Watcher.bat`.
