@echo off
setlocal
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
if not errorlevel 1 (
  py -3 -m %*
  exit /b
)
python -c "import sys" >nul 2>nul
if errorlevel 1 (
  echo [UAH] Python unavailable. Set UAH_PYTHON to a Python with tkinter.
  exit /b 2
)
python -m %*
exit /b
:found
"%UAH_BOOT_PY%" -m %*
exit /b
