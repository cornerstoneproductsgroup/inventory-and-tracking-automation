@echo off
setlocal
cd /d "%~dp0"

set "INV_PY=%~dp0Inventory Submissions\.venv\Scripts\python.exe"
set "RUNNER=python"
if exist "%INV_PY%" set "RUNNER=%INV_PY%"

set "EXTRA_ARGS="
if not "%~1"=="" goto RUN

echo.
echo Which steps to run? ^(keyboard — no Enter needed^)
echo   1  All Steps ^(invoice all: Depot+Lowe's+Tractor, then CH+SPS inventories parallel, then tracking parallel + Grainger^)
echo   2  Depot Tracking and Invoicing
echo   3  Lowe's Tracking and Invoicing
echo   4  Commercehub and SPS Inventory + Depot Tracking Invoicing
echo   5  Commercehub and SPS Inventory + Lowe's Tracking Invoicing
echo   6  Commercehub and SPS Inventory + Depot and Lowe's Tracking Invoicing
echo   7  SPS Full ^(SPS Inventory and Tracking^)
echo   8  Commercehub Inventory
echo   9  SPS Inventory
echo   A  All Tracking and Invoicing ^(No Inventory^)
echo   B  SPS Tracking
echo   C  Grainger ALL
echo   D  Depot Invoice Report only
echo   E  Lowe's Invoice Report only
echo   F  Tractor Supply Invoice Report only
echo   I  Invoice Reports only ^(all three: Depot + Lowe's + Tractor^)
echo.
REM Order must match choice string for errorlevels ^(first char = 1, last = 16^).
choice /C 123456789ABCDEFI /N /M "Press 1-9, A-C, D-F, or I: "
if errorlevel 16 goto OPT_INVOICE_ALL
if errorlevel 15 goto OPT_INV_TRACTOR
if errorlevel 14 goto OPT_INV_LOWES
if errorlevel 13 goto OPT_INV_DEPOT
if errorlevel 12 goto OPT_GRAINGER
if errorlevel 11 goto OPT_B
if errorlevel 10 goto OPT_A
if errorlevel 9 goto OPT_9
if errorlevel 8 goto OPT_8
if errorlevel 7 goto OPT_7
if errorlevel 6 goto OPT_6
if errorlevel 5 goto OPT_5
if errorlevel 4 goto OPT_4
if errorlevel 3 goto OPT_3
if errorlevel 2 goto OPT_2
if errorlevel 1 goto OPT_1

:OPT_INVOICE_ALL
set "EXTRA_ARGS=--invoice-report-only --invoice-report-modes all"
goto RUN
:OPT_INV_TRACTOR
set "EXTRA_ARGS=--invoice-report-only --invoice-report-modes tractor"
goto RUN
:OPT_INV_LOWES
set "EXTRA_ARGS=--invoice-report-only --invoice-report-modes lowes"
goto RUN
:OPT_INV_DEPOT
set "EXTRA_ARGS=--invoice-report-only --invoice-report-modes depot"
goto RUN
:OPT_GRAINGER
set "EXTRA_ARGS=--grainger-only"
goto RUN
:OPT_B
set "EXTRA_ARGS=--skip-invoice-report --skip-commercehub --skip-sps-inventory"
goto RUN
:OPT_A
set "EXTRA_ARGS=--skip-invoice-report --tracking-invoicing-only"
goto RUN
:OPT_9
set "EXTRA_ARGS=--skip-invoice-report --skip-commercehub --skip-sps-tracking"
goto RUN
:OPT_8
set "EXTRA_ARGS=--skip-invoice-report --skip-sps-inventory --skip-sps-tracking --skip-depot --skip-lowes"
goto RUN
:OPT_7
set "EXTRA_ARGS=--skip-invoice-report --skip-commercehub"
goto RUN
:OPT_6
set "EXTRA_ARGS=--skip-invoice-report --skip-sps-tracking"
goto RUN
:OPT_5
set "EXTRA_ARGS=--skip-invoice-report --skip-sps-tracking --skip-depot"
goto RUN
:OPT_4
set "EXTRA_ARGS=--skip-invoice-report --skip-sps-tracking --skip-lowes"
goto RUN
:OPT_3
set "EXTRA_ARGS=--skip-invoice-report --skip-sps-inventory --skip-sps-tracking --skip-inventory --skip-depot"
goto RUN
:OPT_2
set "EXTRA_ARGS=--skip-invoice-report --skip-sps-inventory --skip-sps-tracking --skip-inventory --skip-lowes"
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
