@echo off
cd /d "%~dp0"
where pyw >nul 2>&1 && (start "" pyw -3 "Arrange Windows.pyw" & exit /b)
where pythonw >nul 2>&1 && (start "" pythonw "Arrange Windows.pyw" & exit /b)
start "" python "Arrange Windows.pyw"
