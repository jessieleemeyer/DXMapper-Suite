@echo off
cd /d "%~dp0"
where pyw >nul 2>&1 && (start "" pyw -3 "Start Radio.pyw" & exit /b)
where pythonw >nul 2>&1 && (start "" pythonw "Start Radio.pyw" & exit /b)
start "" python "Start Radio.pyw"
