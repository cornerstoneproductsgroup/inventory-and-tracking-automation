@echo off
setlocal
cd /d "C:\Users\worldship\Desktop\Inventory Feeds"
call ".venv\Scripts\activate.bat"
python run_all.py
pause
