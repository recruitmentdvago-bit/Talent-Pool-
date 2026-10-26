@echo off
title Talent Pool - one-time setup
cd /d "%~dp0"
python --version >nul 2>&1
if errorlevel 1 (
  echo Python is not installed, or "Add python.exe to PATH" was not ticked.
  echo Install it from https://www.python.org/downloads/ and run this again.
  pause
  exit /b 1
)
echo Installing the libraries (this takes a couple of minutes the first time)...
python -m pip install -r requirements.txt
if errorlevel 1 ( echo Install failed - send a screenshot of this window. & pause & exit /b 1 )
echo.
echo Installing the backup browser (only used if Google Chrome is missing)...
python -m playwright install chromium
python -m sourcer init
echo.
echo All set. Now double-click  2_open_app.bat
pause
