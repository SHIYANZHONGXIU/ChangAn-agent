@echo off
chcp 65001 >nul
setlocal
set PYTHONUTF8=1
cd /d "%~dp0"
if not exist "%~dp0logs" mkdir "%~dp0logs"
start "" "%~dp0logs"
if errorlevel 1 echo 操作失败，请查看上面的错误说明。
pause
endlocal
