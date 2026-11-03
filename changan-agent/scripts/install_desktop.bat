@echo off
chcp 65001 >nul
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install-ChangAn.ps1"
if errorlevel 1 (
  echo 安装未完成。原始文件和用户数据均保留；请查看上方提示。
  pause
  exit /b 1
)
endlocal
