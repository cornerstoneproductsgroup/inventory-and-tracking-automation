@echo off
setlocal
cd /d "%~dp0"

echo.
echo Pull-All — download latest from git remote
echo Folder: %CD%
echo.
echo Use this PC for PULL only. Use Push-All.bat on your DEV PC only.
echo.

if exist ".git\MERGE_HEAD" (
  echo Unfinished merge detected ^(MERGE_HEAD^) — aborting...
  git merge --abort
  if errorlevel 1 (
    echo merge --abort failed. Run Sync-From-Remote.bat instead.
    pause
    exit /b 1
  )
  echo Merge aborted.
  echo.
)

git pull
set "ERR=%ERRORLEVEL%"

echo.
if not "%ERR%"=="0" (
  echo Pull failed ^(exit %ERR%^).
  echo.
  echo Recovery on WorldShip ^(match dev PC exactly^):
  echo   1. Back up vendor_email_config.json if needed
  echo   2. Double-click Sync-From-Remote.bat
  echo.
  echo Or manual commands:
  echo   git merge --abort
  echo   git fetch origin
  echo   git reset --hard origin/main
  echo   ^(use origin/master if that is your branch^)
) else (
  git status -sb
  echo.
  echo Done. This PC has the latest code from the remote.
)
pause
exit /b %ERR%
