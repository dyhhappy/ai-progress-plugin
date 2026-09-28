@echo off
setlocal
echo Background checks only: this will NOT change the open HUD.
call "%~dp0run_uah.cmd" uah.tests.verify_progress %*
set "UAH_RESULT=%ERRORLEVEL%"
if not "%~1"=="" exit /b %UAH_RESULT%
echo.
echo Tests finished. Exit code: %UAH_RESULT%
pause
exit /b %UAH_RESULT%
