@echo off
setlocal
cd /d "C:\Chat GPT Automation\Inventory Submissions"
"C:\Chat GPT Automation\Inventory Submissions.venv\Scripts\python.exe" run_all.py >> "C:\Chat GPT Automation\Inventory Submissions\cron.log" 2>&1
