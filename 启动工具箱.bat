@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM Prefer the system Python that has tkinter.
REM NOTE: pythonw on PATH may be a managed build WITHOUT tkinter, so check explicitly.
set "PYW=%LOCALAPPDATA%\Programs\Python\Python315\pythonw.exe"

if exist "%PYW%" (
    "%PYW%" -c "import tkinter" >nul 2>nul
    if not errorlevel 1 (
        start "" "%PYW%" "%~dp0文件工具箱.py"
        exit /b 0
    )
)

echo [Hint] System Python with tkinter not found, falling back to console python.
echo        Any error message will be shown below - please screenshot and report.
echo.
python "%~dp0文件工具箱.py"
pause
