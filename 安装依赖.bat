@echo off
chcp 65001 >nul
setlocal
set PYTHONUTF8=1
cd /d "%~dp0"
set "CHANGAN_PYTHON=python"
if not exist "%~dp0.venv-workbench\Scripts\python.exe" python -m venv "%~dp0.venv-workbench" --system-site-packages
if errorlevel 1 (
  echo 无法创建独立依赖环境，请确认已安装 Python 3.10 或更新版本。
  pause
  exit /b 1
)
set "CHANGAN_PYTHON=%~dp0.venv-workbench\Scripts\python.exe"
"%CHANGAN_PYTHON%" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 echo 操作失败，请查看上面的错误说明。
pause
endlocal
