@echo off
REM Double-click this to start the Good-to-Go intake agent -- watches a
REM folder for contract files and auto-routes them. Stays running in this
REM window; closing the window (or Ctrl+C) stops it.
REM
REM Configure via a .env file in this same folder (not committed to git --
REM see .env.example):
REM   GEMINI_API_KEY=...
REM   FIRESTORE_CREDENTIALS_PATH=...
REM   INTAKE_FOLDER_PATH=C:\Good_to_Go     (optional -- defaults to intake\
REM                                          inside this folder if not set)

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Could not find .venv\Scripts\python.exe
    echo.
    echo First-time setup needed -- open a terminal in this folder and run:
    echo   python -m venv .venv
    echo   .venv\Scripts\pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

echo Starting the Good-to-Go intake agent...
echo Watching folder: see INTAKE_FOLDER_PATH in .env ^(default: intake\ in this folder^)
echo Close this window, or press Ctrl+C, to stop.
echo.

".venv\Scripts\python.exe" "src\intake_agent.py"

echo.
echo The intake agent has stopped.
pause
