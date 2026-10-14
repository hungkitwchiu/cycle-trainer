@echo off
setlocal
rem ---------------------------------------------------------------------------
rem  CycleTrainer launcher. Double-click to run.
rem  The Python environment lives in %LOCALAPPDATA%\CycleTrainer\venv so that
rem  thousands of library files are not synced by Google Drive.
rem ---------------------------------------------------------------------------
set "APPDIR=%~dp0"
set "VENV=%LOCALAPPDATA%\CycleTrainer\venv"
set PYTHONDONTWRITEBYTECODE=1
title CycleTrainer

if exist "%VENV%\Scripts\python.exe" goto :deps

echo First run: setting up Python environment (one time, ~1 minute)...
set "PY="
for %%V in (3.14 3.13 3.12) do (
  if not defined PY (
    py -%%V -c "import sys" >nul 2>nul && set "PY=py -%%V"
  )
)
if not defined PY (
  python -c "import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)" >nul 2>nul && set "PY=python"
)
if not defined PY goto :nopython
%PY% -m venv "%VENV%"
if errorlevel 1 goto :nopython

:deps
"%VENV%\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r "%APPDIR%requirements.txt"
if errorlevel 1 (
  echo.
  echo Installing dependencies failed - check your internet connection and try again.
  pause
  exit /b 1
)
"%VENV%\Scripts\python.exe" "%APPDIR%run.py" %*
if errorlevel 1 pause
exit /b 0

:nopython
echo.
echo Python 3.12 or newer is required.
echo Install it with:   winget install Python.Python.3.13
echo (or from https://www.python.org/downloads/ - tick "Add python.exe to PATH")
echo then double-click start.bat again.
pause
exit /b 1
