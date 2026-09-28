@echo off
call "%~dp0run_uah.cmd" uah.tools.uah hud %*
exit /b %ERRORLEVEL%
