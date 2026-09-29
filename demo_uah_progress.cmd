@echo off
setlocal
if not "%~1"=="" goto with_args
echo DEMO: Start the HUD first. Select "UAH" demo Agent in its right-click menu.
echo This is simulated progress, not a real AI task.
call "%~dp0run_uah.cmd" uah.tools.progress_demo --scenario success --step-seconds 8
set "UAH_RESULT=%ERRORLEVEL%"
echo.
echo Demo finished. Exit code: %UAH_RESULT%
pause
exit /b %UAH_RESULT%
:with_args
call "%~dp0run_uah.cmd" uah.tools.progress_demo %*
exit /b %ERRORLEVEL%
