@echo off
rem build_exe.bat - 一键把 portkit GUI 打包成单个 exe
rem 产物: dist\PortKit.exe
rem chcp 65001 让本脚本中的中文提示在 cmd 下正常显示

chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "OUTPUT_EXE=dist\PortKit.exe"

echo [1/4] 检查虚拟环境...
if not exist ".venv\Scripts\python.exe" (
    echo         创建 .venv ...
    python -m venv .venv || goto :failed
)

echo [2/4] 确认 PyInstaller...
".venv\Scripts\python.exe" -c "import PyInstaller" 2>nul
if errorlevel 1 (
    echo         安装 PyInstaller ...
    ".venv\Scripts\python.exe" -m pip install pyinstaller --quiet || goto :failed
)

echo [3/4] 生成图标...
".venv\Scripts\python.exe" make_icon.py || goto :failed

echo [4/4] 打包 exe（首次约需 1-2 分钟）...
".venv\Scripts\pyinstaller.exe" portkit.spec --noconfirm --clean --log-level WARN || goto :failed

echo.
if not exist "%OUTPUT_EXE%" (
    echo 打包结束，但未找到预期产物 %OUTPUT_EXE%，请检查上面的输出。
    goto :failed
)
for %%F in ("%OUTPUT_EXE%") do echo 打包完成: %%~fF  ^(%%~zF 字节^)
echo 直接双击该 exe 即可使用，无需安装 Python。
endlocal
exit /b 0

:failed
echo.
echo 打包失败，请查看上面的错误信息。
endlocal
exit /b 1
