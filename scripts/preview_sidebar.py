"""使用虚构聊天和临时数据生成侧栏实机截图，不读取用户聊天。"""

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.test_desktop import DesktopTests


def capture(widget, target, extra=None):
    x, y = widget.winfo_rootx(), widget.winfo_rooty()
    box = [x, y, x + widget.winfo_width(), y + widget.winfo_height()]
    if extra:
        box = [min(box[0], extra[0]), min(box[1], extra[1]), max(box[2], extra[2]), max(box[3], extra[3])]
    subprocess.run([sys.executable, "-c", "from PIL import ImageGrab; import json,sys; ImageGrab.grab(bbox=tuple(json.loads(sys.argv[1]))).save(sys.argv[2])", json.dumps(box), str(target)], check=True)


def main():
    folder = ROOT / "outputs" / "verification" / "2026-11-04" / "sidebar"
    folder.mkdir(parents=True, exist_ok=True)
    case = DesktopTests()
    case.setUp()
    try:
        app = case.app
        app.state("normal")
        app.geometry("1160x800+50+50")
        project = app._project()
        project["name"] = "长安工作台"
        app._chat()["title"] = "完善聊天侧栏"
        app._append("user", "侧栏参考圆角菜单，保持简洁、易读。")
        app._append("assistant", "## 聊天，更井然有序\n\n置顶常用聊天，标记未读内容，在熟悉的项目之间移动。\n\n- 圆角浮层与细线图标\n- 操作分组与快捷键\n- 保存聊天原有内容")
        original = app._chat()
        app.new_chat()
        app._chat().update(title="MCP 插件接入", unread=True)
        app.new_chat()
        app._chat()["title"] = "编辑器与并行任务"
        original["pinned"] = True
        app.select_chat(original)
        app._refresh_sidebar()
        app.update()
        time.sleep(.25)
        app.state("normal")
        app.geometry("1160x800+50+50")
        app.lift()
        app.focus_force()
        app.update()
        for dark in (False, True):
            if dark:
                app.theme_switch.select()
                app.toggle_theme()
            app.update()
            row = app.chat_list.winfo_children()[0]
            anchor = row.winfo_children()[1]
            menu = app.open_chat_menu(original, anchor=anchor)
            app.update()
            time.sleep(.25)
            app.update()
            tag = "dark" if dark else "light"
            extra = [menu.winfo_rootx(), menu.winfo_rooty(), menu.winfo_rootx() + menu.winfo_width(), menu.winfo_rooty() + menu.winfo_height()]
            capture(app.sidebar, folder / f"sidebar-{tag}.png", extra)
            capture(app, folder / f"desktop-{tag}.png")
            menu.close()
            app.update()
        assert not case.errors, case.errors
    finally:
        case.tearDown()
    print(folder)


if __name__ == "__main__":
    main()
