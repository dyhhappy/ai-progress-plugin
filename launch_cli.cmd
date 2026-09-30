@echo off
setlocal
call "%~dp0run_uah.cmd" uah.tools.agent_launch --choose --hud
exit /b %ERRORLEVEL%
