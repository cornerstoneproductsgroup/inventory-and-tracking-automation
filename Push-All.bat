@echo off
setlocal
cd /d "%~dp0"

set "MSG=%~1"
if not defined MSG set "MSG=Update automation"

echo.
echo Push-All — stage, commit, push entire repo
echo Message: %MSG%
echo.

git add -A
git diff --cached --quiet
if %ERRORLEVEL%==0 (
  echo Nothing new to commit.
) else (
  git commit -m "%MSG%"
  if errorlevel 1 (
    echo Commit failed.
    pause
    exit /b 1
  )
)

git push
set "ERR=%ERRORLEVEL%"

echo.
if not "%ERR%"=="0" (
  echo Push failed ^(exit %ERR%^).
) else (
  echo Done. Remote is up to date with your latest commit.
)
pause
exit /b %ERR%
