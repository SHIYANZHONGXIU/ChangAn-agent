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

    def test_budget_settings_require_both_prices_and_currency(self):
        for prefix in ("gpt", "deepseek"):
            self.app.vars[prefix + "_key"].set("test-only-key")
        self.app.vars["max_budget"].set("2")
        with patch.object(desktop_app.messagebox, "showwarning") as warning:
            self.assertIsNone(self.app._validated_settings())
            self.assertIn("币种", warning.call_args.args[1])
            self.app.vars["cost_currency"].set("CNY")
            self.app.vars["gpt_input_cost_per_1k"].set("1")
            self.assertIsNone(self.app._validated_settings())
            self.assertIn("输入和输出", warning.call_args.args[1])
            for prefix in ("gpt", "deepseek"):
                self.app.vars[prefix + "_input_cost_per_1k"].set("1")
                self.app.vars[prefix + "_output_cost_per_1k"].set("2")
            settings = self.app._validated_settings()
            self.assertEqual(settings["cost_currency"], "CNY")
            self.assertEqual(settings["max_output_tokens"], "4096")

    def test_unknown_cost_is_visible_on_answer(self):
        self.app._append("assistant", "回答内容", meta={"status_label": "已完成", "usage": 10, "cost": None})
        self.app.update()
        def labels(widget):
            result = [str(widget.cget("text"))] if isinstance(widget, desktop_app.ctk.CTkLabel) else []
            for child in widget.winfo_children():
                result.extend(labels(child))
            return result
        self.assertTrue(any("费用未知" in value for value in labels(self.app)))
        self.assertEqual(self.errors, [])

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
        self.assertTrue((run / "artifacts" / "reports" / "answer.md").exists())
        self.app.open_artifacts(str(run))
        self.app.update()
        self.app.preview_artifact(run / "artifacts" / "reports" / "answer.md")
        self.assertIn("你好", self.app.artifact_preview.get("1.0", "end"))
        self.app._close_artifacts()
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

    def test_empty_prompt_focus_never_overlays_caret_or_ime_preedit(self):
        self.app.chat_search_entry.focus_set()
        self.app._update_prompt_hint()
        self.app.update()
        self.assertTrue(self.app.prompt_hint.winfo_ismapped())
        self.app.focus_force()
        self.app.prompt.focus_set()
        self.app.update()
        self.assertEqual(self.app.focus_get(), self.app.prompt._textbox)
        self.assertFalse(self.app.prompt_hint.winfo_ismapped(), "获得焦点后提示仍遮盖输入光标；中文预编辑尚未提交时也不能显示提示")
        self.app.prompt._textbox.event_generate("<KeyRelease>", keysym="Shift_L")
        self.app.update()
        self.assertFalse(self.app.prompt_hint.winfo_ismapped())
        self.app.chat_search_entry.focus_set()
        self.app.update()
        self.assertTrue(self.app.prompt_hint.winfo_ismapped())
        self.assertEqual(self.app.prompt.get("1.0", "end-1c"), "")

    def test_prompt_hint_tracks_actual_edits_without_key_release(self):
        self.app.chat_search_entry.focus_set()
        self.app.prompt.insert("1.0", "你好，长安")
        self.app.update()
        self.assertFalse(self.app.prompt_hint.winfo_ismapped(), "粘贴或程序写入后不应覆盖真实文字")
        self.app.prompt.delete("1.0", "end")
        self.app.update()
        self.assertTrue(self.app.prompt_hint.winfo_ismapped())
        self.app.prompt.insert("1.0", "  \n")
        self.app.update()
        self.assertFalse(self.app.prompt_hint.winfo_ismapped(), "空格和换行同样是真实输入，不能被提示覆盖")
        self.assertEqual(self.app.prompt.get("1.0", "end-1c"), "  \n")

    def test_top_menus_edit_export_and_sidebar_are_operational(self):
        labels = [self.app.menu_bar.entrycget(i, "label") for i in range(self.app.menu_bar.index("end") + 1)]
        self.assertEqual(labels, ["◧", "文件", "编辑", "查看", "帮助"])
        self.app.toggle_sidebar()
        self.app.update()
        self.assertFalse(self.app.sidebar.winfo_ismapped())
        self.app.toggle_sidebar()
        self.app.update()
        self.assertTrue(self.app.sidebar.winfo_ismapped())
        self.app._menu_toggle_theme()
        self.assertTrue(self.app.dark)
        self.app._menu_toggle_theme()
        self.assertFalse(self.app.dark)
        self.pump_until(lambda: self.app.chat_layout_job is None)
        self.app.focus_force()
        self.app.prompt.focus_set()
        self.app.update()
        self.app.prompt.insert("1.0", "可以撤销的文字")
        self.app.prompt.edit_separator()
        self.app._edit_action("<<Undo>>")
        self.app.update()
        self.assertEqual(self.app.prompt.get("1.0", "end-1c"), "")
        self.app._edit_action("<<Redo>>")
        self.app.update()
        self.assertEqual(self.app.prompt.get("1.0", "end-1c"), "可以撤销的文字")
        self.app.prompt.delete("1.0", "end")
        self.app.clipboard_clear()
        self.app.clipboard_append("普通粘贴文字")
        with patch.object(desktop_app.ImageGrab, "grabclipboard", return_value=None):
            self.app._edit_action("<<Paste>>")
        self.app.update()
        self.assertEqual(self.app.prompt.get("1.0", "end-1c"), "普通粘贴文字")
        with patch.object(desktop_app.filedialog, "askopenfilenames", return_value=()):
            self.app.prompt._textbox.event_generate("<Control-o>")
            self.app.update()
        self.assertEqual(self.app.prompt.get("1.0", "end-1c"), "普通粘贴文字", "附件快捷键不能触发Text默认的插入换行行为")
        self.app._append("user", "导出示例")
        self.app._append("assistant", "```python\n    print('格式保留')\n```")
        exported = self.directory / "export.md"
        with patch.object(desktop_app.filedialog, "asksaveasfilename", return_value=str(exported)):
            self.app.export_chat()
        self.assertIn("    print('格式保留')", exported.read_text(encoding="utf-8"))
        self.assertEqual(self.errors, [])

    def test_image_paste_file_drop_and_draft_attachments_follow_chat(self):
        from PIL import Image
        from attachment_store import attachment_path
        with patch.object(desktop_app.ImageGrab, "grabclipboard", return_value=Image.new("RGB", (32, 20), "green")):
            self.app.prompt._textbox.event_generate("<<Paste>>")
            self.app.update()
        origin = self.app._chat()
        self.assertEqual(len(origin["draft_attachments"]), 1)
        self.assertEqual(origin["draft_attachments"][0]["kind"], "image")
        source = self.directory / "中文 空格{附件}.txt"
        source.write_text("这是真实的文件内容。", encoding="utf-8")
        data = desktop_app.tk._join((str(source),))
        target = self.app.prompt._textbox
        self.assertTrue(target.dnd_bind("<<Drop>>"))
        self.assertIn("CF_HDROP", target.tk.call("bind", target._w, "<<DropTargetTypes>>"))
        callback = target.dnd_bind("<<Drop>>").split()[0]
        arguments = {"%A": "copy", "%a": "copy", "%b": "1", "%D": data, "%W": target._w, "%X": "0", "%Y": "0"}
        values = [arguments.get(token, "") for token in target._subst_format_dnd]
        self.assertEqual(target.tk.call(callback, *values), "copy")
        self.app.update()
        self.assertEqual(len(origin["draft_attachments"]), 2)
        self.assertTrue(self.app.prompt._textbox.dnd_bind("<<Drop>>"))
        self.assertEqual(self.app.prompt.get("1.0", "end-1c"), "")
        self.app.new_chat()
        self.app.update()
        self.assertFalse(self.app.attachment_row.winfo_ismapped())
        self.app.select_chat(origin)
        self.app.update()
        self.assertTrue(self.app.attachment_row.winfo_ismapped())
        before = self.app._chat_dir(self.app._project(), origin)
        with patch.object(desktop_app.simpledialog, "askstring", return_value="附件对话"):
            self.app.rename_chat(origin)
        after = self.app._chat_dir(self.app._project(), origin)
        self.assertFalse(before.exists())
        for entry in origin["draft_attachments"]:
            self.assertTrue(attachment_path(after, entry).exists())
        self.app._remove_attachment(origin["draft_attachments"][0]["id"])
        self.assertEqual(len(origin["draft_attachments"]), 1)
        self.assertEqual(self.errors, [])

    def test_attachment_only_send_uses_real_snapshot_and_persists_run_references(self):
        from persistence import read_json
        for key in ("gpt_key", "deepseek_key"):
            self.app.vars[key].set("test-only-key")
        source = self.directory / "计划.txt"
        source.write_text("发布在周五。", encoding="utf-8")
        self.app.add_attachments([source])
        captured = []
        original = DesktopProvider.chat
        def chat(provider, messages, **kwargs):
            captured.append((provider.name, messages))
            return original(provider, messages, **kwargs)
        with patch.object(DesktopProvider, "chat", chat):
            self.app.start()
            self.pump_until(lambda: not self.app.running)
        user = next(m for m in self.app._chat()["messages"] if m["role"] == "user")
        self.assertEqual(user["content"], "请分析这些附件。")
        self.assertEqual(len(user["meta"]["attachments"]), 1)
        self.assertEqual(self.app._chat()["draft_attachments"], [])
        self.assertTrue(all("发布在周五" in messages[1]["content"] for _, messages in captured))
        meta = read_json(self.app.run_context["run_dir"] / "run.json")
        self.assertEqual(len(meta["attachments"]), 1)
        self.assertTrue((self.app._chat_dir(self.app._project(), self.app._chat()) / meta["attachments"][0]["path"]).exists())
        with patch.object(DesktopProvider, "chat", chat):
            self.app._fill_prompt("继续根据刚才的附件回答")
            self.app.start()
            self.pump_until(lambda: not self.app.running)
        self.assertTrue(all("发布在周五" in messages[1]["content"] for _, messages in captured))
        loaded = self.app.store.load()["projects"][0]["chats"][0]
        self.assertEqual(loaded["messages"][0]["meta"]["attachments"][0]["name"], "计划.txt")
        self.assertEqual(self.errors, [])

    def test_queued_attachments_stay_with_their_task_and_new_draft_is_preserved(self):
        for key in ("gpt_key", "deepseek_key"):
            self.app.vars[key].set("test-only-key")
        entered, release = threading.Event(), threading.Event()
        original = DesktopProvider.chat
        executions = []
        def chat(provider, messages, **kwargs):
            if provider.name == "DeepSeek":
                evidence = json.loads(messages[1]["content"])
                executions.append(evidence)
                if len(executions) == 1:
                    entered.set()
                    if not release.wait(8):
                        raise RuntimeError("测试未解除等待")
            return original(provider, messages, **kwargs)
        first = self.directory / "first.txt"
        second = self.directory / "second.txt"
        third = self.directory / "unsent.txt"
        for path, value in ((first, "第一项独有附件"), (second, "第二项独有附件"), (third, "尚未发送附件")):
            path.write_text(value, encoding="utf-8")
        with patch.object(DesktopProvider, "chat", chat):
            try:
                self.app.add_attachments([first])
                self.app._fill_prompt("第一项任务")
                self.app.start()
                self.pump_until(entered.is_set)
                self.app.add_attachments([second])
                self.app._fill_prompt("第二项任务")
                self.app.start()
                self.assertEqual(self.app.pending_tasks[0]["attachments"][0]["name"], "second.txt")
                self.app.add_attachments([third])
                release.set()
                self.pump_until(lambda: len(executions) == 2 and not self.app.running)
            finally:
                release.set()
        self.assertIn("第一项独有附件", json.dumps(executions[0], ensure_ascii=False))
        self.assertNotIn("第二项独有附件", json.dumps(executions[0], ensure_ascii=False))
        self.assertIn("第二项独有附件", json.dumps(executions[1], ensure_ascii=False))
        self.assertNotIn("尚未发送附件", json.dumps(executions[1], ensure_ascii=False))
        self.assertEqual(self.app._chat()["draft_attachments"][0]["name"], "unsent.txt")
        self.assertEqual(self.errors, [])

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

    def test_memory_editor_new_chat_update_delete_and_scope(self):
        origin_project = self.app._project()
        origin_chat = self.app._chat()
        message = self.app._append("user", "青禾发布日期是2027年2月14日。")
        self.app.open_memory({"chat_id": origin_chat["id"], "message_id": message["id"], "content": message["content"]})
        panel = self.app.memory_panel
        panel.title_var.set("青禾发布日期")
        panel.save_record()
        self.app.update()
        record = self.app.memory_store.search("local", origin_project["id"], "青禾")[0]
        self.assertEqual(record["source_message"], message["id"])
        self.app.new_chat()
        self.assertEqual(len(self.app.memory_store.context("local", origin_project["id"], "青禾")), 1)
        other = self.app._project_data("其他项目")
        self.app.projects.append(other)
        self.app.move_chat(origin_chat, other)
        self.assertEqual(self.app.memory_store.search("local", other["id"], "青禾"), [])
        panel.content.delete("1.0", "end")
        panel.content.insert("1.0", "发布日期改为2028年3月。")
        panel.save_record()
        self.assertEqual(self.app.memory_store.list("local", origin_project["id"])[0]["revision"], 2)
        with patch("memory_panel.messagebox.askyesno", return_value=True):
            panel.delete_record()
        self.assertEqual(self.app.memory_store.list("local", origin_project["id"]), [])
        self.assertEqual(self.errors, [])

    def test_memory_flows_to_models_and_can_be_disabled(self):
        self.app.memory_store.save(user_id="local", project_id=self.app._project()["id"], kind="fact", title="青禾日期", content="2027年2月14日")
        for key in ("gpt_key", "deepseek_key"):
            self.app.vars[key].set("test-only-key")
        captured = []
        original = DesktopProvider.chat
        def chat(provider, messages, **kwargs):
            captured.append(json.loads(messages[1]["content"]).get("memories"))
            return original(provider, messages, **kwargs)
        with patch.object(DesktopProvider, "chat", chat):
            self.app._fill_prompt("青禾日期是什么？")
            self.app.start()
            self.pump_until(lambda: not self.app.running)
            self.assertEqual(len(captured), 3)
            self.assertTrue(all(records and records[0]["content"] == "2027年2月14日" for records in captured))
            self.app.vars["memory_enabled"].set("0")
            captured.clear()
            self.app._fill_prompt("青禾日期是什么？")
            self.app.start()
            self.pump_until(lambda: not self.app.running)
            self.assertEqual(captured, [[], [], []])

    def test_automatic_candidate_confirmation_and_disable(self):
        for key in ("gpt_key", "deepseek_key"):
            self.app.vars[key].set("test-only-key")
        self.app.vars["memory_auto_extract"].set("1")
        project, chat = self.app._project(), self.app._chat()
        original = DesktopProvider.chat
        extraction_calls = []
        def respond(provider, messages, **kwargs):
            if "current_user_message" in messages[0]["content"]:
                source = json.loads(messages[1]["content"])["current_user_message"]
                extraction_calls.append(source)
                return json.dumps({"candidates": [{"kind": "preference", "title": "回答风格", "content": source, "evidence": source}]}, ensure_ascii=False)
            return original(provider, messages, **kwargs)
        with patch.object(DesktopProvider, "chat", respond):
            self.app._fill_prompt("我长期偏好简短结论。")
            self.app.start()
            self.app.new_chat()
            self.pump_until(lambda: not self.app.running)
            candidates = self.app.memory_store.candidates("local", project["id"])
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["source_chat"], chat["id"])
            self.assertEqual(self.app.memory_store.list("local", project["id"]), [])
            self.app.open_memory()
            panel = self.app.memory_panel
            panel.view_var.set("待确认")
            panel.select_candidate(candidates[0])
            panel.save_record()
            record = self.app.memory_store.list("local", project["id"])[0]
            self.assertEqual(record["content"], "我长期偏好简短结论。")
            self.assertEqual(record["source_message"], next(m["id"] for m in chat["messages"] if m["role"] == "user"))
            self.app.vars["memory_auto_extract"].set("0")
            self.app._fill_prompt("这次不要提取。")
            self.app.start()
            self.pump_until(lambda: not self.app.running)
        self.assertEqual(extraction_calls, ["我长期偏好简短结论。"])
        self.assertEqual(self.errors, [])

    def test_moving_chat_dismisses_unconfirmed_candidates(self):
        from test_memory_candidates import Extractor, SOURCE
        project, chat = self.app._project(), self.app._chat()
        self.app.memory_store.extract(Extractor(), user_id="local", project_id=project["id"], source_chat=chat["id"], source_message="virtual", text=SOURCE)
        target = self.app._project_data("目标项目")
        self.app.projects.append(target)
        self.app.move_chat(chat, target)
        self.assertEqual(self.app.memory_store.candidates("local", project["id"]), [])
        self.assertEqual(self.app.memory_store.candidates("local", target["id"]), [])

    def test_forget_source_chat_clears_messages_memory_and_files(self):
        project, chat = self.app._project(), self.app._chat()
        message = self.app._append("user", "我喜欢简短回答。")
        self.app.memory_store.save(user_id="local", project_id=project["id"], kind="preference", title="回答风格", content="我喜欢简短回答。", source_type="user", source_chat=chat["id"], source_message=message["id"])
        chat_dir = self.app._chat_dir(project, chat)
        run = chat_dir / "runs" / "r-forget"
        run.mkdir(parents=True)
        (run / "checkpoint.json").write_text("{}", encoding="utf-8")
        self.app.forget_chat(chat["id"])
        self.assertEqual(self.app.memory_store.search("local", project["id"], "回答风格"), [])
        self.assertEqual(chat["messages"], [])
        self.assertFalse(chat_dir.exists())
        self.assertNotEqual(self.app._project()["chats"][0]["id"], chat["id"])
        self.assertEqual(self.errors, [])

    def test_settings_and_scroll_do_not_obstruct_chat(self):
        self.assertEqual(self.app.settings_window.state(), "withdrawn")
        self.app.focus_settings()
        self.app.update()
        self.assertNotEqual(self.app.settings_window.state(), "withdrawn")
        self.app._save_settings()
        for _ in range(5):
            self.app._append("assistant", "用于检查长消息滚动。\n" * 18)
        self.app.update()
        self.app._scroll_chat("moveto", 0)
        self.app.update()
        self.assertFalse(self.app.follow_latest)
        self.assertTrue(self.app.bottom_btn.winfo_ismapped())
        self.app._scroll_to_bottom()
        self.app.update()
        self.assertTrue(self.app.follow_latest)
        self.assertFalse(self.app.bottom_btn.winfo_ismapped())
        self.assertEqual(self.errors, [])

    def test_short_user_message_is_right_aligned_and_assistant_is_left(self):
        first = self.app._append("user", "你好")
        second = self.app._append("assistant", "你好，我是长安。")
        self.app.update()
        user = self.app.message_labels[id(first)].master
        assistant = self.app.message_labels[id(second)].master
        self.assertEqual(user.pack_info()["anchor"], "e")
        self.assertEqual(assistant.pack_info()["anchor"], "w")
        self.assertGreater(user.cget("corner_radius"), 0)
        self.assertEqual(assistant.cget("border_width"), 0)

    def test_turn_divider_discloses_only_its_run_and_survives_relayout(self):
        self.app._append("user", "整理第一轮内容")
        first = self.app._append("assistant", "第一轮回答", meta={"run_id": "first-run", "duration_seconds": 1791, "status_label": "已完成"})
        self.app._append("trace", "第一轮工具记录", meta={"run_id": "first-run"})
        self.app._append("user", "再继续第二轮")
        second = self.app._append("assistant", "第二轮回答", meta={"run_id": "second-run", "duration_seconds": 5, "status_label": "已停止"})
        self.app._append("trace", "第二轮核验记录", meta={"run_id": "second-run"})
        self.app.update()
        first_key = (self.app.active_chat_id, first["id"])
        second_key = (self.app.active_chat_id, second["id"])
        section = self.app.run_sections[first_key]
        self.assertEqual(section["button"].cget("text"), "用时 29 分钟 51 秒  ›")
        self.assertFalse(section["panel"].winfo_ismapped())
        self.assertLess(self.app.message_labels[id(first)].master.winfo_height(), 250, "用时移到折叠条后，空的统计区域不应留下大片留白")
        self.assertTrue(section["line"].winfo_ismapped())
        section["button"].invoke()
        self.app.update()
        self.assertTrue(section["panel"].winfo_ismapped())
        self.assertIn("第一轮工具记录", section["box"].get("1.0", "end"))
        self.assertNotIn("第二轮核验记录", section["box"].get("1.0", "end"))
        self.assertFalse(self.app.run_sections[second_key]["panel"].winfo_ismapped())
        self.app._append("trace", "本轮后续输出", meta={"run_id": "first-run"})
        self.pump_until(lambda: "本轮后续输出" in section["box"].get("1.0", "end"))
        self.app._show_chat(scroll=False)
        self.app.update()
        self.assertTrue(self.app.run_sections[first_key]["panel"].winfo_ismapped())
        self.assertIn("已停止", self.app.run_sections[second_key]["button"].cget("text"))
        self.app.run_sections[first_key]["button"].invoke()
        self.app.update()
        self.assertFalse(self.app.run_sections[first_key]["panel"].winfo_ismapped())
        self.assertEqual(self.app._duration_text(3661), "1 小时 1 分钟 1 秒")
        self.assertEqual(self.app._duration_text(0.1), "0 秒")
        self.assertEqual(self.app._duration_text(float("nan")), "")
        self.assertEqual(self.app._duration_text("bad"), "")
        self.assertEqual(self.errors, [])

    def test_running_turn_timer_refreshes_inline_and_does_not_leak_across_chats(self):
        self.app._create_live_message()
        message = self.app.live_message
        message["meta"]["run_id"] = "live-test"
        self.app.run_context = {"id": "live-test", "started": time.perf_counter() - 65, "elapsed_before": 0}
        self.app.current_run_id = "live-test"
        self.app.live_stream = "本轮实时输出"
        self.app._refresh_run_sections()
        key = (self.app.active_chat_id, message["id"])
        self.assertIn("正在工作 1 分钟", self.app.run_sections[key]["button"].cget("text"))
        self.app.run_sections[key]["button"].invoke()
        self.app.update()
        self.assertIn("本轮实时输出", self.app.run_sections[key]["box"].get("1.0", "end"))
        origin = self.app._chat()
        self.app.new_chat()
        self.assertEqual(self.app.run_sections, {})
        self.app.select_chat(origin)
        self.app.live_message = None
        self.app.live_chat_id = None
        self.app.run_context = None
        run = self.app._chat_dir(self.app._project(), origin) / "runs" / "from-journal"
        run.mkdir(parents=True)
        (run / "events.jsonl").write_text('{"type":"stage","value":"磁盘运行记录"}', encoding="utf-8")
        self.app._append("assistant", "恢复的回答", meta={"run_id": "from-journal", "run_path": str(run), "duration_seconds": 2})
        self.assertIn("磁盘运行记录", self.app._trace_content("from-journal"))
        self.assertNotIn("磁盘运行记录", self.app._trace_content("live-test"))
        self.assertEqual(self.errors, [])
