@echo off
rem port-gui.bat - 启动 portkit 图形界面（用 pythonw 启动，不留黑色控制台窗口）
setlocal
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "%~dp0portkit_gui.py"
    goto :done
)

rem pythonw 不在 PATH 时，从 python 所在目录推导它的位置
for /f "delims=" %%I in ('python -c "import os,sys;print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))"') do set "PYTHONW_PATH=%%I"
if exist "%PYTHONW_PATH%" (
    start "" "%PYTHONW_PATH%" "%~dp0portkit_gui.py"
) else (
    python "%~dp0portkit_gui.py"
)

:done
endlocal
