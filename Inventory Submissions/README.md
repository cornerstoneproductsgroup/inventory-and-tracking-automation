# Inventory-Automation

Automates the daily Rithum and SPS inventory submission flows using Playwright.

## What This Automates

1. Log in to Rithum.
2. Handle profile selection (first available Select/Continue option).
3. Open Inventory Update page.
4. Check All (`#selectAllIBL`).
5. Click Next (`#iblsubmit`).
6. Check Mark all SKU's as Current (`input[name='skudates'][value='1']`).
7. Click Submit (`#submitButton`).
8. Save screenshots for traceability.

## Setup

1. Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

2. Install dependencies:

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

3. Create your env file:

```bash
cp .env.example .env
```

4. Edit `.env` and set values:

```dotenv
RITHUM_URL=https://dsm.commercehub.com/dsm/gotoHome.do
RITHUM_USERNAME=your-email@example.com
RITHUM_PASSWORD=your-password
HEADLESS=false
TIMEOUT_MS=30000
```

## Run

```bash
python run_all.py
```

Run only Rithum:

```bash
python run_rithum.py
```

Run only SPS:

```bash
python run_sps.py
```

Screenshots are saved under `screenshots/`.

## Scheduling (Linux cron example)

Run daily at 7:00 AM:

```bash
0 7 * * * /workspaces/Inventory-Automation/run_daily.sh >> /workspaces/Inventory-Automation/cron.log 2>&1
```

Install the cron job:

```bash
crontab -e
```

Then paste the line above and save.

The scheduled job runs both automations by calling `run_all.py` through the project virtual environment.

## Scheduling (systemd timer)

For a more reliable scheduler than cron, use a `systemd` service and timer. This gives you better logging, restart behavior, and catch-up handling if the machine was off at the scheduled time.

Copy the included unit files into your systemd directory:

```bash
sudo cp systemd/inventory-automation.service /etc/systemd/system/
sudo cp systemd/inventory-automation.timer /etc/systemd/system/
```

Enable and start the timer:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now inventory-automation.timer
```

Check status:

```bash
systemctl list-timers inventory-automation.timer
systemctl status inventory-automation.timer
systemctl status inventory-automation.service
```

View logs:

```bash
journalctl -u inventory-automation.service -n 100 --no-pager
```

The timer is configured to run every day at 7:00 AM and to run the missed job shortly after boot if the machine was off at the scheduled time.

## Security Notes

- Do not commit `.env`.
- Use a dedicated automation account when possible.
- Rotate credentials if they were shared in chat or email.