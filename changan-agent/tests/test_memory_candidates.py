import json
import tempfile
import unittest
from pathlib import Path

from long_term_memory import MemoryStore, validate_candidates
from orchestrator import Agent, BudgetExceeded
from test_reliability import FakeProvider, GOALS, PASS


SOURCE = "我长期偏好先给简短结论。"
CANDIDATE = {"kind": "preference", "title": "回答风格", "content": SOURCE, "evidence": SOURCE}


class Extractor:
    def __init__(self, data=None):
        self.data = {"candidates": [CANDIDATE]} if data is None else data
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return json.dumps(self.data, ensure_ascii=False)


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = MemoryStore(Path(self.temp.name) / "memory.sqlite3")

    def extract(self, provider=None, **overrides):
        return self.store.extract(provider or Extractor(), **{"user_id": "u", "project_id": "p", "source_chat": "c", "source_message": "m", "text": SOURCE, **overrides})

    def test_pending_confirm_restart_and_no_resurrection(self):
        provider = Extractor()
        self.assertEqual(self.extract(provider), 1)
        self.assertEqual(self.store.context("u", "p", "回答风格"), [])
        candidate = self.store.candidates("u", "p")[0]
        record = self.store.confirm_candidate("u", "p", candidate["id"], kind="preference", title="回答风格", content=SOURCE)
        self.assertEqual(record["source_message"], "m")
        self.store.delete("u", "p", record["id"])
        self.store = MemoryStore(self.store.path)
        self.assertEqual(self.extract(provider), 0)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(self.store.candidates("u", "p"), [])
        self.assertEqual(self.store.context("u", "p", "风格"), [])

    def test_rejection_scope_and_atomic_validation(self):
        self.extract()
        c = self.store.candidates("u", "p")[0]
        self.assertEqual(self.store.candidates("other", "p"), [])
        with self.assertRaises(ValueError):
            self.store.confirm_candidate("u", "other", c["id"], kind="preference", title="风格", content=SOURCE)
        self.assertTrue(self.store.reject_candidate("u", "p", c["id"]))
        self.assertEqual(self.extract(), 0)
        invalid = Extractor({"candidates": [CANDIDATE, {**CANDIDATE, "evidence": "模型编造证据"}]})
        with self.assertRaises(ValueError):
            self.extract(invalid, source_message="new")
        self.assertEqual(self.store.candidates("u", "p"), [])

    def test_secret_quotes_and_bad_schema_not_saved(self):
        for raw, source in [({"candidates": [{**CANDIDATE, "kind": []}]}, SOURCE),
                            ({"candidates": [{**CANDIDATE, "evidence": "简短结论"}]}, '他说“简短结论”。'),
                            ({"candidates": [CANDIDATE]}, '```\n' + SOURCE + '\n```'),
                            ({"candidates": [{**CANDIDATE, "evidence": "引用内容"}]}, '> 引用内容'),
                            ({"candidates": [CANDIDATE] * 6}, SOURCE)]:
            with self.assertRaises(ValueError):
                validate_candidates(raw, source)
        provider = Extractor()
        with self.assertRaises(ValueError):
            self.extract(provider, text="凭据 sk-" + "x" * 32)
        self.assertEqual(provider.calls, [])

    def test_conflicting_memory_needs_expected_id_and_revision(self):
        old = self.store.save(user_id="u", project_id="p", kind="preference", title="回答风格", content="旧偏好")
        self.extract()
        candidate = self.store.candidates("u", "p")[0]
        kwargs = dict(kind="preference", title="回答风格", content=SOURCE)
        with self.assertRaises(ValueError):
            self.store.confirm_candidate("u", "p", candidate["id"], **kwargs)
        updated = self.store.confirm_candidate("u", "p", candidate["id"], expected_memory_id=old["id"], expected_revision=old["revision"], **kwargs)
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(updated["content"], SOURCE)
        self.assertEqual(self.store.candidates("u", "p"), [])

    def test_cancellation_and_moved_chat_drop_pending_only(self):
        self.assertEqual(self.extract(stop_check=lambda: True), 0)
        self.extract()
        self.store.dismiss_chat_candidates("u", "p", "c")
        self.assertEqual(self.store.candidates("u", "p"), [])
        self.assertEqual(self.extract(), 0)

    def test_engine_optional_extraction_failure_and_budget_keep_answer(self):
        for failure in (ValueError("格式错误"), BudgetExceeded("budget")):
            events = []
            def broken(*args):
                raise failure
            agent = Agent(FakeProvider("GPT", [GOALS, PASS]), FakeProvider("D", ['{"summary":"有效回答","actions":[]}']),
                          memory_extract=broken, on_event=lambda *event: events.append(event))
            result = agent.run("虚构问题")
            self.assertEqual(result["final"], "有效回答")
            self.assertTrue(agent.saved["memory_extraction_done"])
            self.assertTrue(any(e[0] == "memory_candidates" for e in events))

    def test_evaluation_input_excludes_expected_answers_and_score_is_exact(self):
        from experiments.memory.evaluate import messages_for, score
        protocol = json.loads((Path(__file__).parents[1] / "experiments/memory/protocol.json").read_text(encoding="utf-8"))
        questions = protocol["snapshots"][0]["questions"]
        payload = messages_for(questions)
        self.assertNotIn("QH-731", payload[1]["content"])
        self.assertNotIn("2027", payload[1]["content"])
        self.assertTrue(score(questions[1], "2027-02-14"))
        self.assertFalse(score(questions[1], "2027年3月14日"))
        updated = protocol["snapshots"][1]["questions"][1]
        self.assertTrue(score(updated, "当前是2027年3月9日。原定2027年2月14日已经取消。"))
        self.assertFalse(score(updated, "当前是2027年3月9日或2027年2月14日。"))
