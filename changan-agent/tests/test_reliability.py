import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from orchestrator import Agent, BudgetExceeded, ProviderRequestError, TaskStopped, ToolExecutor
from persistence import WorkspaceStore, read_json, write_json
from app_runtime import InstanceLock, redact
from message_format import CODE_FENCES


GOALS = json.dumps({"goals": [{"id": "G1", "description": "完成请求", "done_when": "回答正确"}]})
PASS = json.dumps({"passed": True, "issues": [], "feedback": ""})


class FakeProvider:
    def __init__(self, name, replies):
        self.name = name
        self.replies = list(replies)
        self.calls = []
        self.usage = {"estimated_cost": 0, "total_tokens": 0}

    def chat(self, messages, **kwargs):
        self.before_request()
        self.calls.append(messages)
        value = self.replies.pop(0)
        if isinstance(value, Exception):
            raise value
        self.usage["total_tokens"] += 1
        return value


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_plain_conversation_passes_without_tools(self):
        gpt = FakeProvider("GPT", [GOALS, PASS])
        deep = FakeProvider("DeepSeek", ['{"summary":"你好！","actions":[]}'])
        result = Agent(gpt, deep).run("你好")
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["final"], "你好！")
        self.assertNotIn("claude", result["usage"])

    def test_code_fence_preserves_indentation_and_blank_lines(self):
        text = "说明\n```python\n\n    print('hello')\n\n```\n结束"
        match = CODE_FENCES.search(text)
        self.assertEqual(match.group(3), "\n    print('hello')\n\n")
        self.assertEqual(CODE_FENCES.search("~~~json\n{}\n~~~").group(3), "{}\n")

    def test_budget_counts_shared_gpt_once(self):
        gpt = FakeProvider("GPT", [])
        deep = FakeProvider("DeepSeek", [])
        gpt.usage["estimated_cost"] = 0.6
        Agent(gpt, deep, max_budget=1)._check_budget()
        deep.usage["estimated_cost"] = 0.5
        with self.assertRaises(BudgetExceeded):
            Agent(gpt, deep, max_budget=1)._check_budget()

    def test_claude_failure_falls_back_once_per_task(self):
        gpt = FakeProvider("GPT", [PASS, PASS])
        claude = FakeProvider("Claude", [ProviderRequestError("Claude", 403, "All available accounts exhausted")])
        events = []
        agent = Agent(gpt, FakeProvider("DeepSeek", []), verifier=claude, on_event=lambda *event: events.append(event))
        self.assertEqual(agent._verify([])[1], "GPT")
        self.assertEqual(agent._verify([])[1], "GPT")
        self.assertEqual(len(claude.calls), 1)
        self.assertTrue(any(event[0] == "verifier_fallback" for event in events))

    def test_cancel_and_budget_must_not_trigger_fallback(self):
        for error in (TaskStopped("stop"), BudgetExceeded("budget")):
            gpt = FakeProvider("GPT", [])
            agent = Agent(gpt, FakeProvider("DeepSeek", []), verifier=FakeProvider("Claude", [error]))
            with self.assertRaises(type(error)):
                agent._verify([])
            self.assertEqual(gpt.calls, [])

    def test_tool_results_are_fed_back_to_executor(self):
        (self.root / "input.txt").write_text("真实内容", encoding="utf-8")
        deep = FakeProvider("DeepSeek", [
            json.dumps({"summary": "读取中", "actions": [{"tool": "read_file", "args": {"path": "input.txt"}}], "done": False}),
            '{"summary":"文件内容是真实内容","actions":[],"done":true}',
        ])
        result = Agent(FakeProvider("GPT", [GOALS, PASS]), deep, tool_executor=ToolExecutor(self.root, "full access")).run("读取文件")
        self.assertEqual(result["status"], "passed")
        self.assertIn("真实内容", deep.calls[1][1]["content"])

    def test_resume_verification_does_not_repeat_tool_or_analysis(self):
        checkpoint = self.root / "runs" / "r1" / "checkpoint.json"
        gpt = FakeProvider("GPT", [GOALS, ProviderRequestError("GPT", 503, "unavailable")])
        deep = FakeProvider("DeepSeek", ['{"summary":"完成","actions":[{"tool":"write_file","args":{"path":"hello.txt","content":"hello"}}],"done":true}'])
        executor = ToolExecutor(self.root, "full access")
        with self.assertRaises(ProviderRequestError):
            Agent(gpt, deep, checkpoint_path=checkpoint, tool_executor=executor).run("写文件")
        self.assertEqual(read_json(checkpoint)["stage"], "before_verification")
        resumed_gpt = FakeProvider("GPT", [PASS])
        with patch.object(executor, "run", side_effect=AssertionError("不得重复执行工具")):
            result = Agent(resumed_gpt, FakeProvider("DeepSeek", []), checkpoint_path=checkpoint, tool_executor=executor).run("写文件")
        self.assertEqual(result["status"], "passed")
        self.assertEqual(len(resumed_gpt.calls), 1)
        self.assertEqual(read_json(checkpoint)["stage"], "completed")
        self.assertTrue(checkpoint.with_name("result.json").exists())

    def test_bad_followup_keeps_completed_file_and_evidence(self):
        deep = FakeProvider("DeepSeek", [
            '{"summary":"文件已写入","actions":[{"tool":"write_file","args":{"path":"kept.txt","content":"keep"}}],"done":false}',
            'invalid JSON',
            '{"summary":"已确认文件","actions":[],"done":true}',
        ])
        gpt = FakeProvider("GPT", [GOALS, PASS, GOALS, PASS])
        result = Agent(gpt, deep, tool_executor=ToolExecutor(self.root, "full access")).run("写文件")
        self.assertEqual(result["status"], "passed")
        self.assertFalse(result["rounds"][0]["passed"])
        self.assertEqual((self.root / "kept.txt").read_text(), "keep")
        self.assertEqual(len(result["artifacts"]), 1)
        self.assertIn("kept.txt", deep.calls[2][1]["content"])

    def test_resume_after_successful_review_only_finalizes(self):
        checkpoint = self.root / "checkpoint.json"
        gpt = FakeProvider("GPT", [GOALS, PASS])
        agent = Agent(gpt, FakeProvider("DeepSeek", ['{"summary":"已完成","actions":[]}']), checkpoint_path=checkpoint)
        with patch.object(agent, "_complete", side_effect=OSError("模拟保存结果前退出")):
            with self.assertRaises(OSError):
                agent.run("问候")
        self.assertEqual(read_json(checkpoint)["stage"], "after_verification")
        resumed = Agent(FakeProvider("GPT", []), FakeProvider("DeepSeek", []), checkpoint_path=checkpoint).run("问候")
        self.assertEqual(resumed["status"], "passed")
        self.assertEqual(resumed["final"], "已完成")

    def test_failed_task_can_continue_after_increasing_round_limit(self):
        checkpoint = self.root / "checkpoint.json"
        failed_review = '{"passed":false,"issues":["需要补充"],"feedback":"补充回答"}'
        first = Agent(FakeProvider("GPT", [GOALS, failed_review]), FakeProvider("DeepSeek", ['{"summary":"初步回答","actions":[]}']), max_rounds=1, checkpoint_path=checkpoint).run("继续完善")
        self.assertEqual(first["status"], "failed")
        gpt = FakeProvider("GPT", [GOALS, PASS])
        deep = FakeProvider("DeepSeek", ['{"summary":"完整回答","actions":[]}'])
        result = Agent(gpt, deep, max_rounds=2, checkpoint_path=checkpoint).run("继续完善")
        self.assertEqual(result["status"], "passed")
        self.assertEqual(len(result["rounds"]), 2)
        self.assertIn("初步回答", gpt.calls[0][1]["content"])
        self.assertEqual(result["final"], "完整回答")

    def test_web_cancel_keeps_original_error_when_tree_cleanup_fails(self):
        from unittest.mock import MagicMock
        import subprocess
        ctx = MagicMock()
        parent, child = MagicMock(), MagicMock()
        ctx.Pipe.return_value = (parent, child)
        process = ctx.Process.return_value
        process.pid = 12345
        process.is_alive.side_effect = [True, True, False]
        tool = ToolExecutor(self.root, "full access", should_stop=lambda: True)
        with patch("orchestrator.multiprocessing.get_context", return_value=ctx), patch("orchestrator.os.name", "nt"), patch("orchestrator.subprocess.run", side_effect=subprocess.TimeoutExpired("taskkill", 5)):
            with self.assertRaises(TaskStopped):
                tool._external("http_get", {"url": "https://example.com"}, 30)
        process.terminate.assert_called_once()
        parent.close.assert_called_once()
        process.close.assert_called_once()

    def test_uncertain_mutating_action_requires_new_confirmation(self):
        checkpoint = self.root / "checkpoint.json"
        write_json(checkpoint, {"task": "写文件", "next_round": 1, "rounds": [], "previous": "", "feedback": "", "stage": "executing_tools", "state": {"goals": json.loads(GOALS), "execution": {"summary": "", "actions": []}, "tool_step": 0, "in_flight": 0, "plan": {"summary": "完成", "actions": [{"tool": "write_file", "args": {"path": "x.txt", "content": "x"}}], "done": True}}})
        with self.assertRaises(TaskStopped):
            Agent(FakeProvider("GPT", []), FakeProvider("DeepSeek", []), checkpoint_path=checkpoint, tool_executor=ToolExecutor(self.root, "full access")).run("写文件")
        self.assertFalse((self.root / "x.txt").exists())

    def test_ask_mode_without_callback_denies_write(self):
        with self.assertRaisesRegex(RuntimeError, "批准"):
            ToolExecutor(self.root).run("write_file", {"path": "x.txt", "content": "x"})
        self.assertFalse((self.root / "x.txt").exists())

    def test_invalid_browser_actions_are_rejected_before_approval(self):
        approval = Mock(return_value=True)
        tool = ToolExecutor(self.root, "ask for approve", approval=approval)
        with self.assertRaisesRegex(ValueError, "类型无效"):
            tool.run("browser_automate", {"url": "https://example.com", "actions": [{"type": "evaluate", "script": "1+1"}]})
        approval.assert_not_called()

    def test_file_tools_cannot_read_config_or_escape_project(self):
        tool = ToolExecutor(self.root, "full access")
        for path in ("agent_config.json", "../outside.txt", ".changan/chats/x.json"):
            with self.assertRaises(RuntimeError):
                tool.run("read_file", {"path": path})

    def test_command_can_be_cancelled(self):
        import sys
        stop = threading.Event()
        tool = ToolExecutor(self.root, "full access", should_stop=stop.is_set)
        timer = threading.Timer(0.2, stop.set)
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaises(TaskStopped):
                tool.run("run_command", {"argv": [sys.executable, "-c", "import time; time.sleep(10)"]})
        finally:
            timer.cancel()
        self.assertLess(time.monotonic() - started, 4)

    def test_command_output_is_bounded_and_truncation_is_reported(self):
        import sys
        tool = ToolExecutor(self.root, "full access")
        result = tool.run("run_command", {"argv": [sys.executable, "-c", "print('x' * 1000000)"]})
        self.assertTrue(result["ok"])
        self.assertTrue(result["stdout_truncated"])
        self.assertLessEqual(len(result["stdout"].encode()), 20000)

    def test_command_descendants_are_cleaned_when_parent_exits(self):
        import sys
        marker = self.root / "child-survived.txt"
        child = f"import time,pathlib; time.sleep(1); pathlib.Path({str(marker)!r}).write_text('alive')"
        parent = f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',{child!r}])"
        ToolExecutor(self.root, "full access").run("run_command", {"argv": [sys.executable, "-c", parent]})
        time.sleep(1.5)
        self.assertFalse(marker.exists(), "命令退出后遗留的子进程未被清理")

    def test_strict_schema_rejects_before_side_effects(self):
        with self.assertRaises(ValueError):
            Agent._plan('{"summary":"test","actions":[{"tool":"write_file","args":{}}, {"tool":"x","args":[]}]}')

    def test_chat_rename_move_keeps_messages_and_attachments(self):
        store = WorkspaceStore(self.root / "workspace.json")
        a = {"id": "p1", "chats": []}
        b = {"id": "p2", "chats": []}
        chat = {"id": "c1", "title": "旧标题", "created": "2026-10-30 10:00", "messages": [{"role": "user", "content": "消息"}]}
        a["chats"].append(chat)
        store.save([a, b])
        old = store.chat_dir(a, chat)
        (old / "attachment.txt").write_text("preserve")
        store.relocate(a, chat, title="新标题")
        renamed = store.chat_dir(a, chat)
        self.assertTrue((renamed / "attachment.txt").exists())
        self.assertFalse(old.exists())
        store.relocate(a, chat, b)
        a["chats"].remove(chat)
        b["chats"].append(chat)
        store.save([a, b])
        loaded = store.load()["projects"][1]["chats"][0]
        self.assertEqual(loaded["messages"][0]["content"], "消息")
        self.assertTrue((store.chat_dir(b, chat) / "attachment.txt").exists())

    def test_corrupt_json_recovers_previous_version(self):
        path = self.root / "state.json"
        write_json(path, {"n": 1}, backup=True)
        write_json(path, {"n": 2}, backup=True)
        path.write_text("{broken")
        self.assertEqual(read_json(path), {"n": 1})
        self.assertEqual(len(list(self.root.glob("*.corrupt-*"))), 1)

    def test_save_failure_does_not_empty_workspace_index(self):
        store = WorkspaceStore(self.root / "workspace.json")
        project = {"id": "p", "chats": [{"id": "c", "title": "t", "messages": [{"content": "first"}]}]}
        store.save([project])
        before = store.path.read_bytes()
        project["chats"][0]["messages"].append({"content": "second"})
        with patch("persistence.atomic_write", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                store.save([project])
        self.assertEqual(store.path.read_bytes(), before)
        self.assertEqual(len(project["chats"][0]["messages"]), 2)

    def test_storage_path_cannot_escape_project(self):
        store = WorkspaceStore(self.root / "workspace.json")
        with self.assertRaises(ValueError):
            store.chat_dir({"id": "p"}, {"id": "c", "storage_folder": "../../outside"})

    def test_logs_redact_keys(self):
        self.assertNotIn("sk-abc123", redact("failure sk-abc123 Authorization: Bearer secret"))
        self.assertNotIn("Bearer secret", redact("Authorization: Bearer secret"))

    def test_duplicate_instance_lock_is_released(self):
        lock = InstanceLock(self.root)
        try:
            with self.assertRaises(RuntimeError):
                InstanceLock(self.root)
        finally:
            lock.close()
        InstanceLock(self.root).close()
