@echo off
setlocal
cd /d "%~dp0"

if exist "laptop_server\.venv\Scripts\python.exe" (
    "laptop_server\.venv\Scripts\python.exe" -X utf8 "build_scripts\start_web.py" %*
    goto finished
)

py -3 -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>&1
if not errorlevel 1 (
    py -3 -X utf8 "build_scripts\start_web.py" %*
    goto finished
)

python -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>&1
if not errorlevel 1 (
    python -X utf8 "build_scripts\start_web.py" %*
    goto finished
)

echo Install Python 3.12 from https://www.python.org/downloads/windows/
echo Enable 'Add python.exe to PATH' during installation, then run this file again.
pause
exit /b 1

:finished
set "WIFI_DENSEPOSE_EXIT=%ERRORLEVEL%"
if not "%WIFI_DENSEPOSE_EXIT%"=="0" pause
exit /b %WIFI_DENSEPOSE_EXIT%
