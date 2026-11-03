import tempfile
import unittest
import json
from pathlib import Path

from orchestrator import Agent, ToolExecutor, parse_json_object
from validators import validate_execution


class EngineTests(unittest.TestCase):
    def test_final_answer_only_returns_summary(self):
        self.assertEqual(Agent._final_answer('{"summary":"只显示答案","actions":[]}'), "只显示答案")
        self.assertEqual(Agent._final_answer({"summary": "完成", "actions": [{"tool": "x"}]}), "完成")

    def test_json_schema_input(self):
        value = parse_json_object("```json\n{\"ok\": true}\n```")
        self.assertTrue(value["ok"])

    def test_write_backup_and_validate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "result.json"
            target.write_text("{}", encoding="utf-8")
            executor = ToolExecutor(root, "full access")
            result = executor.run("write_file", {"path": "result.json", "content": "{\"ok\": true}"})
            self.assertTrue(result["ok"])
            self.assertTrue((root / ".agent-backups").exists())
            issues = validate_execution(root, {"actions": [{"tool": "write_file", "result": result}]})
            self.assertEqual(issues, [])
            index = json.loads((root / ".agent-backups" / "index.json").read_text(encoding="utf-8"))
            self.assertEqual(index[-1]["path"], "result.json")

    def test_dangerous_command_and_private_url_are_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            executor = ToolExecutor(Path(directory), "full access")
            with self.assertRaisesRegex(RuntimeError, "危险命令"):
                executor.run("run_command", {"command": "shutdown /s /t 0"})
            for command in ("rm -rf /tmp/test", "mkfs.ext4 /dev/sda", "Remove-Item -Recurse -Force .\\data"):
                with self.assertRaisesRegex(RuntimeError, "危险命令"):
                    executor.run("run_command", {"command": command})
            with self.assertRaisesRegex(RuntimeError, "private"):
                executor.run("http_get", {"url": "http://127.0.0.1:9"})

    def test_document_outputs_get_structural_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "broken.xlsx").write_bytes(b"not an office archive")
            issues = validate_execution(root, {"actions": [{"tool": "write_file", "result": {"ok": True, "path": "broken.xlsx"}}]})
            self.assertTrue(any("校验失败" in issue for issue in issues))

    def test_jsonl_validation_reports_bad_line(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "rows.jsonl").write_text('{"ok":true}\nnot-json\n', encoding="utf-8")
            issues = validate_execution(root, {"actions": [{"tool": "write_file", "result": {"ok": True, "path": "rows.jsonl"}}]})
            self.assertTrue(any("第 2 行" in issue for issue in issues))

    def test_reading_broken_code_does_not_require_fixing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "broken.py").write_text("def broken(\n", encoding="utf-8")
            result = ToolExecutor(root, "full access").run("read_file", {"path": "broken.py"})
            self.assertEqual(validate_execution(root, {"actions": [{"tool": "read_file", "result": result}]}), [])
            self.assertTrue(validate_execution(root, {"actions": [{"tool": "write_file", "result": result}]}))


if __name__ == "__main__":
    unittest.main()
