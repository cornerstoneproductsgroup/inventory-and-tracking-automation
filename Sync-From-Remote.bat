@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo.
echo Sync-From-Remote — make THIS PC match GitHub ^(your dev PC pushes^)
echo Folder: %CD%
echo.
echo WARNING: Uncommitted changes on this PC will be discarded.
echo          Back up vendor_email_config.json first if you edited it here.
echo.
pause

if exist ".git\MERGE_HEAD" (
  echo Aborting unfinished merge...
  git merge --abort
  if errorlevel 1 (
    echo merge --abort failed.
    pause
    exit /b 1
  )
)

echo Fetching from remote...
git fetch origin
if errorlevel 1 (
  echo fetch failed.
  pause
  exit /b 1
)

for /f "delims=" %%b in ('git rev-parse --abbrev-ref HEAD 2^>nul') do set "BRANCH=%%b"
if not defined BRANCH set "BRANCH=main"

echo Resetting to origin/!BRANCH! ...
git reset --hard "origin/!BRANCH!"
set "ERR=%ERRORLEVEL%"

echo.
if not "%ERR%"=="0" (
  echo Sync failed ^(exit %ERR%^). Try: git branch -vv
) else (
  git status -sb
  echo.
  echo Done. This PC now matches the remote ^(same as your dev PC after Push-All^).
)
pause
exit /b %ERR%
