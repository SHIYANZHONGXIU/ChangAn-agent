"""实际 Tk 窗口回归；独立临时工作区，不触碰用户配置和聊天。"""

import json
import gc
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import desktop_app
from orchestrator import ProviderRequestError


class DesktopProvider:
    def __init__(self, name, *args, **kwargs):
        self.name = name
        self.usage = {"total_tokens": 10, "estimated_cost": 0}
        self.calls = 0
        self.before_request = lambda: None

    def configure_budget(self, *args):
        pass

    def chat(self, messages, **kwargs):
        self.before_request()
        self.calls += 1
        if self.name == "Claude":
            raise ProviderRequestError("Claude", 403, "All available accounts exhausted")
        if self.name == "DeepSeek":
            return json.dumps({"summary": "你好，长安已完成回复。", "actions": [], "done": True}, ensure_ascii=False)
        if "goals" in messages[0]["content"]:
            return json.dumps({"goals": [{"id": "G1", "description": "回答问候", "done_when": "正常回复"}]})
        return '{"passed":true,"issues":[],"feedback":""}'


class DesktopTests(unittest.TestCase):
    def setUp(self):
        # 连续创建测试窗口时，在主线程回收旧 Tk 字体，避免后台 GC 等待已销毁的窗口。
        gc.collect()
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.patches = [
            patch.object(desktop_app, "WORKSPACE", self.directory / "workspace.json"),
            patch.object(desktop_app, "ROOT", self.directory / "agent_config.json"),
            patch.object(desktop_app, "DATA_DIR", self.directory),
            patch.dict(os.environ, {"CHANGAN_OFFLINE": "1"}),
            patch.object(desktop_app, "Provider", DesktopProvider),
        ]
        for item in self.patches:
            item.start()
        self.app = desktop_app.App()
        self.errors = []
        self.app.report_callback_exception = lambda *error: self.errors.append(error)
        self.app.update()
        gc.collect()

    def tearDown(self):
        self.app._on_close()
        until = time.monotonic() + 6
        while time.monotonic() < until:
            try:
                self.app.update()
                if not self.app.winfo_exists():
                    break
            except Exception:
                break
            time.sleep(0.02)
        for item in reversed(self.patches):
            item.stop()
        self.app = None
        gc.collect()
        self.temporary.cleanup()

    def pump_until(self, condition):
        deadline = time.monotonic() + 8
        while not condition() and time.monotonic() < deadline:
            self.app.update()
            time.sleep(0.02)
        self.app.update()
        self.assertTrue(condition(), "界面任务超时")

    def test_send_render_code_fallback_and_persist(self):
        for key in ("gpt_key", "deepseek_key", "claude_key"):
            self.app.vars[key].set("test-only-key")
        self.app.prompt.insert("1.0", "你好")
        self.app.start()
        self.assertTrue(self.app.running)
        self.app.new_chat()
        other_chat = self.app._chat()
        self.pump_until(lambda: not self.app.running)
        self.assertEqual(self.app.status.cget("text"), "已完成")
        self.assertEqual(other_chat["messages"], [])
        origin = self.app._project()["chats"][1]
        self.assertTrue(any(m["role"] == "assistant" and m["meta"]["status_label"] == "已完成" for m in origin["messages"]))
        self.assertTrue(any("自动降级" in m["content"] for m in origin["messages"] if m["role"] == "trace"))
        self.app.select_chat(origin)
        self.app._append("assistant", "示例代码：\n```python\nprint('长安')\n```\n可以直接复制。")
        self.app.update()
        self.assertEqual(self.errors, [])
        loaded = self.app.store.load()["projects"][0]["chats"][1]
        self.assertIn("```python", loaded["messages"][-1]["content"])
        run = self.app.run_context["run_dir"]
        self.assertEqual(run.name, self.app.current_run_id)
        self.assertTrue((run / "result.json").exists())
        self.assertTrue((run / "events.jsonl").exists())
        capture = os.environ.get("CHANGAN_CAPTURE_DIR")
        if capture:
            import subprocess
            import sys
            self.app.lift()
            self.app.update()
            time.sleep(0.3)
            folder = Path(capture)
            folder.mkdir(parents=True, exist_ok=True)
            box = (self.app.winfo_rootx(), self.app.winfo_rooty(), self.app.winfo_rootx() + self.app.winfo_width(), self.app.winfo_rooty() + self.app.winfo_height())
            capture_python = os.environ.get("CHANGAN_CAPTURE_PYTHON", sys.executable)
            script = "from PIL import ImageGrab; import json,sys; ImageGrab.grab(bbox=tuple(json.loads(sys.argv[1]))).save(sys.argv[2])"
            subprocess.run([capture_python, "-c", script, json.dumps(box), str(folder / "desktop-light.png")], check=True)
            self.app.theme_switch.select()
            self.app.toggle_theme()
            self.app.update()
            time.sleep(0.3)
            subprocess.run([capture_python, "-c", script, json.dumps(box), str(folder / "desktop-dark.png")], check=True)

    def test_rename_preserves_messages_and_timer_has_separate_column(self):
        chat = self.app._chat()
        self.app._append("user", "不能消失的消息")
        old = self.app._chat_dir(self.app._project(), chat)
        with patch.object(desktop_app.simpledialog, "askstring", return_value="重命名聊天"):
            self.app.rename_chat(chat)
        self.assertFalse(old.exists())
        self.assertTrue((self.app._chat_dir(self.app._project(), chat) / "chat.json").exists())
        self.assertEqual(chat["messages"][0]["content"], "不能消失的消息")
        self.assertNotEqual(self.app.runtime_label.grid_info()["column"], self.app.trace_btn.grid_info()["column"])
        self.assertEqual(self.errors, [])

    def test_drafts_and_copy_preserve_content(self):
        first = self.app._chat()
        self.app.prompt.insert("1.0", "未发送的草稿")
        self.app.new_chat()
        self.assertEqual(self.app.prompt.get("1.0", "end-1c"), "")
        self.app.select_chat(first)
        self.assertEqual(self.app.prompt.get("1.0", "end-1c"), "未发送的草稿")
        code = "\n    print('长安')\n"
        self.app._copy_code(code)
        self.assertEqual(self.app.clipboard_get(), code)

    def test_queue_does_not_duplicate_requests_or_include_future_context(self):
        for key in ("gpt_key", "deepseek_key"):
            self.app.vars[key].set("test-only-key")
        entered, release = threading.Event(), threading.Event()
        executions = []
        original_chat = DesktopProvider.chat

        def chat(provider, messages, **kwargs):
            if provider.name == "DeepSeek":
                executions.append(json.loads(messages[1]["content"]))
                if len(executions) == 1:
                    entered.set()
                    if not release.wait(6):
                        raise RuntimeError("测试未解除等待")
            return original_chat(provider, messages, **kwargs)

        with patch.object(DesktopProvider, "chat", chat):
            try:
                self.app.prompt.insert("1.0", "第一项任务")
                self.app.start()
                self.pump_until(entered.is_set)
                for task in ("第二项任务", "第三项任务"):
                    self.app.prompt.insert("1.0", task)
                    self.app.start()
                self.assertEqual(len(self.app.pending_tasks), 2)
                release.set()
                self.pump_until(lambda: len(executions) == 3 and not self.app.running)
            finally:
                release.set()
        self.assertEqual([x["task"] for x in executions], ["第一项任务", "第二项任务", "第三项任务"])
        self.assertNotIn("第三项任务", json.dumps(executions[1]["history"], ensure_ascii=False))
        users = [m["content"] for m in self.app._chat()["messages"] if m["role"] == "user"]
        self.assertEqual(users, ["第一项任务", "第二项任务", "第三项任务"])
        self.assertEqual(self.errors, [])

    def test_saved_result_is_recovered_without_a_model_request(self):
        from persistence import write_json
        run = self.app._chat_dir(self.app._project(), self.app._chat()) / "runs" / "saved-run"
        write_json(run / "result.json", {"status": "passed", "final": "这是保存的完整答案"})
        self.app._append("assistant", "处理中…", meta={"run_path": str(run), "status_label": "运行中"})
        with patch.object(DesktopProvider, "chat", side_effect=AssertionError("不应请求模型")):
            self.app._load_workspace()
        message = self.app._chat()["messages"][-1]
        self.assertEqual(message["content"], "这是保存的完整答案")
        self.assertEqual(message["meta"]["status_label"], "已完成")

    def test_failed_checkpoint_is_available_in_resume_picker(self):
        from persistence import write_json
        chat = self.app._chat()
        run = self.app._chat_dir(self.app._project(), chat) / "runs" / "failed-run"
        write_json(run / "checkpoint.json", {"task": "继续完善", "stage": "completed", "next_round": 2, "result": {"status": "failed"}})
        write_json(run / "run.json", {"status": "failed", "chat_id": chat["id"]})
        self.app.vars["max_rounds"].set("3")
        with patch.object(desktop_app.simpledialog, "askinteger", return_value=1) as picker, patch.object(self.app, "_launch_task") as launch:
            self.app.resume_checkpoint()
        picker.assert_called_once()
        self.assertEqual(launch.call_args.args[0], "继续完善")
        self.assertTrue(launch.call_args.kwargs["resumed"])

    def test_original_file_has_no_unrelated_backup(self):
        root = self.app._project_root()
        from orchestrator import ToolExecutor
        executor = ToolExecutor(root, "full access")
        executor.run("write_file", {"path": "a/file.txt", "content": "one"})
        executor.run("write_file", {"path": "a/file.txt", "content": "two"})
        other = root / "b" / "file.txt"
        other.parent.mkdir(parents=True)
        other.write_text("different")
        self.assertIsNone(self.app._latest_backup(other))
        self.assertIsNotNone(self.app._latest_backup(root / "a" / "file.txt"))
