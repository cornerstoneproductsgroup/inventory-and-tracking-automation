@echo off
setlocal
cd /d "%~dp0Inventory Submissions"

set "INV_PY=%~dp0Inventory Submissions\.venv\Scripts\python.exe"
set "RUNNER=python"
if exist "%INV_PY%" set "RUNNER=%INV_PY%"

echo.
echo FedEx Manual Login Test
echo - Opens FedEx SIGN-IN in Microsoft Edge (not Playwright Chromium)
echo - Uses fedex_browser_profile so cookies work like your normal browser
echo - YOU type username and password (script does not auto-fill)
echo - Verifies batch Upload page loads, then saves fedex_storage_state.json
echo - No CSV upload or label processing
echo.
"%RUNNER%" "run_fedex_batch.py" --login-test
set "ERR=%ERRORLEVEL%"

echo.
if not "%ERR%"=="0" (
  echo FedEx manual login test finished with errors ^(exit %ERR%^).
) else (
  echo FedEx manual login test succeeded — session saved for future runs.
)
pause
exit /b %ERR%
