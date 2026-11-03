"""附件快照、读取与模型传递；所有资料和模型均为测试样例。"""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from attachment_store import attachment_path, prepare_attachments, save_attachment
from orchestrator import Agent


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.chat = self.root / "chat"

    def test_snapshot_survives_source_changes_and_chat_move(self):
        source = self.root / "中文 文件.txt"
        source.write_text("项目交付时间：周五。", encoding="utf-8")
        entry = save_attachment(self.chat, source)
        source.write_text("后来改动", encoding="utf-8")
        self.assertIn("周五", prepare_attachments(self.chat, [entry])["files"][0]["text"])
        moved = self.root / "new-project" / "renamed-chat"
        moved.parent.mkdir()
        shutil.move(str(self.chat), str(moved))
        self.assertTrue(attachment_path(moved, entry).is_file())
        self.assertIn("周五", prepare_attachments(moved, [entry])["files"][0]["text"])

    def test_clipboard_image_becomes_real_vision_content(self):
        entry = save_attachment(self.chat, image=Image.new("RGB", (64, 40), "red"))
        data = prepare_attachments(self.chat, [entry])
        self.assertEqual(entry["kind"], "image")
        self.assertTrue(data["images"][0]["image_url"]["url"].startswith("data:image/jpeg;base64,/9j/"))
        self.assertEqual(data["files"][0]["name"], entry["name"])

    def test_tamper_path_escape_and_oversize_are_rejected(self):
        entry = save_attachment(self.chat, image=Image.new("RGB", (10, 10)))
        with self.assertRaises(ValueError):
            attachment_path(self.chat, {"path": "../../outside.txt"})
        with self.assertRaises(ValueError):
            attachment_path(self.chat, {"path": str(self.root / "outside.txt")})
        attachment_path(self.chat, entry).write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "已变更"):
            prepare_attachments(self.chat, [entry])
        source = self.root / "big.txt"
        source.write_text("test")
        with patch("attachment_store.MAX_BYTES", 1):
            with self.assertRaises(ValueError):
                save_attachment(self.chat, source)
        with self.assertRaises(ValueError):
            save_attachment(self.chat, self.root)

    def test_unknown_formats_are_explicit_and_never_executed(self):
        source = self.root / "unsafe.exe"
        source.write_bytes(b"MZsample")
        entry = save_attachment(self.chat, source)
        data = prepare_attachments(self.chat, [entry])
        self.assertIn("暂不支持提取", data["files"][0]["text"])
        self.assertEqual(data["images"], [])

    def test_document_and_excel_attachments_are_extracted(self):
        from docx import Document
        from openpyxl import Workbook
        word = self.root / "说明.docx"
        document = Document()
        document.add_paragraph("交付物是长安桌面版。")
        document.save(word)
        excel = self.root / "数据.xlsx"
        workbook = Workbook()
        workbook.active.append(["项目", "数量"])
        workbook.active.append(["长安", 3])
        workbook.save(excel)
        workbook.close()
        data = prepare_attachments(self.chat, [save_attachment(self.chat, word), save_attachment(self.chat, excel)])
        self.assertIn("长安桌面版", data["files"][0]["text"])
        self.assertIn("长安", data["files"][1]["text"])

    def test_gpt_receives_image_and_all_stages_receive_text_without_persisting_base64(self):
        source = self.root / "plan.md"
        source.write_text("青禾交付日期为周五。", encoding="utf-8")
        entries = [save_attachment(self.chat, source), save_attachment(self.chat, image=Image.new("RGB", (20, 20), "blue"))]
        data = prepare_attachments(self.chat, entries)
        calls = {"gpt": [], "deepseek": [], "claude": []}
        class Provider:
            def __init__(self, name):
                self.name = name
                self.usage = {}
            def chat(self, messages, **kwargs):
                calls[self.name].append(messages)
                if self.name == "gpt":
                    return '{"goals":[{"id":"G1","description":"结合蓝色图片与周五交付日期作答","done_when":"明确回答"}]}'
                if self.name == "deepseek":
                    return '{"summary":"已参考附件回答","actions":[],"done":true}'
                return '{"passed":true,"issues":[],"feedback":""}'
        checkpoint = self.root / "checkpoint.json"
        result = Agent(Provider("gpt"), Provider("deepseek"), verifier=Provider("claude"), checkpoint_path=checkpoint, attachments=data).run("分析这些附件")
        self.assertEqual(result["status"], "passed")
        gpt_content = calls["gpt"][0][1]["content"]
        self.assertIsInstance(gpt_content, list)
        self.assertEqual(gpt_content[1]["type"], "image_url")
        for name in ("deepseek", "claude"):
            self.assertIn("青禾交付日期为周五", calls[name][0][1]["content"])
            self.assertNotIn("data:image", calls[name][0][1]["content"])
        self.assertNotIn("data:image", checkpoint.read_text(encoding="utf-8"))
