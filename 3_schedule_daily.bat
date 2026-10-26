@echo off
title Talent Pool - daily schedule
cd /d "%~dp0"
set /p T="What time should it run each day? (24h, e.g. 10:30) [10:30]: "
if "%T%"=="" set T=10:30
schtasks /Create /F /SC DAILY /ST %T% /TN "Talent Pool daily sourcing" /TR "\"%~dp0daily_run.bat\""
echo.
echo Scheduled for %T% every day. It only runs while you are logged in to Windows,
echo because LinkedIn is opened in a normal visible Chrome window.
echo To remove it:  schtasks /Delete /TN "Talent Pool daily sourcing" /F
pause
