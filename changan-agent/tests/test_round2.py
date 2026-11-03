import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from experiments.round2.dataset import make_data, validate, score, local_messages
from experiments.round2.memory_run import HERE, budget_exceeded, messages_for, prepare_store, request_budget_reservation, summarize


class RoundTwoTests(unittest.TestCase):
    def setUp(self):
        self.memory = json.loads((HERE / "memory_sessions.json").read_text(encoding="utf-8"))
        self.lora = json.loads((HERE / "lora_data.json").read_text(encoding="utf-8"))

    def test_frozen_data_are_reproducible_and_group_disjoint(self):
        self.assertEqual((self.memory, self.lora), make_data())
        validate(self.memory, self.lora)
        protocol = json.loads((HERE / "protocol.json").read_text(encoding="utf-8"))
        for filename, key in (("memory_sessions.json", "memory_data_sha256"), ("lora_data.json", "lora_data_sha256")):
            self.assertEqual(hashlib.sha256((HERE / filename).read_bytes()).hexdigest(), protocol[key])
        groups = [{r["group"] for r in self.lora[split]} for split in ("train", "dev", "test")]
        self.assertEqual([len(g) for g in groups], [12, 8, 10])
        self.assertFalse(groups[0] & groups[1] or groups[0] & groups[2] or groups[1] & groups[2])

    def test_duplicate_prompt_cannot_cross_splits(self):
        self.lora["test"][0]["prompt"] = self.lora["train"][0]["prompt"]
        with self.assertRaises(ValueError):
            validate(self.memory, self.lora)

    def test_request_excludes_answers_future_and_other_project(self):
        session = self.memory["sessions"][-1]
        for condition in ("recent", "history", "summary", "external"):
            payload = json.loads(messages_for(session, condition)[1]["content"])
            self.assertEqual(set(payload["questions"][0]), {"id", "text"})
            text = json.dumps(payload, ensure_ascii=False)
            self.assertNotIn(session["other_project"]["code"], text)
            self.assertNotIn(session["deleted_source"]["value"], text)
            self.assertNotIn(session["future_messages"][0]["content"], text)
            if condition == "recent":
                self.assertEqual(len(payload["history"]), 12)
                self.assertNotIn(session["questions"][0]["expected"], text)

    def test_control_records_update_isolate_and_forget(self):
        session = self.memory["sessions"][-1]
        with tempfile.TemporaryDirectory() as folder:
            store = prepare_store(session, Path(folder) / "memory.sqlite3")
            records = store.context("virtual", session["id"], " ".join(q["text"] for q in session["questions"]))
            self.assertTrue({r["title"] for r in session["records"]} <= {r["title"] for r in records})
            text = json.dumps(records, ensure_ascii=False)
            self.assertNotIn(session["other_project"]["code"], text)
            self.assertNotIn(session["deleted_source"]["value"], text)
            self.assertLessEqual(len(records), 6)

    def test_scorer_rejects_negation_old_dates_and_extra_output(self):
        session = self.memory["sessions"][-1]
        self.assertTrue(score(session["questions"][0], session["questions"][0]["expected"]))
        self.assertFalse(score(session["questions"][1], "2035-12-31"))
        state = session["questions"][2]
        self.assertFalse(score(state, "尚未完成" + state["contains"][0] + "，下一步" + state["contains"][1]))
        self.assertFalse(score(session["questions"][5], "未知，但猜测是5月1日"))
        self.assertFalse(score(session["questions"][4], "结论：收到"))

    def test_fewshot_baseline_does_not_include_heldout_reference(self):
        row = self.lora["test"][0]
        for condition in ("baseline", "prompt", "external", "lora", "hybrid"):
            payload = json.dumps(local_messages(row["prompt"], condition), ensure_ascii=False)
            self.assertNotIn(row["answer"], payload)
        self.assertEqual(local_messages("测试", "baseline"), local_messages("测试", "lora"))

    def test_failed_calls_remain_in_denominator(self):
        report = {"runs": [{"kind": "answer", "split": "test", "provider": "gpt", "condition": "history", "question_count": 9, "error": "timeout"}]}
        row = summarize(report)[0]
        self.assertEqual((row["passed"], row["questions"], row["errors"]), (0, 9, 1))

    def test_budget_skips_are_reported_but_not_scored_as_wrong(self):
        report = {"runs": [{"kind": "answer", "split": "test", "provider": "gpt", "condition": "history",
                             "question_count": 9, "status": "skipped_budget",
                             "error": "达到冻结评测用量或输入上限，未发请求"}]}
        row = summarize(report)[0]
        self.assertEqual((row["questions"], row["scored_questions"], row["not_sent_questions"], row["errors"]), (0, 0, 9, 0))

    def test_request_reservation_includes_output_headroom(self):
        messages = [{"role": "user", "content": "中文测试"}]
        self.assertGreaterEqual(request_budget_reservation(messages, 2048), 2048 + 1024)

    def test_budget_gate_counts_inflight_reservations(self):
        report = {"attempts": 1, "total_tokens": 100, "runs": [{"status": "inflight", "budget_reservation_tokens": 700}]}
        settings = {"max_requests": 10, "max_total_tokens": 1000, "max_input_characters": 10000}
        self.assertTrue(budget_exceeded(report, settings, 10, 300))
