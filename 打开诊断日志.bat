@echo off
chcp 65001 >nul
setlocal
set PYTHONUTF8=1
cd /d "%~dp0"
set "CHANGAN_LOGS=%~dp0data\logs"
if defined CHANGAN_DATA_DIR set "CHANGAN_LOGS=%CHANGAN_DATA_DIR%\logs"
if not exist "%CHANGAN_LOGS%" mkdir "%CHANGAN_LOGS%"
start "" "%CHANGAN_LOGS%"
if errorlevel 1 echo 操作失败，请查看上面的错误说明。
pause
endlocal
