@echo off
REM AXIOM-Kronos runtime setup (Windows).
REM The base AXIOM/Arena runtime does NOT need any of this.
REM
REM 1) Create an isolated virtual environment for the Kronos process
py -3.11 -m venv .venv-kronos
call .venv-kronos\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements-kronos.txt

REM 2) Kronos is not on PyPI: clone the upstream implementation
if not exist "%LOCALAPPDATA%\AXIOM\Kronos" (
  git clone https://github.com/shiyu-coder/Kronos "%LOCALAPPDATA%\AXIOM\Kronos"
)
setx AXIOM_KRONOS_SOURCE "%LOCALAPPDATA%\AXIOM\Kronos"

REM 3) Verified inference does NOT download weights automatically. Stage the
REM    approved model/tokenizer files under %%LOCALAPPDATA%%\AXIOM\models\kronos,
REM    then record and verify the SHA-256 manifests before enabling inference.
echo.
echo AXIOM-Kronos runtime ready. Start the terminal with:
echo   python -m desktop
call .venv-kronos\Scripts\deactivate.bat
