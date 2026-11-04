@echo off
chcp 65001 >nul
setlocal
set PYTHONUTF8=1
cd /d "%~dp0"
set "CHANGAN_PYTHON=python"
if exist "%~dp0.venv\Scripts\python.exe" set "CHANGAN_PYTHON=%~dp0.venv\Scripts\python.exe"
"%CHANGAN_PYTHON%" "%~dp0scripts\run_checks.py"
if errorlevel 1 echo 操作失败，请查看上面的错误说明。
pause
endlocal
