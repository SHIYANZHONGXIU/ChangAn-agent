@echo off
chcp 65001 >nul
setlocal
set PYTHONUTF8=1
cd /d "%~dp0"
if not exist ".venv-lora\Scripts\python.exe" (
  echo 缺少可选训练环境。请参照 docs\自动记忆与模型实验验收.md 中的环境准备步骤。
  pause
  exit /b 1
)
echo 使用固定小模型和虚构偏好训练60步；不训练真实聊天，不修改日常聊天模型。
echo 首次运行需要联网下载模型；训练及结果保存在 outputs\experiments。
".venv-lora\Scripts\python.exe" experiments\lora\run.py
pause
endlocal
