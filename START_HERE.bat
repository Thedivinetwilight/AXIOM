@echo off
REM ===================================================================
REM  Arena - double-click launcher for Windows
REM
REM  If you already built Arena.exe, just run Arena.exe instead - this
REM  file is for running Arena from source, or for building the .exe.
REM ===================================================================
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo   Python was not found.
  echo.
  echo   Install Python 3.11 or newer from https://python.org/downloads
  echo   and TICK "Add python.exe to PATH" during installation.
  echo.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo.
  echo   Setting up Arena for the first time. This takes a few minutes.
  echo.
  python -m venv .venv
  if errorlevel 1 goto :failed
  .venv\Scripts\python.exe -m pip install --upgrade pip
  .venv\Scripts\python.exe -m pip install -r requirements.txt
  if errorlevel 1 goto :failed
)

echo.
echo   Starting Arena...
echo.
.venv\Scripts\python.exe arena.py %*
if errorlevel 1 goto :failed
exit /b 0

:failed
echo.
echo   Arena could not start. The message above says why.
echo   Diagnostics you can paste into a bug report:
echo.
.venv\Scripts\python.exe arena.py --diagnostics 2>nul
echo.
pause
exit /b 1
