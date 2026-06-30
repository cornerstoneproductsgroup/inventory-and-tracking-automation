@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "INV_PY=%~dp0Inventory Submissions\.venv\Scripts\python.exe"
set "RUNNER=python"
if exist "%INV_PY%" set "RUNNER=%INV_PY%"

set "EXTRA_ARGS="
if not "%~1"=="" goto RUN

:MAIN_MENU
echo.
echo ============================================================
echo   Full Workflow — main menu
echo ============================================================
echo   F  FedEx Batch ^(Lowe's CSV upload, finalize, labels^)
echo   W  WorldShip / UPS shipping
echo   O  Vendor Emails ^(Outlook — ALL or pick one vendor^)
echo   0  Pull Orders ^(CommerceHub PDF/CSV, SPS, warehouse print^)
echo   S  Scheduled morning chain ^(see SCHEDULED_WORKFLOW.md^)
echo   1  All Steps ^(vendor emails, invoice reports, inventories, tracking/invoicing^)
echo   T  Tracking / Invoicing ^(submenu^)
echo   I  Inventory ^(submenu^)
echo   R  Invoice Reports ^(submenu^)
echo   9  Custom Invoice Report Date ^(pick retailer, then enter date^)
echo.
echo Invoice reports need commercehub_invoice_export.py in "invoice report" folder.
echo.
choice /C FWO01TIR9S /N /M "Press F, W, O, 0, 1, T, I, R, 9, or S: "
REM choice sets ERRORLEVEL to key index: F=1 W=2 O=3 0=4 1=5 T=6 I=7 R=8 9=9 S=10
REM "if errorlevel N" means ERRORLEVEL >= N — test highest index first.
if errorlevel 10 goto OPT_S
if errorlevel 9 goto OPT_9
if errorlevel 8 goto SUBMENU_INVOICE
if errorlevel 7 goto SUBMENU_INVENTORY
if errorlevel 6 goto SUBMENU_TRACKING
if errorlevel 5 goto OPT_1
if errorlevel 4 goto OPT_0
if errorlevel 3 goto OPT_O
if errorlevel 2 goto OPT_W
if errorlevel 1 goto OPT_F
goto MAIN_MENU

:OPT_F
set "EXTRA_ARGS=--fedex-batch-only"
goto RUN

:OPT_W
goto SUBMENU_W

:SUBMENU_W
cls
echo.
echo ============================================================
echo   WorldShip / UPS shipping
echo ============================================================
echo   1  WorldShip Batch Import ^(CornerstoneMaster — default^)
echo   2  UPS.com Online Batch ^(Depot / Special Order / Tractor^)
echo   3  Export Tracking ^(Batch Export — Depot Shipments CSV^)
echo   0  Back to main menu
echo.
choice /C 0123 /N /M "Press 0-3: "
REM 0=1 1=2 2=3 3=4
if errorlevel 4 goto WORLDSHIP_EXPORT
if errorlevel 3 goto SUBMENU_UPS
if errorlevel 2 goto WORLDSHIP_IMPORT
if errorlevel 1 goto MAIN_MENU
goto SUBMENU_W

:WORLDSHIP_EXPORT
set "EXTRA_ARGS=--worldship-export-only"
goto RUN

:WORLDSHIP_IMPORT
set "EXTRA_ARGS=--worldship-import-only"
goto RUN

:SUBMENU_UPS
cls
echo.
echo ============================================================
echo   UPS.com Online Batch
echo ============================================================
echo   1  Home Depot
echo   2  Depot Special Order
echo   3  Tractor Supply
echo   4  All three ^(Depot, Special Order, Tractor^)
echo   5  Void today's shipments ^(Shipping History^)
echo   0  Back to main menu
echo.
choice /C 012345 /N /M "Press 0-5: "
REM 0=1 1=2 2=3 3=4 4=5 5=6
if errorlevel 6 goto UPS_VOID
if errorlevel 5 goto UPS_ALL
if errorlevel 4 goto UPS_TRACTOR
if errorlevel 3 goto UPS_THDSO
if errorlevel 2 goto UPS_DEPOT
if errorlevel 1 goto SUBMENU_W
goto SUBMENU_UPS

:UPS_DEPOT
set "EXTRA_ARGS=--ups-online-batch-only --ups-online-batch-lane depot"
goto RUN

:UPS_THDSO
set "EXTRA_ARGS=--ups-online-batch-only --ups-online-batch-lane thdso"
goto RUN

:UPS_TRACTOR
set "EXTRA_ARGS=--ups-online-batch-only --ups-online-batch-lane tractor"
goto RUN

:UPS_ALL
set "EXTRA_ARGS=--ups-online-batch-only --ups-online-batch-lane all"
goto RUN

:UPS_VOID
call "%~dp0Run UPS Void Shipments.bat"
goto MAIN_MENU

:OPT_O
set "EXTRA_ARGS=--vendor-emails-only"
goto RUN

:OPT_0
set "EXTRA_ARGS=--pull-orders-only"
goto RUN

:OPT_S
goto RUN_SCHEDULED

:OPT_1
set "EXTRA_ARGS=--with-vendor-emails --invoice-report-modes all --run-grainger-all"
goto RUN

:OPT_9
goto SUBMENU_INVOICE_CUSTOM_DATE

:SUBMENU_INVOICE_CUSTOM_DATE
cls
echo.
echo ============================================================
echo   Custom Invoice Report Date
echo ============================================================
echo   1  All ^(Depot, Lowe's, Tractor Supply^)
echo   2  Depot
echo   3  Lowe's
echo   4  Tractor Supply
echo   0  Back to main menu
echo.
choice /C 01234 /N /M "Press 0-4: "
REM 0=1 1=2 2=3 3=4 4=5
if errorlevel 5 goto IR9_TRACTOR
if errorlevel 4 goto IR9_LOWES
if errorlevel 3 goto IR9_DEPOT
if errorlevel 2 goto IR9_ALL
if errorlevel 1 goto MAIN_MENU
goto SUBMENU_INVOICE_CUSTOM_DATE

:IR9_PROMPT_DATE
echo.
set /p INVOICE_DATE="Enter invoice date (MM/DD/YYYY or YYYY-MM-DD): "
if not defined INVOICE_DATE (
  echo No date entered — cancelled.
  pause
  goto MAIN_MENU
)
set "EXTRA_ARGS=--invoice-report-only --invoice-report-modes %IR9_MODE% --invoice-report-date %INVOICE_DATE%"
goto RUN

:IR9_ALL
set "IR9_MODE=all"
goto IR9_PROMPT_DATE

:IR9_DEPOT
set "IR9_MODE=depot"
goto IR9_PROMPT_DATE

:IR9_LOWES
set "IR9_MODE=lowes"
goto IR9_PROMPT_DATE

:IR9_TRACTOR
set "IR9_MODE=tractor"
goto IR9_PROMPT_DATE

:SUBMENU_TRACKING
cls
echo.
echo ============================================================
echo   Tracking / Invoicing
echo ============================================================
echo   1  All ^(CommerceHub + SPS: Depot, Lowe's, Special Orders, Tractor, Grainger^)
echo   2  CommerceHub ALL ^(Depot, Lowe's, Depot Special Orders^)
echo   3  SPS Commerce ALL ^(Tractor Supply + Grainger^)
echo   4  Depot ^(regular Home Depot tracking/invoicing only^)
echo   5  Lowe's
echo   6  Depot Special Orders ^(thdso only^)
echo   7  Tractor Supply
echo   8  Grainger
echo   0  Back to main menu
echo.
choice /C 012345678 /N /M "Press 0-8: "
REM 0=1 1=2 2=3 3=4 4=5 5=6 6=7 7=8 8=9
if errorlevel 9 goto TI_GRAINGER
if errorlevel 8 goto TI_TRACTOR
if errorlevel 7 goto TI_SPECIAL
if errorlevel 6 goto TI_LOWES
if errorlevel 5 goto TI_DEPOT
if errorlevel 4 goto TI_SPS_ALL
if errorlevel 3 goto TI_CH_ALL
if errorlevel 2 goto TI_ALL
if errorlevel 1 goto MAIN_MENU
goto SUBMENU_TRACKING

:TI_ALL
set "EXTRA_ARGS=--skip-invoice-report --tracking-invoicing-only --run-grainger-all"
goto RUN

:TI_CH_ALL
set "EXTRA_ARGS=--skip-invoice-report --tracking-invoicing-only --skip-sps-inventory --skip-sps-tracking"
goto RUN

:TI_SPS_ALL
set "EXTRA_ARGS=--skip-invoice-report --tracking-invoicing-only --skip-commercehub --run-grainger-all"
goto RUN

:TI_DEPOT
set "EXTRA_ARGS=--skip-invoice-report --tracking-invoicing-only --skip-sps-inventory --skip-sps-tracking --skip-lowes --skip-special-orders"
goto RUN

:TI_LOWES
set "EXTRA_ARGS=--skip-invoice-report --tracking-invoicing-only --skip-sps-inventory --skip-sps-tracking --skip-depot --skip-special-orders"
goto RUN

:TI_SPECIAL
set "EXTRA_ARGS=--skip-invoice-report --tracking-invoicing-only --skip-sps-inventory --skip-sps-tracking --skip-depot --skip-lowes"
goto RUN

:TI_TRACTOR
set "EXTRA_ARGS=--skip-invoice-report --tracking-invoicing-only --skip-commercehub --skip-grainger"
goto RUN

:TI_GRAINGER
set "EXTRA_ARGS=--skip-invoice-report --tracking-invoicing-only --skip-commercehub --grainger-only"
goto RUN

:SUBMENU_INVENTORY
cls
echo.
echo ============================================================
echo   Inventory
echo ============================================================
echo   1  All ^(CommerceHub + SPS Tractor Supply inventory^)
echo   2  CommerceHub ALL ^(Lowe's + Home Depot IBL — submitted together^)
echo   3  Depot ^(same as CommerceHub ALL — both retailers on one form^)
echo   4  Lowe's ^(same as CommerceHub ALL — both retailers on one form^)
echo   5  Tractor Supply ^(SPS inventory only^)
echo   0  Back to main menu
echo.
choice /C 012345 /N /M "Press 0-5: "
REM 0=1 1=2 2=3 3=4 4=5 5=6
if errorlevel 6 goto INV_TRACTOR
if errorlevel 3 goto INV_CH_ALL
if errorlevel 2 goto INV_ALL
if errorlevel 1 goto MAIN_MENU
goto SUBMENU_INVENTORY

:INV_ALL
set "EXTRA_ARGS=--skip-invoice-report --skip-sps-tracking --skip-depot --skip-lowes --skip-special-orders"
goto RUN

:INV_CH_ALL
set "EXTRA_ARGS=--skip-invoice-report --skip-sps-tracking --skip-sps-inventory --skip-depot --skip-lowes --skip-special-orders"
goto RUN

:INV_TRACTOR
set "EXTRA_ARGS=--skip-invoice-report --skip-commercehub --skip-sps-tracking --skip-depot --skip-lowes --skip-special-orders"
goto RUN

:SUBMENU_INVOICE
cls
echo.
echo ============================================================
echo   Invoice Reports
echo ============================================================
echo   1  All ^(Depot, Lowe's, Tractor Supply — previous business day^)
echo   2  CommerceHub ALL ^(Depot + Lowe's^)
echo   3  Depot
echo   4  Lowe's
echo   5  Tractor Supply
echo   6  Amazon ^(Seller Central Deferred Transaction CSV to Input share^)
echo   0  Back to main menu
echo.
choice /C 0123456 /N /M "Press 0-6: "
REM 0=1 1=2 2=3 3=4 4=5 5=6 6=7
if errorlevel 7 goto IR_AMAZON
if errorlevel 6 goto IR_TRACTOR
if errorlevel 5 goto IR_LOWES
if errorlevel 4 goto IR_DEPOT
if errorlevel 3 goto IR_CH_ALL
if errorlevel 2 goto IR_ALL
if errorlevel 1 goto MAIN_MENU
goto SUBMENU_INVOICE

:IR_ALL
set "EXTRA_ARGS=--invoice-report-only --invoice-report-modes all"
goto RUN

:IR_CH_ALL
set "EXTRA_ARGS=--invoice-report-only --invoice-report-modes retail"
goto RUN

:IR_DEPOT
set "EXTRA_ARGS=--invoice-report-only --invoice-report-modes depot"
goto RUN

:IR_LOWES
set "EXTRA_ARGS=--invoice-report-only --invoice-report-modes lowes"
goto RUN

:IR_TRACTOR
set "EXTRA_ARGS=--invoice-report-only --invoice-report-modes tractor"
goto RUN

:IR_AMAZON
set "EXTRA_ARGS=--amazon-seller-download-only"
goto RUN

:RUN
echo.
echo Running workflow...
if /I not "%RUNNER%"=="python" echo Using: %RUNNER%
if defined EXTRA_ARGS echo Options: %EXTRA_ARGS%
echo.

"%RUNNER%" "run_full_workflow.py" %EXTRA_ARGS% %*
set "ERR=%ERRORLEVEL%"
goto DONE

:RUN_SCHEDULED
echo.
echo Running scheduled workflow chain...
if /I not "%RUNNER%"=="python" echo Using: %RUNNER%
echo.

"%RUNNER%" "run_scheduled_workflow.py" %*
set "ERR=%ERRORLEVEL%"
goto DONE

:DONE
echo.
if not "%ERR%"=="0" (
  echo One or more steps failed ^(exit %ERR%^).
) else (
  echo All steps finished successfully.
)

pause
exit /b %ERR%
