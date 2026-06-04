@echo off
setlocal
cd /d "%~dp0"

echo.
echo Pull-All — download latest from git remote
echo Folder: %CD%
echo.

git pull
set "ERR=%ERRORLEVEL%"

echo.
if not "%ERR%"=="0" (
  echo Pull failed ^(exit %ERR%^).
  echo.
  echo If vendor_email_config.json conflicts, back it up, then either:
  echo   git stash
  echo   git pull
  echo   git stash pop
  echo or resolve the conflict and run Pull-All again.
) else (
  echo Done. This PC has the latest code from the remote.
)
pause
exit /b %ERR%
