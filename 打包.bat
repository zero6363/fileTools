@echo off
chcp 65001 >nul
cd /d "%~dp0"

:: ============ 一键打包：文件工具箱_便携版\ ============
:: 源码已按功能拆分为 toolbox/ 包；产物是 onedir 便携版文件夹
:: （整个文件夹可拷贝带走，配置和日志都写在 exe 同级，不写 AppData）。
:: 不再出单文件 exe：onefile 每次启动都要解压到临时目录，启动慢、退出也慢。

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
"%PY%" -m PyInstaller --onedir --windowed --noconfirm --clean ^
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

echo [3/3] 整理便携版文件夹 ...
set "PORTABLE=文件工具箱_便携版"
:: 删除旧便携版（可能正被运行占用，重试几次）
if exist "%PORTABLE%" (
    set "_delok="
    for /l %%i in (1,1,5) do (
        if not defined _delok (
            rmdir /s /q "%PORTABLE%" >nul 2>nul
            if not exist "%PORTABLE%" ( set "_delok=1" ) else (
                echo   旧便携版被占用，1 秒后重试 %%i/5 ...
                timeout /t 1 /nobreak >nul
            )
        )
    )
    if exist "%PORTABLE%" (
        echo [警告] 无法删除旧便携版（可能被占用），将覆盖更新。
    )
)
:: 复制整个 onedir 产物为便携版文件夹（可直接整体拷贝带走，不写系统目录）
robocopy ".pybuild\dist\文件工具箱" "%PORTABLE%" /E /R:3 /W:1 /NFL /NDL /NJH /NJS
if errorlevel 8 (
    echo [错误] 复制便携版失败，请检查上方信息或手动复制：
    echo        .pybuild\dist\文件工具箱 -^> %PORTABLE%\
    pause
    exit /b 1
)
rmdir /s /q .pybuild

echo.
echo ========================================
echo   打包完成: %PORTABLE%\  （便携版文件夹，可整体复制使用）
echo ========================================
pause
