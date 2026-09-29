@echo off
setlocal
if not "%~1"=="" goto with_args
call "%~dp0run_uah.cmd" uah.tools.agent_launch --choose --hud
exit /b %ERRORLEVEL%
:with_args
call "%~dp0run_uah.cmd" uah.tools.agent_launch %*
exit /b %ERRORLEVEL%
