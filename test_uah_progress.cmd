@echo off
call "%~dp0run_uah.cmd" uah.tests.verify_progress %*
exit /b %ERRORLEVEL%
