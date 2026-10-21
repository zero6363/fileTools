@echo off
chcp 65001 >nul
cd /d "%~dp0"

:: ============ 一键打包：文件工具箱.exe ============
:: 源码已按功能拆分为 toolbox/ 包，此处打包仍为单个无控制台 exe

set "PY=%LOCALAPPDATA%\Programs\Python\Python315\python.exe"
if not exist "%PY%" (
    where python >nul 2>nul && set "PY=python" || (
        echo [错误] 未找到 Python，请先安装 Python 3.10+
        pause
        exit /b 1
    )
)

:: 检查并安装 PyInstaller
"%PY%" -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
    echo [提示] 正在安装 PyInstaller ...
    "%PY%" -m pip install pyinstaller
    if errorlevel 1 (
        echo [错误] PyInstaller 安装失败
        pause
        exit /b 1
    )
)

echo [1/3] 清理旧构建 ...
if exist .pybuild rmdir /s /q .pybuild

echo [2/3] PyInstaller 打包中（约 1 分钟，请稍候）...
"%PY%" -m PyInstaller --onefile --windowed --noconfirm --clean ^
    --name 文件工具箱 ^
    --add-data "%CD%\bin\mozjpeg;bin/mozjpeg" ^
    --add-binary "%CD%\bin\mediainfo\MediaInfo.dll;bin/mediainfo" ^
    --add-binary "%CD%\bin\mediainfo\LIBCURL.DLL;bin/mediainfo" ^
    --hidden-import PIL.Image ^
    --hidden-import PIL.ImageDraw ^
    --hidden-import PIL.ImageFont ^
    --hidden-import Crypto.Cipher.AES ^
    --hidden-import Crypto.Util._raw_api ^
    --distpath .pybuild\dist ^
    --workpath .pybuild\build ^
    --specpath .pybuild ^
    文件工具箱.py
if errorlevel 1 (
    echo.
    echo [错误] 打包失败，请检查上方错误信息。
    pause
    exit /b 1
)

echo [3/3] 复制到根目录并清理 ...
:: 杀毒软件可能瞬时占用旧 exe，重试几次再报错
set "_copied="
for /l %%i in (1,1,5) do (
    if not defined _copied (
        copy /y ".pybuild\dist\文件工具箱.exe" "文件工具箱.exe" >nul 2>nul
        if not errorlevel 1 set "_copied=1"
        if not defined _copied (
            echo   复制被占用，1 秒后重试 %%i/5 ...
            timeout /t 1 /nobreak >nul
        )
    )
)
if not defined _copied (
    echo [错误] 复制 exe 失败：文件被其他程序占用
    echo        请关闭正在运行的文件工具箱后重试，或手动复制：
    echo        .pybuild\dist\文件工具箱.exe -^> 文件工具箱.exe
    pause
    exit /b 1
)
rmdir /s /q .pybuild

echo.
echo ========================================
echo   打包完成: 文件工具箱.exe
echo ========================================
pause
