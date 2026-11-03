import copy
import json
import tempfile
import unittest
from pathlib import Path

from experiments.lora.run import messages_for, resolve_run_config, style_score, validate_data
from experiments.lora.aggregate import aggregate, metric_value


class LoraProtocolTests(unittest.TestCase):
    def test_synthetic_data_splits_do_not_overlap(self):
        path = Path(__file__).parents[1] / "experiments/lora/data.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        validate_data(data)
        duplicate = copy.deepcopy(data)
        duplicate["test"][0]["prompt"] = duplicate["train"][0]["prompt"]
        with self.assertRaises(ValueError):
            validate_data(duplicate)

    def test_style_scoring_and_condition_inputs(self):
        self.assertTrue(style_score("结论：开始。\n1. 第一项\n2. 第二项\n3. 第三项\n长安已记"))
        self.assertFalse(style_score("结论：长安已记"))
        self.assertFalse(style_score("结论：开始。\n1. 一\n2. 二\n3. 三\n4. 四\n长安已记"))
        baseline = messages_for("新问题", "baseline", "虚构偏好")
        lora = messages_for("新问题", "lora", "虚构偏好")
        self.assertEqual(baseline, lora)
        self.assertNotIn("虚构偏好", str(baseline))
        self.assertIn("虚构偏好", str(messages_for("新问题", "external", "虚构偏好")))

    def test_aggregate_accepts_round_two_local_report_metrics(self):
        self.assertEqual(metric_value({"rates": {"style": 0.75}}, "style_pass_rate"), 0.75)
        self.assertEqual(metric_value({"rates": {"general": 0.9}}, "general_accuracy"), 0.9)

    def test_round_two_reports_aggregate_without_mixing_seeds(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = []
            for seed in (42, 1337, 2026):
                run = Path(folder) / str(seed)
                run.mkdir()
                protocol = {"version": 4, "lora": {"seeds": [42, 1337, 2026]}}
                evaluations = [{"condition": name, "rates": {"style": 1.0 if name == "hybrid" else 0.5,
                               "general": 0.95, "override": 1.0}}
                               for name in ("baseline", "prompt", "external", "lora", "hybrid")]
                (run / "report.json").write_text(json.dumps({"status": "completed", "seed": seed,
                    "protocol": protocol, "data_sha256": "a" * 64, "code_sha256": "c" * 64,
                    "evaluations": evaluations, "reload_exact_match": True,
                    "rollback_exact_match": True}), encoding="utf-8")
                paths.append(run)
            result = aggregate(paths)
        self.assertTrue(result["gate"]["continue_lora"])
        self.assertEqual(result["metrics"]["hybrid.style_pass_rate"]["mean"], 1.0)

    def test_seed_override_is_bounded_and_does_not_mutate_protocol(self):
        original = {"seed": 42, "steps": 60}
        resolved = resolve_run_config(original, steps=2, seed=43)
        self.assertEqual(original, {"seed": 42, "steps": 60})
        self.assertEqual(resolved, {"seed": 43, "steps": 2})
        with self.assertRaises(ValueError):
            resolve_run_config(original, seed=-1)
        with self.assertRaises(ValueError):
            resolve_run_config(original, steps=0)

    def test_multiseed_gate_rejects_general_capability_regression(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for seed in (7, 23):
                run = root / str(seed)
                run.mkdir()
                evaluations = [{"condition": name, "style_pass_rate": style, "general_accuracy": general}
                               for name, style, general in (("baseline", 0.0, 1.0), ("prompt", 0.0, 1.0),
                                                            ("external", 0.0, 1.0), ("lora", 1.0, 0.25), ("hybrid", 0.2, 0.25))]
                (run / "report.json").write_text(json.dumps({"status": "completed", "seed": seed,
                    "protocol": {"steps": 60, "seed": seed}, "data_sha256": "a" * 64,
                    "code_sha256": "c" * 64, "evaluations": evaluations,
                    "reload_exact_match": True, "rollback_exact_match": True}), encoding="utf-8")
            result = aggregate([root / "7", root / "23"])
        self.assertFalse(result["gate"]["continue_lora"])
        self.assertFalse(result["gate"]["general_ok"])

    def test_multiseed_summary_rejects_mixed_protocols(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = []
            for seed, data_hash in ((7, "a"), (23, "b")):
                run = Path(folder) / str(seed)
                run.mkdir()
                evaluations = [{"condition": name, "style_pass_rate": 0.0, "general_accuracy": 1.0}
                               for name in ("baseline", "prompt", "external", "lora", "hybrid")]
                (run / "report.json").write_text(json.dumps({"status": "completed", "seed": seed,
                    "protocol": {"steps": 60, "seed": seed}, "data_sha256": data_hash * 64,
                    "code_sha256": "c" * 64, "evaluations": evaluations,
                    "reload_exact_match": True, "rollback_exact_match": True}), encoding="utf-8")
                paths.append(run)
            with self.assertRaisesRegex(ValueError, "数据或代码不一致"):
                aggregate(paths)

    def test_multiseed_requires_three_seeds_and_valid_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = []
            for seed in (7, 23, 41):
                path = Path(folder) / str(seed)
                path.mkdir()
                report = {"status": "completed", "seed": seed, "protocol": {"steps": 60, "seed": seed},
                          "data_sha256": "a" * 64, "code_sha256": "c" * 64,
                          "reload_exact_match": True, "rollback_exact_match": True,
                          "evaluations": [{"condition": name, "style_pass_rate": 1.0 if name in ("lora", "hybrid") else 0.0,
                                           "general_accuracy": 1.0}
                                          for name in ("baseline", "prompt", "external", "lora", "hybrid")]}
                (path / "report.json").write_text(json.dumps(report), encoding="utf-8")
                paths.append(path)
            self.assertTrue(aggregate(paths)["gate"]["continue_lora"])
            self.assertFalse(aggregate(paths[:2])["gate"]["continue_lora"])
            for field, value in (("data_sha256", None), ("seed", -1)):
                broken = {**report, field: value}
                (paths[-1] / "report.json").write_text(json.dumps(broken), encoding="utf-8")
                with self.subTest(field=field), self.assertRaises(ValueError):
                    aggregate(paths)
            report["evaluations"][0]["general_accuracy"] = float("nan")
            (paths[-1] / "report.json").write_text(json.dumps(report), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "指标无效"):
                aggregate(paths)
