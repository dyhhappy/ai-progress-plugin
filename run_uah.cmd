@echo off
setlocal
if "%~1"=="" (
  echo This is an internal launcher. Choose one of these files:
  echo   start_uah_hud.cmd     - Open the HUD
  echo   demo_uah_progress.cmd - Show a demo task
  echo   test_uah_progress.cmd - Run background checks
  echo.
  pause
  exit /b 0
)
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "UAH_BOOT_PY="
if defined UAH_PYTHON (
  if not exist "%UAH_PYTHON%" (
    echo [UAH] UAH_PYTHON does not exist: "%UAH_PYTHON%"
    exit /b 2
  )
  set "UAH_BOOT_PY=%UAH_PYTHON%"
)
if not defined UAH_BOOT_PY if exist "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" set "UAH_BOOT_PY=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if defined UAH_BOOT_PY goto found
where py >nul 2>nul
if not errorlevel 1 goto py_launcher
python -c "import sys" >nul 2>nul
if errorlevel 1 (
  echo [UAH] Python unavailable. Set UAH_PYTHON to a Python with tkinter.
  exit /b 2
)
python -m %*
exit /b %ERRORLEVEL%
:found
"%UAH_BOOT_PY%" -m %*
exit /b %ERRORLEVEL%

:py_launcher
py -3 -m %*
exit /b %ERRORLEVEL%
