@echo off
chcp 65001 >nul
setlocal
set PYTHONUTF8=1
cd /d "%~dp0"
echo 本次将调用已配置的GPT和DeepSeek，产生API用量；只发送实验自带的虚构资料。
echo 配置价格为零表示无法估算费用，不表示服务免费。结果写入 outputs\experiments。
python experiments\memory\evaluate.py --real
pause
endlocal
