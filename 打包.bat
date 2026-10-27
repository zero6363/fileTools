@echo off
chcp 65001 >nul
cd /d "%~dp0"

:: ============ 一键打包：单文件 文件工具箱.exe ============
:: 源码已按功能拆分为 toolbox/ 包；产物是单个 exe（可直接拷走，
:: 配置和日志都写在 exe 同级目录，不写 AppData）。
:: 注意：onefile 每次启动都要把内置资源解压到 %TEMP%\_MEIxxxx，
:: 退出时再删一遍 —— 这是单文件的固有代价（约 1~3 秒，取决于杀软扫描）。
:: 如果哪天更在意启动速度，把下面的 --onefile 换成 --onedir，
:: 第 3 步改成复制整个文件夹即可（产物变成 文件工具箱_便携版\）。

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

echo [2/3] PyInstaller 打包中（约 1~2 分钟，请稍候）...
"%PY%" -m PyInstaller --onefile --windowed --noconfirm --clean ^
    --name 文件工具箱 ^
    --add-data "%CD%\bin\mozjpeg;bin/mozjpeg" ^
    --add-binary "%CD%\bin\mediainfo\MediaInfo.dll;bin/mediainfo" ^
    --add-binary "%CD%\bin\mediainfo\LIBCURL.DLL;bin/mediainfo" ^
    --collect-all customtkinter ^
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

echo [3/3] 整理产物 ...
:: 覆盖根目录的旧 exe（可能正被运行占用，重试几次）
set "_ok="
for /l %%i in (1,1,5) do (
    if not defined _ok (
        copy /y ".pybuild\dist\文件工具箱.exe" "文件工具箱.exe" >nul 2>nul
        if not errorlevel 1 ( set "_ok=1" ) else (
            echo   旧 exe 被占用，2 秒后重试 %%i/5 ...
            timeout /t 2 /nobreak >nul
        )
    )
)
if not defined _ok (
    echo [错误] 无法覆盖 文件工具箱.exe（程序可能正在运行，先关掉再打包）
    pause
    exit /b 1
)

:: 顺手清掉旧的 onedir 便携版目录（不再产出，避免占 70+MB）
if exist "文件工具箱_便携版" (
    rmdir /s /q "文件工具箱_便携版" >nul 2>nul
    if exist "文件工具箱_便携版" echo [提示] 旧便携版目录删不掉（可能被占用），可手动删除。
)

rmdir /s /q .pybuild

echo.
echo ========================================
echo   打包完成: 文件工具箱.exe  （单文件，可直接拷走）
echo ========================================
pause
