@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo  DXMapper Suite - installer for FM Tuner, RDS Monitor and DX Mapper
echo ============================================================
echo.

set "PY=py -3"
py -3 --version >nul 2>&1
if errorlevel 1 (
    set "PY=python"
    python --version >nul 2>&1
    if errorlevel 1 (
        echo [X] Python 3 was not found.
        echo     Install Python 3.10+ ^(64-bit^) from
        echo     https://www.python.org/downloads/windows/
        echo     and tick "Add python.exe to PATH" during setup.
        goto :end
    )
)
for /f "delims=" %%v in ('%PY% --version') do echo [ok] %%v ^(launched as "%PY%"^)

%PY% -c "import tkinter" >nul 2>&1
if errorlevel 1 (
    echo [X] tkinter is missing - reinstall Python with "tcl/tk and IDLE" ticked.
    goto :end
)
echo [ok] tkinter present
echo.
%PY% -m pip install --upgrade pip

echo.
echo --- numpy, sounddevice, scipy ^(required^); requests, pillow, pyserial, pywebview ^(optional^)
%PY% -m pip install -r "requirements.txt"
if errorlevel 1 (
    echo [!] Some packages failed - see the messages above. numpy,
    echo     sounddevice and scipy are required; on a brand-new Python ^(3.14+^) they may
    echo     have no wheel yet - install Python 3.12 ^(64-bit^) and run this again.
    echo     The others are optional: logos, USB GPS and the map's own window.
) else (
    echo [ok] All packages installed
)

echo.
if exist "rtlsdr.dll" (
    echo [ok] rtlsdr.dll found
) else (
    echo [!] rtlsdr.dll is NOT in this folder. Download
    echo     https://github.com/rtlsdrblog/rtl-sdr-blog/releases/latest/download/Release.zip
    echo     and copy rtlsdr.dll and libusb-1.0.dll from its x64 folder into
    echo     %~dp0
)
echo.
echo One manual step if not done before: run Zadig ^(https://zadig.akeo.ie/^)
echo and set the dongle's driver to WinUSB - see INSTALL.txt, FM Tuner step 3.
echo.
echo Then double-click "Start Radio.pyw" to open all three parts,
echo or FM_Tuner.py, RDS Monitor.py or DX Mapper.py to open just one.

:end
echo.
pause
endlocal
