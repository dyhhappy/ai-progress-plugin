@echo off
setlocal
call "%~dp0run_uah.cmd" uah.tools.desktop_launch --background
exit /b %ERRORLEVEL%
