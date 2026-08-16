@echo off
rem port.bat - portkit 的 Windows 快捷入口，可直接执行: port check 3000 / port kill 3000
setlocal
where python >nul 2>nul
if %errorlevel%==0 (
    python "%~dp0portkit.py" %*
) else (
    py "%~dp0portkit.py" %*
)
endlocal
