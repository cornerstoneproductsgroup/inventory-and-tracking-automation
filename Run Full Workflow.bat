@echo off
setlocal
cd /d "%~dp0"

set "INV_PY=%~dp0Inventory Submissions\.venv\Scripts\python.exe"
set "RUNNER=python"
if exist "%INV_PY%" set "RUNNER=%INV_PY%"

set "EXTRA_ARGS="
if not "%~1"=="" goto RUN

echo.
echo Which steps to run? ^(press a menu key — no Enter needed^)
echo   1  All Steps ^(invoice reports, CH+SPS inventories, tracking/invoicing + Grainger^)
echo   2  All Tracking and Invoicing ^(no inventory, no invoice reports^)
echo   3  All Inventory ^(CommerceHub + SPS inventory only^)
echo   4  All Invoice Reports ^(Depot, Lowe's, Tractor Supply — previous business day^)
echo   5  CommerceHub ALL ^(Depot+Lowe's invoice reports, inventory, tracking/invoicing^)
echo   6  SPS Commerce ALL ^(Tractor invoice report, inventory, tracking/invoicing + Grainger^)
echo   7  CommerceHub Tracking/Invoicing ^(Depot and Lowe's only^)
echo   8  SPS Commerce Tracking/Invoicing ^(Tractor Supply and Grainger^)
echo   9  Custom Invoice Report Date ^(Depot, Lowe's, Tractor Supply for a date you enter^)
echo.
echo Invoice reports need the CommerceHub invoice export project ^(commercehub_invoice_export.py^):
echo   Easiest: folder named "invoice report" inside this repo ^(copy the whole project there^).
echo   Or: "CommerceHub Invoice Report (Depot and Lowe's)" inside or next to this repo, OR set COMMERCEHUB_INVOICE_REPORT_DIR.
echo   Other PC: git pull, then run Inventory Submissions\Install-Deps.bat ^(adds pandas etc. for invoice phase^).
echo.
choice /C 123456789 /N /M "Press 1-9: "
if errorlevel 9 goto OPT_CUSTOM_DATE
if errorlevel 8 goto OPT_8
if errorlevel 7 goto OPT_7
if errorlevel 6 goto OPT_6
if errorlevel 5 goto OPT_5
if errorlevel 4 goto OPT_4
if errorlevel 3 goto OPT_3
if errorlevel 2 goto OPT_2
if errorlevel 1 goto OPT_1

:OPT_CUSTOM_DATE
echo.
set /p INVOICE_DATE="Enter invoice date (MM/DD/YYYY or YYYY-MM-DD): "
if not defined INVOICE_DATE (
  echo No date entered — cancelled.
  pause
  exit /b 1
)
set "EXTRA_ARGS=--invoice-report-only --invoice-report-modes all --invoice-report-date %INVOICE_DATE%"
goto RUN
:OPT_8
set "EXTRA_ARGS=--skip-invoice-report --skip-commercehub --skip-sps-inventory --run-grainger-all"
goto RUN
:OPT_7
set "EXTRA_ARGS=--skip-invoice-report --skip-sps-inventory --skip-sps-tracking --skip-inventory"
goto RUN
:OPT_6
set "EXTRA_ARGS=--invoice-report-modes tractor --skip-commercehub --run-grainger-all"
goto RUN
:OPT_5
set "EXTRA_ARGS=--invoice-report-modes retail --skip-sps-inventory --skip-sps-tracking"
goto RUN
:OPT_4
set "EXTRA_ARGS=--invoice-report-only --invoice-report-modes all"
goto RUN
:OPT_3
set "EXTRA_ARGS=--skip-invoice-report --skip-sps-tracking --skip-depot --skip-lowes"
goto RUN
:OPT_2
set "EXTRA_ARGS=--skip-invoice-report --tracking-invoicing-only"
goto RUN
:OPT_1
set "EXTRA_ARGS=--invoice-report-modes all --run-grainger-all"
goto RUN

:RUN
echo.
echo Running workflow...
if /I not "%RUNNER%"=="python" echo Using: %RUNNER%
if defined EXTRA_ARGS echo Options: %EXTRA_ARGS%
echo.

"%RUNNER%" "run_full_workflow.py" %EXTRA_ARGS% %*
set "ERR=%ERRORLEVEL%"

echo.
if not "%ERR%"=="0" (
  echo One or more steps failed ^(exit %ERR%^).
) else (
  echo All steps finished successfully.
)

pause
exit /b %ERR%
