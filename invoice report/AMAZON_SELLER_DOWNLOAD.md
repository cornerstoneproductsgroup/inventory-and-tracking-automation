# Amazon Seller Central download (Deferred Transaction CSV → Input share; watcher formats + prints)

Downloads **Deferred Transaction** CSV from Payments → Reports Repository and saves to:

`\\rygarcorp.com\shares\Cornerstone\Invoice Reports\Amazon\Input`

Filename: **`Amazon Invoice M-D-YYYY.csv`** (today’s date).

## Security (no remote debugging)

By default the script uses **Playwright direct control** of Chrome — it does **not** open a remote debugging port. That avoids security alerts (e.g. Huntress flags `--remote-debugging-port` like infostealer malware).

Do **not** use `Run Amazon Chrome Debug.bat` unless IT has approved and set `AMAZON_ALLOW_UNSAFE_CDP=1`.

## Use your normal Chrome login (default)

The script uses **your installed Chrome profile** (same cookies as daily Chrome).

**First run:** Chrome closes briefly, reopens under automation, and navigates to Seller Central.

Add to `Inventory Submissions\.env`:

```
AMAZON_KILL_CHROME=1
```

(`AMAZON_KILL_CHROME=1` closes Chrome before reopening with your profile. Amazon **never** uses Edge — only Google Chrome.)

### Run

1. Close Chrome (or set `AMAZON_KILL_CHROME=1`)
2. Run `Run Amazon Seller Download.bat` or menu **R → 6**
3. Chrome reopens and automation continues while you stay logged in

## Credentials (fallback)

If sign-in is required:

```
AMAZON_USERNAME=you@company.com
AMAZON_PASSWORD=...
```

(Read from `Inventory Submissions\.env` or `invoice report\.env`.)

## Isolated profile instead of system Chrome

```
AMAZON_CHROME_USE_SYSTEM_PROFILE=false
```

Log in once in the isolated profile; session is saved under `invoice report\.amazon-chrome-profile`.

## IT-only: remote debugging (disabled by default)

Only if security has explicitly approved:

```
AMAZON_ALLOW_UNSAFE_CDP=1
AMAZON_CHROME_LAUNCH_MODE=cdp
```

Optional attach to a running debug Chrome:

```
AMAZON_CHROME_CDP_URL=http://127.0.0.1:9348
```

## Post-process

Post-process after download is **off** by default — use `Run Amazon Invoice Watcher.bat`.
