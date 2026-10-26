@echo off
title Talent Pool
cd /d "%~dp0"
echo Starting Talent Pool. Your browser will open in a moment.
echo Keep this window open while you work. Close it to stop the app.
python -m sourcer ui
pause
