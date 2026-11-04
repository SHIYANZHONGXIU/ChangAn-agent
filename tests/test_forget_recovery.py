import json
import tempfile
import unittest
from pathlib import Path

from persistence import WorkspaceStore, write_json


class ForgetRecoveryTests(unittest.TestCase):
    def test_intent_hides_chat_on_next_load_and_can_be_completed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = WorkspaceStore(root / "workspace.json")
            chat = {"id": "chat1", "title": "会被忘记", "created": "2026-01-01 00:00:00", "messages": [{"role": "user", "content": "secret"}]}
            project = {"id": "project1", "name": "项目", "folder": "", "chats": [chat]}
            store.save([project])
            chat_dir = store.chat_dir(project, chat)
            (chat_dir / "runs" / "r1").mkdir(parents=True)
            (chat_dir / "runs" / "r1" / "checkpoint.json").write_text("{}", encoding="utf-8")
            store.begin_forget_chat(project, chat)
            recovered = WorkspaceStore(root / "workspace.json")
            data = recovered.load()
            self.assertEqual(data["projects"][0]["chats"], [])
            recovered.forget_chat_data(project, chat)
            recovered.clear_forget_intent(project, chat["id"])
            self.assertFalse((root / "projects" / "project1" / ".forget-intents").exists())
            self.assertFalse(chat_dir.exists())

    def test_forget_intent_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkspaceStore(Path(tmp) / "workspace.json")
            with self.assertRaises(ValueError):
                store.begin_forget_chat({"id": "p"}, {"id": "../../outside"})
