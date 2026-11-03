# 长安 · 多模型 Agent 工作台

长安是面向 Windows 本地项目的多模型桌面 Agent。默认流程为：GPT 分析目标 → DeepSeek 根据真实工具结果执行 → Claude 优先核验；未配置 Claude 或服务不可用时，由 GPT 接替核验。模型、服务地址与思考强度可在应用中配置。

本仓库提供源码、测试和合成实验协议，不包含 API 密钥、个人聊天、项目运行数据、模型权重或 Windows 安装包。项目目前仍是个人桌面项目，尚未选择开源许可证。

## 快速开始

需要 Windows 和 Python 3.10 或更新版本；模型调用需要自备兼容服务账号与 API 密钥。

1. 点击仓库上方 **Code → Download ZIP** 并解压。
2. 进入 `changan-agent` 文件夹。
3. 双击 `安装依赖.bat`，完成依赖安装。
4. 双击 `启动桌面版.bat`，在应用设置中配置模型服务地址、模型和密钥。
5. 新建项目并选择本地文件夹，再创建聊天和提交任务。

开发者也可在 `changan-agent` 目录运行：

```powershell
python -m pip install -r requirements.txt
python desktop_app.py
```

浏览器自动化首次使用需要额外安装 Playwright Chromium：

```powershell
python -m playwright install chromium
```

## 功能概览

- 项目与聊天：选择本地文件夹，支持搜索、改名、移动、归档和恢复。
- 多模型协作：分析、执行、核验；服务设置可按角色切换。
- 聊天排版：支持 Markdown 标题、粗体、列表和代码，回答字号可调；回答区域滚轮可继续滚动聊天，运行详情启动即显示状态。
- 工具：项目文件读写、命令、网页读取、浏览器操作及部分 PDF、Word、Excel 处理。
- 恢复与记录：阶段检查点、运行详情、任务队列和中断恢复。
- 长期记忆：可查看、编辑、删除与按项目隔离；自动记忆候选需要用户确认。
- 产物：文本预览、代码复制、文件备份、差异查看与恢复。
- 安全策略：`ask for approve` 与 `full access` 两种应用权限模式。

## 模型与隐私

当前使用 OpenAI 兼容的 `POST /chat/completions` 接口。Claude 也需要兼容接口；尚不支持 Anthropic 原生 Messages 协议。服务地址、模型别名、思考参数、余额与价格均由实际服务商决定。

聊天和本机任务数据保存在本地，但模型请求会向配置的服务发送相关问题、上下文和工具结果。仓库不包含真实密钥；请勿把密钥、私人聊天或运行数据提交到公开仓库。

`ask for approve` 和 `full access` 是应用层策略，不是操作系统沙箱。命令仍可能启动其他程序；建议先在临时项目和审批模式下使用。

## 验证

在 `changan-agent` 目录双击 `运行测试.bat`，或运行：

```powershell
python scripts/run_checks.py
```

截至 2026-11-03，本机 Windows、Python 3.13.3 环境的离线回归为 161 项通过，0 失败、0 错误、0 跳过。测试使用假模型、本机模拟 HTTP 服务和临时数据，不调用真实模型 API。详见[验证说明](changan-agent/docs/verification.md)。这不能证明真实服务可用，也不能保证所有任务的业务结果正确。

仓库不附带独立桌面安装包；当前源码可本地启动。独立包在本机完成过启动验收，但尚未在干净 Windows 电脑完成安装验收，也未签名或配置自动更新。

## 目录

```text
ChangAn-agent/
├─ README.md
└─ changan-agent/
   ├─ desktop_app.py       桌面界面
   ├─ orchestrator.py      多模型流程与工具调度
   ├─ long_term_memory.py  本地长期记忆
   ├─ specialized_tools.py 浏览器与办公文档工具
   ├─ tests/               离线回归测试与虚构用例
   ├─ scripts/             验证、打包与安装脚本
   └─ experiments/         可选研究代码及合成数据
```

可通过 [GitHub Issues](https://github.com/SHIYANZHONGXIU/ChangAn-agent/issues) 反馈问题。请提供脱敏错误信息，不要上传 API 密钥、个人聊天或本地数据。
