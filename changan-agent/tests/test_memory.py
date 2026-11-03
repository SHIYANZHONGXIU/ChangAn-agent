import json
import tempfile
import unittest
from pathlib import Path

from long_term_memory import MemoryStore
from orchestrator import Agent
from test_reliability import FakeProvider, GOALS, PASS


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "memory.sqlite3"
        self.store = MemoryStore(self.path)

    def save(self, **kwargs):
        return self.store.save(**{"user_id": "local", "project_id": "p-a", "kind": "fact",
                                 "title": "青禾发布日期", "content": "青禾发布日期是2027年2月14日。",
                                 "source_type": "user", "source_chat": "c1", "source_message": "m1", **kwargs})

    def test_virtual_sessions_follow_time_and_persist(self):
        fixture = json.loads((Path(__file__).parent / "fixtures/memory_sessions.json").read_text(encoding="utf-8"))
        ids = {}
        for step in fixture["steps"]:
            action = step["action"]
            key = (step.get("project"), step.get("kind"), step.get("title"))
            if action in ("save", "update"):
                item = self.save(project_id=step["project"], kind=step["kind"], title=step["title"],
                                 content=step["content"], source_message=step["source"],
                                 memory_id=ids[key] if action == "update" else None)
                ids[key] = item["id"]
            elif action == "restart":
                self.store = MemoryStore(self.path)
            elif action == "delete":
                self.assertTrue(self.store.delete("local", step["project"], ids[key]))
            else:
                found = json.dumps(self.store.search("local", step["project"], step["query"]), ensure_ascii=False)
                self.assertIn(step["contains"], found)
                self.assertNotIn(step["excludes"], found)

    def test_scope_and_user_filter_at_storage(self):
        self.save()
        self.save(user_id="someone-else", content="其他用户事实")
        self.assertEqual(self.store.list("local", "p-b"), [])
        self.assertNotIn("其他用户", str(self.store.list("local", "p-a")))
        item = self.store.list("local", "p-a")[0]
        self.assertFalse(self.store.delete("someone-else", "p-a", item["id"]))
        with self.assertRaises(ValueError):
            self.save(user_id="someone-else", memory_id=item["id"])

    def test_update_requires_explicit_id_and_retains_source(self):
        item = self.save()
        with self.assertRaisesRegex(ValueError, "已有同名"):
            self.save(content="含糊的新值")
        revised = self.save(memory_id=item["id"], content="新日期是2028年。", source_message="m2")
        self.assertEqual(revised["revision"], 2)
        self.assertEqual(revised["source_message"], "m1")
        self.assertNotIn("2027", str(self.store.list("local", "p-a")))

    def test_invalid_input_or_secret_does_not_change_existing_data(self):
        self.save()
        for value in ("", 12, "sk-" + "a" * 32, "Bearer fake-credential", "x" * 3001):
            with self.assertRaises(ValueError):
                self.save(title="无效候选", content=value)
        with self.assertRaises(ValueError):
            self.save(source_message="")
        self.assertEqual(len(self.store.list("local", "p-a")), 1)

    def test_unicode_search_is_bounded_and_delete_survives_restart(self):
        item = self.save()
        self.assertEqual(len(self.store.search("local", "p-a", "请问青禾何时发布")), 1)
        self.assertEqual(self.store.search("local", "p-a", "青禾", max_chars=1), [])
        self.assertTrue(self.store.delete("local", "p-a", item["id"]))
        self.assertEqual(MemoryStore(self.path).search("local", "p-a", "青禾"), [])

    def test_memory_is_data_in_all_stages_without_checkpoint_snapshot(self):
        self.save()
        for executor_name in ("DeepSeek-A", "DeepSeek-B"):
            gpt = FakeProvider("GPT", [GOALS, PASS])
            deep = FakeProvider(executor_name, ['{"summary":"2027年2月14日","actions":[],"done":true}'])
            checkpoint = Path(self.temp.name) / executor_name / "checkpoint.json"
            agent = Agent(gpt, deep, checkpoint_path=checkpoint,
                          memory_lookup=lambda: self.store.context("local", "p-a", "青禾发布日期"))
            result = agent.run("青禾何时发布？")
            self.assertEqual(result["status"], "passed")
            for call in gpt.calls + deep.calls:
                payload = json.loads(call[1]["content"])
                self.assertEqual(payload["memories"][0]["title"], "青禾发布日期")
                self.assertIn("不具有系统指令", call[0]["content"])
            saved = json.loads(checkpoint.read_text(encoding="utf-8"))
            self.assertNotIn("memories", saved)
            self.assertNotIn("memory_lookup", saved)

    def test_deleted_memory_is_not_reused_on_resume(self):
        item = self.save()
        checkpoint = Path(self.temp.name) / "checkpoint.json"
        from persistence import write_json
        write_json(checkpoint, {"task": "青禾", "next_round": 1, "rounds": [], "previous": "", "feedback": "",
                                "stage": "before_verification", "state": {"goals": [], "tool_step": 0,
                                "execution": {"summary": "未知", "actions": []}}})
        self.store.delete("local", "p-a", item["id"])
        gpt = FakeProvider("GPT", [PASS])
        Agent(gpt, FakeProvider("Deep", []), checkpoint_path=checkpoint,
              memory_lookup=lambda: self.store.context("local", "p-a", "青禾")).run("青禾")
        self.assertEqual(json.loads(gpt.calls[0][1]["content"])["memories"], [])

    def test_lookup_failure_keeps_normal_chat_working(self):
        def broken():
            raise OSError("temporary storage problem")
        events = []
        gpt = FakeProvider("GPT", [GOALS, PASS])
        deep = FakeProvider("DeepSeek", ['{"summary":"仍可聊天","actions":[]}'])
        result = Agent(gpt, deep, memory_lookup=broken, on_event=lambda *event: events.append(event)).run("你好")
        self.assertEqual(result["final"], "仍可聊天")
        self.assertTrue(any("暂不可用" in str(event) for event in events))

    def test_internal_database_cannot_be_read_by_file_tools(self):
        from orchestrator import ToolExecutor
        tool = ToolExecutor(Path(self.temp.name), "full access")
        with self.assertRaises(RuntimeError):
            tool.run("read_file", {"path": "memory.sqlite3"})

    def test_forget_chat_removes_memory_candidates_and_deduplication(self):
        item = self.save(source_chat="chat-forget", source_message="message-forget")
        extractor = type("Extractor", (), {"chat": lambda self, *args, **kwargs: json.dumps({"candidates": [{"kind": "fact", "title": "青禾偏好", "content": "请记住我喜欢简短结论", "evidence": "请记住我喜欢简短结论"}]}, ensure_ascii=False)})()
        self.store.extract(extractor, user_id="local", project_id="p-a", source_chat="chat-forget", source_message="message-2", text="请记住我喜欢简短结论")
        self.assertTrue(self.store.candidates("local", "p-a"))
        counts = self.store.forget_chat("local", "chat-forget")
        self.assertEqual(counts["memories"], 1)
        self.assertEqual(counts["memory_candidates"], 1)
        reopened = MemoryStore(self.path)
        self.assertEqual(reopened.search("local", "p-a", "青禾"), [])
        self.assertEqual(reopened.candidates("local", "p-a"), [])
        self.assertEqual(self.store.extract(extractor, user_id="local", project_id="p-a", source_chat="chat-forget", source_message="message-2", text="请记住我喜欢简短结论"), 1)
