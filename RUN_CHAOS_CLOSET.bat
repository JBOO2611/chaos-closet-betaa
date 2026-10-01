@echo off
title Chaos Closet Crosslister
cd /d "%~dp0"
echo.
echo =============================================
echo      CHAOS CLOSET CROSSLISTER
echo =============================================
echo.
if not exist ".venv\Scripts\python.exe" (
  echo First run: setting up your app...
  py -m venv .venv
  if errorlevel 1 (
    echo.
    echo Python is not installed yet.
    echo Install Python from https://www.python.org/downloads/
    echo IMPORTANT: check "Add Python to PATH" during install.
    pause
    exit /b 1
  )
  call .venv\Scripts\activate.bat
  python -m pip install --upgrade pip
  pip install -r requirements.txt
) else (
  call .venv\Scripts\activate.bat
)
if not exist "data" mkdir data
echo Starting Chaos Closet...
python app.py
pause
