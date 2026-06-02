# Scheduled morning workflow

Runs a fixed sequence of automation steps on a daily schedule (default **5:00 AM local**).

## Default steps (in order)

1. **Pull Orders** — CommerceHub PDF/CSV, SPS Tractor/Grainger, warehouse print  
2. **FedEx Batch** — upload Lowe's CSV, finalize labels, save `Lowe's Fedex Master.xlsx`  
3. **All Invoice Reports** — Depot, Lowe's, Tractor Supply (previous business day)  
4. **All Inventories** — CommerceHub Rithum + SPS Tractor Supply (no tracking/invoicing)

**On hold (not scheduled):** Amazon Seller CSV download — see `scheduled_workflow.json` → `_on_hold_steps` and `invoice report/AMAZON_SELLER_DOWNLOAD.md`.

Later steps continue even if invoice reports fail (same as the full workflow). Pull Orders and FedEx Batch stop the chain on failure.

## Quick start

```powershell
# Preview steps (no browser)
python run_scheduled_workflow.py --dry-run

# Run manually
Run Scheduled Workflow.bat

# Install Windows Task Scheduler job (~5:00 AM daily)
Install-Morning-Schedule-Task.bat

If the window closes instantly, use **Install-Morning-Schedule-Task (Debug).bat** or run from an open cmd window so you can read errors.
```

Log file: `logs\scheduled_workflow.log`

Silent runner for Task Scheduler: `Run-Scheduled-Workflow-Silent.bat` (legacy name with parentheses still works via stub).

## Change time or add steps

Edit **`scheduled_workflow.json`**:

| Field | Purpose |
|--------|---------|
| `schedule.time_local` | Daily run time, 24h `HH:MM` (default `05:00`) |
| `schedule.task_name` | Windows Task Scheduler name |
| `steps[]` | Ordered list — set `enabled: false` to skip |
| `steps[].continue_on_error` | `false` = stop chain; `true` = log and continue |

Environment overrides:

- `SCHEDULED_WORKFLOW_TIME=05:30` — used when installing the task  
- `SCHEDULED_WORKFLOW_CONFIG=path\to\custom.json`

### Add a step later

Copy an entry in `steps` (see `_future_steps_examples` in the JSON):

```json
{
  "id": "worldship_import",
  "label": "WorldShip Batch Import",
  "enabled": true,
  "continue_on_error": true,
  "type": "workflow",
  "args": ["--worldship-import-only"]
}
```

Or call any script under the repo:

```json
{
  "id": "custom_script",
  "label": "My script",
  "enabled": true,
  "continue_on_error": true,
  "type": "script",
  "script": "Inventory Submissions/run_something.py",
  "cwd": "Inventory Submissions",
  "args": []
}
```

## Requirements

- Windows user **logged in** at run time (Playwright browsers need an interactive session).  
- `Inventory Submissions\.venv` with deps installed (`Install-Deps.bat`).  
- `.env` credentials configured for CommerceHub, FedEx, SPS, etc.

## Remove the schedule

```bat
Uninstall-Morning-Schedule-Task.bat
```

Or delete the task in **Task Scheduler** (`taskschd.msc`).

## CLI

```powershell
python run_scheduled_workflow.py --dry-run
python run_scheduled_workflow.py --only pull_orders fedex_batch
python run_scheduled_workflow.py --show-schedule
```
