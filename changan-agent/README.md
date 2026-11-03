# 长安源码目录

仓库首页的功能说明与安全边界见[上级 README](../README.md)。本目录包含桌面应用源码、离线测试以及可选的合成记忆/LoRA 实验脚本；不会自动加载训练适配器。

## 启动

Windows 需要 Python 3.10 或更新版本。双击 `安装依赖.bat` 后，再双击 `启动桌面版.bat`。启动应用后，在“设置与模型”中配置你自己的兼容 API 地址、模型与密钥。仓库中的 `.env.example` 只有占位值，不会自动加载。

也可在本目录运行：

```powershell
python -m pip install -r requirements.txt
python desktop_app.py
```

## 验证

双击 `运行测试.bat`，或执行 `python scripts/run_checks.py`。测试会使用临时数据、假模型和模拟 HTTP 服务，不会产生真实模型 API 请求。验收范围和已知边界见[验证说明](docs/verification.md)。

记忆接续评测脚本会请求真实模型并可能产生费用；LoRA 脚本会使用额外训练依赖和模型资源。运行前先检查各自协议与本机预算，不要把个人聊天用于实验。`experiments/` 内样例数据为虚构合成数据，研究脚本与桌面应用相互独立。
