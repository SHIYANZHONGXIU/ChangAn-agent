# 聊天侧栏与 LOGO 设计验收

日期：2026-11-04。

## 已完成的侧栏

- 按用户截图复现浅灰底、白色圆角浮层、细线图标、灰色快捷键、分组与子菜单；浅色与深色保持同一层级。
- 聊天与项目菜单使用同一组件。支持右键或点击省略号打开、方向键/回车、Esc、外部点击关闭与焦点恢复。
- 置顶聊天排在前面；置顶、未读状态存入现有工作区，进入聊天后清除未读，不更改消息正文。
- 项目移动、重命名、归档继续复用原有数据处理及运行中保护；删除、彻底忘记收进“更多”，确认步骤未取消。
- 复制可选择聊天名称或聊天正文。“在新窗口中打开”明确标为只读快照，不启动第二个可写实例。

源码：`desktop_app.py`、`sidebar_menu.py`。测试：`tests/test_desktop.py`。

## 验证证据

- 全量离线回归：199 项，0 失败、0 错误、0 跳过，0 次真实模型 API 请求；其中 7 项菜单/状态回归、4 项品牌资源/界面回归为本轮新增。
- 报告：`outputs/verification/2026-11-04/checks.json`、`tests.txt`。
- 实机截图：`outputs/verification/2026-11-04/sidebar/sidebar-light.png`、`sidebar-dark.png`、`desktop-light.png`、`desktop-dark.png`。
- 可复跑 `python scripts/preview_sidebar.py`：临时数据、虚构聊天、假模型；不会读取用户现有配置与聊天。测试窗口退出后已关闭，用户原有运行进程未结束。

## LOGO 已选定 A「启阙」

用户看过三方向后回复 **A**，确认记录见 `design-demos/sidebar-logo/direction-approved.md`。以下三版保留为设计过程，正式运行资源在 `assets/`，不再从初稿目录加载。

- **A 启阙**：长安城门与 A 形结构，橙色输入光标。推荐与当前黑白灰工作台搭配。
- **B 安印**：原创几何笔画组成「安」，朱红字印，更有东方气质。
- **C 折羽**：双折线同向交汇，呼应多 Agent 协作，偏现代开发者工具。

对比：`design-demos/sidebar-logo/logo-comparison.html`、`logo-comparison.png`。

方向板：同目录 `dir-1.html/png`、`dir-2.html/png`、`dir-3.html/png`；六格备选 `thumbs.html/png`；可编辑矢量 `logo-a.svg`、`logo-b.svg`、`logo-c.svg`。

三份 SVG 的 XML 结构校验通过；浏览器已实际渲染检查 1440×900 对比板、1200×900 三方向板和 375px 窄屏，无横向溢出或控制台错误。正文为 14px，图标已展示彩色、反白与侧栏小尺寸。检查记录 `visual-checks.json`。

花叔 Design 的 Python 自动体检因其 Playwright 浏览器版本缺失而未能运行，未为此安装依赖；使用已安装的 Node Playwright 检查布局、控制台与渲染，并人工查看了三版截图。所有标识为源码原生 SVG，不是调用图像生成服务的位图。

## 尚未执行与限制

- 已按用户选择统一侧栏浅深色、Tk/Qt 窗口与构建资源；构建入口直接使用同一枚九档 ICO，旧三横线图标生成代码已移除。
- 独立 EXE 已构建并通过隔离数据首次/异目录再次启动关闭检查，详见 [启阙LOGO应用与桌面版验收](启阙LOGO应用与桌面版验收.md)；桌面“长安1”源码副本未自动覆盖，原有运行进程未结束。
- “新窗口”是打开时的只读快照，不会实时同步或独立发送任务。
- 仅本机 Windows 验证；未做其他系统/所有 DPI 设置的像素级一致性验收。
- LOGO 未进行商标近似检索，公开商业发行前需另外核查。
