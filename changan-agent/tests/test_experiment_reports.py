import json
import tempfile
import unittest
from pathlib import Path

from experiments.summarize import summarize_lora, summarize_memory
from persistence import write_json, read_json


class ExperimentReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def test_recovery_and_rescore_preserve_original_errors_and_bytes(self):
        recovery = self.folder / "recovery"
        recovery.mkdir()
        usage = {"gpt": {"attempts": 1, "total_tokens": 10}}
        protocol = {"conditions": ["external"], "snapshots": [{"id": "s", "questions": [{"id": "code", "type": "fact", "contains": ["QH-731"]}]}]}
        row = {"snapshot": "s", "condition": "external", "provider": "gpt", "error": "timeout"}
        write_json(self.folder / "protocol.json", protocol)
        write_json(self.folder / "report.json", {"models": {"gpt": {}}, "runs": [row], "usage": usage})
        write_json(recovery / "report.json", {"runs": [{"snapshot": "s", "condition": "external", "provider": "gpt", "raw_answer": '{"answers":{"code":"QH-731"}}'}], "usage": usage})
        original = (self.folder / "report.json").read_bytes()
        result = summarize_memory(self.folder, recovery)
        self.assertEqual(result["aggregate"][0]["passed"], 1)
        self.assertEqual(result["original_errors_retained"], 1)
        self.assertEqual(result["requests_including_errors"], 2)
        self.assertEqual((self.folder / "report.json").read_bytes(), original)

    def test_lora_gate_rejects_general_regression_and_extra_numbered_item(self):
        style = "结论：开始。\n1. 一\n2. 二\n3. 三\n4. 四\n长安已记"
        evaluations = [{"condition": c, "style_pass_rate": 1.0, "general_accuracy": 1.0 if c == "baseline" else .25,
                        "rows": [{"split": "test", "answer": style, "passed": True}]} for c in ("baseline", "prompt", "external", "lora", "hybrid")]
        write_json(self.folder / "report.json", {"evaluations": evaluations, "protocol": {"steps": 60, "screening_gate": {"style_over_best_non_lora": .05, "general_accuracy_drop_max": .1}},
                    "reload_exact_match": True, "rollback_exact_match": True, "training_seconds": 1, "peak_gpu_bytes": 1000})
        result = summarize_lora(self.folder)
        self.assertFalse(result["continue_lora"])
        self.assertTrue(all(e["style_pass_rate"] == 0 for e in result["evaluations"]))
        self.assertTrue(read_json(self.folder / "report.json")["evaluations"][0]["rows"][0]["passed"])
