@echo off
REM Legacy name — forwards to Run-Scheduled-Workflow-Silent.bat (no parentheses in path).
call "%~dp0Run-Scheduled-Workflow-Silent.bat" %*
exit /b %ERRORLEVEL%
