@echo off
REM Arena desktop launcher (Windows)
REM 1) Start the Arena core backend in a separate terminal:
REM    python -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
REM 2) Set ARENA_API_TOKEN to the value of API_TOKEN from your .env
REM 3) Run this file.
py -m venv .venv
.venv\Scripts\pip install -r requirements-desktop.txt
set ARENA_BACKEND_URL=http://127.0.0.1:8000
.venv\Scripts\python -m desktop
