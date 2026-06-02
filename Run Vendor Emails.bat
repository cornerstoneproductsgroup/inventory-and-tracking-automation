@echo off
setlocal
cd /d "%~dp0"

set "INV_PY=%~dp0Inventory Submissions\.venv\Scripts\python.exe"
set "RUNNER=python"
if exist "%INV_PY%" set "RUNNER=%INV_PY%"

echo.
echo Vendor Emails (Outlook)
echo   1  Dry run log only ^(no Outlook^)
echo   2  Preview in Outlook ^(opens each draft, no send^)
echo   3  Send emails now
echo.
choice /C 123 /N /M "Choose 1, 2, or 3: "
if errorlevel 3 goto SEND
if errorlevel 2 goto PREVIEW
if errorlevel 1 goto LOGONLY

:LOGONLY
echo.
echo Running dry run (console only)...
"%RUNNER%" "Inventory Submissions\run_vendor_emails.py"
set "ERR=%ERRORLEVEL%"
goto DONE

:PREVIEW
echo.
echo Opening Outlook previews ^(close each message, press Enter for next^)...
"%RUNNER%" "Inventory Submissions\run_vendor_emails.py" --preview
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
