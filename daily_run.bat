@echo off
cd /d "%~dp0"
if not exist storage\logs mkdir storage\logs
for /f "tokens=1-3 delims=-/ " %%a in ('powershell -nologo -command "Get-Date -Format yyyy-MM-dd"') do set TODAY=%%a-%%b-%%c
echo ===== %date% %time% =====>> storage\logs\%TODAY%.log
python -m sourcer run --source pdf_inbox --source csv --source linkedin_browser >> storage\logs\%TODAY%.log 2>&1
python -m sourcer purge --yes >> storage\logs\%TODAY%.log 2>&1
