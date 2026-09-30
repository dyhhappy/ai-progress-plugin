@echo off
call "%~dp0run_uah.cmd" uah.tools.report_progress %*
exit /b %ERRORLEVEL%
