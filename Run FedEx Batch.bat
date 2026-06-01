@echo off
setlocal
cd /d "%~dp0Inventory Submissions"

set "INV_PY=%~dp0Inventory Submissions\.venv\Scripts\python.exe"
set "RUNNER=python"
if exist "%INV_PY%" set "RUNNER=%INV_PY%"

echo.
echo FedEx Batch — upload Lowe's CSV, finalize shipments, save labels by SKU/vendor
echo.
"%RUNNER%" "run_fedex_batch.py" %*
set "ERR=%ERRORLEVEL%"

echo.
if not "%ERR%"=="0" (
  echo FedEx batch finished with errors ^(exit %ERR%^).
) else (
  echo FedEx batch finished successfully.
)
pause
exit /b %ERR%
