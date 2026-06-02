@echo off
setlocal
cd /d "%~dp0"

set "INV_PY=%~dp0Inventory Submissions\.venv\Scripts\python.exe"
set "RUNNER=python"
if exist "%INV_PY%" set "RUNNER=%INV_PY%"

echo.
echo Vendor Emails (Outlook)
echo   1  Dry run preview (no emails sent)
echo   2  Send emails now
echo.
choice /C 12 /N /M "Choose 1 or 2: "
if errorlevel 2 goto SEND
if errorlevel 1 goto PREVIEW

:PREVIEW
echo.
echo Running dry run...
"%RUNNER%" "Inventory Submissions\run_vendor_emails.py"
set "ERR=%ERRORLEVEL%"
goto DONE

:SEND
echo.
echo Sending emails...
"%RUNNER%" "Inventory Submissions\run_vendor_emails.py" --send
set "ERR=%ERRORLEVEL%"
goto DONE

:DONE
echo.
if not "%ERR%"=="0" (
  echo Vendor emails finished with errors ^(exit %ERR%^).
) else (
  echo Vendor emails finished successfully.
)
echo.
pause
exit /b %ERR%
