"""集中存储迁移与产物留存；只使用临时数据，不请求模型。"""

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app_runtime import prepare_data_layout
from artifact_store import artifact_category
from orchestrator import ToolExecutor
from persistence import WorkspaceStore, atomic_write, read_json, write_json


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / "data"

    def tearDown(self):
        self.temp.cleanup()

    def legacy(self, external=False):
        old = self.root / "external" if external else self.root / "projects" / "p"
        project = {"id": "p", "name": "项目", "folder": str(old) if external else None, "chats": []}
        for cid in ("c1", "c2"):
            folder = "20261012-" + cid + "-旧名"
            run = old / "runs" / ("run-" + cid)
            chat = {"id": cid, "title": "标题" + cid, "created": "2026-10-12 11:20:00", "storage_folder": folder, "storage_path": ".changan/chats/" + folder, "message_count": 2}
            project["chats"].append(chat)
            messages = [{"role": "user", "content": "保留原始文字与缩进\n  x"}, {"role": "assistant", "content": "结果", "meta": {"run_id": run.name, "run_path": str(run)}}]
            write_json(old / ".changan" / "chats" / folder / "chat.json", {**chat, "messages": messages})
            # 其中一个旧任务没有run.json，必须通过消息找到它的聊天。
            if cid == "c1":
                write_json(run / "run.json", {"chat_id": cid, "project_dir": str(old)})
            write_json(run / "checkpoint.json", {"task": "任务", "project_dir": str(old), "stage": "execute"})
        atomic_write(old / "code.py", "print('保留')\n")
        atomic_write(old / ".agent-backups" / "saved.bak", "旧版本")
        atomic_write(old / "artifacts" / "result.md", "历史报告")
        write_json(old / ".changan" / "chats" / "orphan" / "chat.json", {"id": "orphan", "messages": [{"content": "保留历史"}]})
        write_json(self.data / "workspace.json", {"version": 2, "projects": [project], "active_project_id": "p", "pending_tasks": [{"task": "排队任务", "chat_id": "c1"}]})
        return old, WorkspaceStore(self.data / "workspace.json", self.root)

    def test_legacy_migration_preserves_history_and_run_association(self):
        old, store = self.legacy()
        before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in old.rglob("*") if p.is_file()}
        loaded = store.load()
        project = loaded["projects"][0]
        self.assertEqual(loaded["version"], 3)
        self.assertEqual(loaded["pending_tasks"][0]["task"], "排队任务")
        self.assertEqual(store.project_root(project), self.data / "projects" / "p" / "workspace")
        for chat in project["chats"]:
            self.assertEqual(len(chat["messages"]), 2)
            self.assertEqual(chat["messages"][0]["content"], "保留原始文字与缩进\n  x")
            run = store.chat_dir(project, chat) / "runs" / ("run-" + chat["id"])
            self.assertTrue((run / "checkpoint.json").exists())
            self.assertEqual(chat["messages"][1]["meta"]["run_path"], str(run))
            self.assertEqual(read_json(run / "checkpoint.json")["project_dir"], str(store.project_root(project)))
        self.assertEqual((store.project_root(project) / "code.py").read_text(encoding="utf-8"), "print('保留')\n")
        self.assertTrue((store.project_root(project) / ".agent-backups" / "saved.bak").exists())
        self.assertTrue((store.project_data_dir(project) / "history" / "unindexed-chats" / "orphan" / "chat.json").exists())
        self.assertTrue((store.project_data_dir(project) / "history" / "artifacts" / "reports" / "result.md").exists())
        self.assertEqual(before, {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in old.rglob("*") if p.is_file()})
        self.assertEqual(store.load(), loaded)
        chat = project["chats"][0]
        primary = store.chat_dir(project, chat) / "chat.json"
        primary.write_text("{broken", encoding="utf-8")
        recovered = store.load()["projects"][0]["chats"][0]
        self.assertTrue(Path(recovered["messages"][1]["meta"]["run_path"]).exists())

    def test_external_project_keeps_original_work_files(self):
        old, store = self.legacy(external=True)
        project = store.load()["projects"][0]
        self.assertEqual(store.project_root(project), old)
        self.assertFalse((store.project_data_dir(project) / "workspace" / "code.py").exists())
        self.assertTrue(store.chat_dir(project, project["chats"][0]).is_relative_to(self.data))
        self.assertTrue((old / "code.py").exists())

    def test_unindexed_and_unassigned_runs_keep_historical_ownership(self):
        old, store = self.legacy()
        write_json(old / "runs" / "old-orphan" / "run.json", {"chat_id": "orphan", "project_dir": str(old)})
        write_json(old / "runs" / "unknown" / "checkpoint.json", {"task": "来源不明但保留", "project_dir": str(old)})
        project = store.load()["projects"][0]
        history = store.project_data_dir(project) / "history"
        self.assertEqual(read_json(history / "unindexed-chats" / "orphan" / "runs" / "old-orphan" / "run.json")["chat_id"], "orphan")
        self.assertTrue((history / "runs" / "unknown" / "checkpoint.json").exists())
        self.assertFalse((store.chat_dir(project, project["chats"][0]) / "runs" / "old-orphan").exists())

    def test_failed_index_commit_can_resume_without_changing_sources(self):
        old, store = self.legacy()
        original = store.path.read_bytes()
        real_write = atomic_write
        def interrupted(path, content, **kwargs):
            if Path(path) == store.path:
                raise OSError("模拟提交时断电")
            return real_write(path, content, **kwargs)
        with patch("persistence.atomic_write", side_effect=interrupted):
            with self.assertRaises(OSError):
                store.load()
        self.assertEqual(store.path.read_bytes(), original)
        self.assertTrue((old / "code.py").exists())
        self.assertEqual(len(store.load()["projects"][0]["chats"]), 2)

    def test_migration_conflict_never_overwrites_existing_file(self):
        old, store = self.legacy()
        conflicting = self.data / "projects" / "p" / "workspace" / "code.py"
        atomic_write(conflicting, "不能覆盖")
        with self.assertRaises(RuntimeError):
            store.load()
        self.assertEqual(conflicting.read_text(encoding="utf-8"), "不能覆盖")
        self.assertEqual(read_json(store.path)["version"], 2)
        conflicting.unlink()
        self.assertEqual(store.load()["version"], 3)

    def test_rename_move_keeps_artifacts_and_updates_all_run_paths(self):
        old, store = self.legacy()
        projects = store.load()["projects"]
        source = projects[0]
        target = {"id": "target", "name": "目标", "chats": []}
        chat = source["chats"][0]
        before = store.chat_dir(source, chat)
        artifact = before / "runs" / "run-c1" / "artifacts" / "code" / "test.py"
        atomic_write(artifact, "pass\n")
        write_json(before / "runs" / "run-c1" / "result.json", {"artifact_path": str(artifact)})
        after = store.relocate(source, chat, target, title="新标题")
        source["chats"].remove(chat)
        target["chats"].append(chat)
        store.save([source, target])
        self.assertFalse(before.exists())
        self.assertIn("新标题", after.name)
        self.assertTrue((after / artifact.relative_to(before)).exists())
        self.assertEqual(read_json(after / "runs" / "run-c1" / "result.json")["artifact_path"], str(after / artifact.relative_to(before)))
        self.assertEqual(read_json(after / "runs" / "run-c1" / "run.json")["project_id"], "target")
        self.assertTrue(Path(store.load()["projects"][1]["chats"][0]["messages"][1]["meta"]["run_path"]).is_relative_to(after))
        primary = after / "chat.json"
        primary.write_text("{broken", encoding="utf-8")
        recovered = store.load()["projects"][1]["chats"][0]
        self.assertTrue(Path(recovered["messages"][1]["meta"]["run_path"]).is_relative_to(after))

    def test_runtime_layout_moves_config_memory_logs_once(self):
        atomic_write(self.root / "agent_config.json", "{}")
        atomic_write(self.root / "memory.sqlite3", b"db")
        atomic_write(self.root / "logs" / "changan.log", "日志")
        prepare_data_layout(self.root, self.data)
        prepare_data_layout(self.root, self.data)
        self.assertFalse((self.root / "agent_config.json").exists())
        self.assertTrue((self.data / "config" / "agent_config.json").exists())
        self.assertTrue((self.data / "memory" / "memory.sqlite3").exists())
        self.assertTrue((self.data / "logs" / "changan.log").exists())
        atomic_write(self.root / "agent_config.json", '{"conflict":true}')
        with self.assertRaises(RuntimeError):
            prepare_data_layout(self.root, self.data)
        self.assertTrue((self.root / "agent_config.json").exists())

    def test_artifact_types_paths_and_changed_versions_are_preserved(self):
        work = self.root / "work"
        artifact_dir = self.data / "artifacts"
        tool = ToolExecutor(work, "full access", artifact_dir=artifact_dir)
        for path, content in (("a/file.py", "one"), ("b/file.py", "two"), ("a/file.py", "three"), ("report.md", "报告"), ("data.csv", "x\n1")):
            result = tool.run("write_file", {"path": path, "content": content})
            saved = result["saved_artifacts"][0]
            self.assertTrue(Path(saved["artifact_path"]).is_file())
            self.assertEqual(hashlib.sha256(Path(saved["artifact_path"]).read_bytes()).hexdigest(), saved["sha256"])
        self.assertEqual(len(list((artifact_dir / "code").rglob("*.py"))), 3)
        self.assertEqual(len(read_json(artifact_dir / "index.json")), 5)
        self.assertEqual(artifact_category("book.xlsx"), "documents")
        self.assertEqual(artifact_category("shot.png"), "images")
        self.assertEqual((work / "a" / "file.py").read_text(), "three")

    def test_archive_failure_keeps_real_tool_success_evidence(self):
        tool = ToolExecutor(self.root / "work", "full access", artifact_dir=self.data / "artifacts")
        with patch("orchestrator.capture_artifact", side_effect=OSError("disk full")):
            result = tool.run("write_file", {"path": "ok.txt", "content": "已实际写入"})
        self.assertTrue(result["ok"])
        self.assertIn("artifact_error", result)
        self.assertTrue((self.root / "work" / "ok.txt").exists())
        write_json(self.data / "artifacts" / "index.json", {"invalid": True})
        result = tool.run("write_file", {"path": "second.txt", "content": "索引损坏不抹去执行证据"})
        self.assertTrue(result["ok"])
        self.assertIn("artifact_error", result)

    def test_internal_data_is_hidden_but_own_workspace_is_available(self):
        atomic_write(self.data / "config" / "agent_config.json", "{}")
        atomic_write(self.root / "public.txt", "ok")
        with patch.dict(os.environ, {"CHANGAN_DATA_DIR": str(self.data)}):
            tool = ToolExecutor(self.root, "full access")
            with self.assertRaises(RuntimeError):
                tool.run("read_file", {"path": "data/config/agent_config.json"})
            self.assertEqual(tool.run("list_files", {})["files"], ["public.txt"])
            own = ToolExecutor(self.data / "projects" / "p" / "workspace", "full access")
            self.assertTrue(own.run("write_file", {"path": "result.md", "content": "正常工作"})["ok"])


if __name__ == "__main__":
    unittest.main()
