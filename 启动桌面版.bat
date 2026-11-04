@echo off
chcp 65001 >nul
setlocal
set PYTHONUTF8=1
cd /d "%~dp0"
set "CHANGAN_PYTHON=python"
if exist "%~dp0.venv\Scripts\python.exe" set "CHANGAN_PYTHON=%~dp0.venv\Scripts\python.exe"
if exist "%~dp0.venv-workbench\Scripts\python.exe" set "CHANGAN_PYTHON=%~dp0.venv-workbench\Scripts\python.exe"
"%CHANGAN_PYTHON%" -c "import sys; assert sys.version_info >= (3, 10)" >nul 2>&1
if errorlevel 1 (
  echo 需要 Python 3.10 或更新版本。请安装 Python，并勾选添加到 PATH。
  pause
  exit /b 1
)
"%CHANGAN_PYTHON%" -c "import customtkinter, tkinterdnd2; from PIL import Image, ImageGrab" >nul 2>&1
if errorlevel 1 (
  echo 缺少桌面依赖，请先双击“安装依赖.bat”。
  pause
  exit /b 1
)
"%CHANGAN_PYTHON%" "%~dp0desktop_app.py"
if errorlevel 1 (
  echo 启动失败，请查看 data\logs\changan.log。
  pause
  exit /b 1
)
endlocal
