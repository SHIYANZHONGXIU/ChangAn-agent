import json
import copy
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from orchestrator import Agent, BudgetExceeded, Provider, ProviderRequestError, usage_summary
from persistence import read_json, write_json


class MockHandler(BaseHTTPRequestHandler):
    calls = 0
    mode = "ok"

    def log_message(self, *_):
        return

    def do_POST(self):
        MockHandler.calls += 1
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
        MockHandler.last_payload = payload
        if "/always429/" in self.path:
            self.send_response(429); self.end_headers(); self.wfile.write(b'{"error":"rate limited"}'); return
        if "/retry/" in self.path and MockHandler.calls == 1:
            self.send_response(429); self.end_headers(); self.wfile.write(b'{"error":"rate limited"}'); return
        if "/reasoning/" in self.path and "reasoning_effort" in payload:
            self.send_response(400); self.end_headers(); self.wfile.write(b'{"error":"reasoning_effort unsupported"}'); return
        if "/slow/" in self.path:
            time.sleep(2)
        if "/badstream/" in self.path:
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers(); self.wfile.write(b"data: not-json\n\n"); self.wfile.flush(); return
        if "/truncated/" in self.path:
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
            self.wfile.write(b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'); self.wfile.flush(); return
        if "/lengthlimit/" in self.path:
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
            self.wfile.write(b'data: {"choices":[{"delta":{"content":"partial"},"finish_reason":"length"}]}\n\n'); self.wfile.flush(); return
        if "/usagechunk/" in self.path:
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
            self.wfile.write(b'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}\n\ndata: {"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":1,"total_tokens":3}}\n\ndata: [DONE]\n\n'); self.wfile.flush(); return
        if payload.get("stream"):
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
            for text in ("hello", " world"):
                line = json.dumps({"choices": [{"delta": {"content": text}}]}).encode()
                self.wfile.write(b"data: " + line + b"\n\n"); self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush(); return
        usage = {"prompt_tokens": -3, "completion_tokens": 2, "total_tokens": -1} if "/badusage/" in self.path else {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
        body = json.dumps({"choices": [{"message": {"content": json.dumps({"ok": True})}}], "usage": usage}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)


class QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        return


class ProviderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = QuietServer(("127.0.0.1", 0), MockHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True); cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close()

    def setUp(self):
        MockHandler.calls = 0

    def test_retry_on_429(self):
        provider = Provider("mock", self.base + "/retry", "x", "m", retries=2, timeout=3)
        self.assertIn("ok", provider.chat([{"role": "user", "content": "hi"}]))
        self.assertEqual(provider.usage["attempts"], 2)

    def test_request_is_not_sent_when_reservation_exceeds_budget(self):
        provider = Provider("mock", self.base, "x", "m", retries=1)
        provider.configure_budget(1, 1, 0.01, "CNY", 16)
        with self.assertRaises(BudgetExceeded):
            provider.chat([{"role": "user", "content": "hi"}])
        self.assertEqual((MockHandler.calls, provider.usage["attempts"]), (0, 0))

    def test_reported_usage_replaces_reservation_once(self):
        p = Provider("mock", self.base, "x", "m", retries=1)
        p.configure_budget(1, 2, currency="CNY", max_output_tokens=16)
        p.chat([])
        self.assertAlmostEqual(p.usage["estimated_cost"], 0.007)
        self.assertEqual(p.usage["total_tokens"], 5)
        self.assertEqual(p.usage["request_log"][0]["usage_source"], "reported")

    def test_explicit_stage_limit_cannot_exceed_configured_output_cap(self):
        p = Provider("mock", self.base, "x", "m", retries=1)
        p.configure_budget(max_output_tokens=1024)
        p.chat([], max_tokens=1600)
        self.assertEqual(MockHandler.last_payload["max_tokens"], 1024)

    def test_output_limit_does_not_repeat_same_truncated_request(self):
        p = Provider("mock", self.base + "/lengthlimit", "x", "m", retries=3)
        with self.assertRaises(ProviderRequestError):
            p.chat([], on_delta=lambda _: None)
        self.assertEqual(MockHandler.calls, 1)
        self.assertEqual(p.usage["attempts"], 1)

    def test_unknown_historical_cost_blocks_budget_before_tools_or_requests(self):
        p, deep = (Provider(name, self.base, "x", "m") for name in ("G", "D"))
        p.restore_usage({"attempts": 1, "total_tokens": 10, "estimated_cost": 0})
        with self.assertRaises(BudgetExceeded):
            Agent(p, deep, max_budget=10)._check_budget()
        self.assertEqual(MockHandler.calls, 0)

    def test_incomplete_prices_or_currency_cannot_enforce_money_budget(self):
        for prices in ((1, 0, "CNY"), (0, 1, "USD"), (1, 1, "")):
            provider = Provider("mock", self.base, "x", "m", retries=1)
            provider.configure_budget(prices[0], prices[1], 10, prices[2], 16)
            with self.assertRaises(BudgetExceeded):
                provider.chat([])
        self.assertEqual(MockHandler.calls, 0)

    def test_task_budget_reserves_across_providers(self):
        gpt, deep = (Provider(name, self.base, "x", "m") for name in ("GPT", "D"))
        for p in (gpt, deep):
            p.configure_budget(1, 1, currency="CNY", max_output_tokens=16)
        gpt.usage.update(attempts=1, total_tokens=5, estimated_cost=0.95)
        Agent(gpt, deep, max_budget=2)
        with self.assertRaises(BudgetExceeded):
            deep.chat([{"role": "user", "content": "hi"}])
        self.assertEqual((MockHandler.calls, deep.usage["attempts"]), (0, 0))

    def test_failed_attempt_is_reserved_before_retry(self):
        p = Provider("mock", self.base + "/always429", "x", "m", retries=3)
        p.configure_budget(1, 1, 1.5, "CNY", 16)
        with self.assertRaises(BudgetExceeded):
            p.chat([{"role": "user", "content": "hi"}])
        self.assertEqual((MockHandler.calls, p.usage["attempts"]), (1, 1))
        self.assertEqual(p.usage["request_log"][0]["status"], "failed")
        self.assertGreater(p.usage["estimated_cost"], 1)
        self.assertEqual(p.usage["uncertain_attempts"], 1)

    def test_checkpoint_write_failure_prevents_network_request(self):
        p = Provider("mock", self.base, "x", "m", retries=1)
        def fail():
            raise OSError("disk full")
        p.on_usage = fail
        with self.assertRaises(OSError):
            p.chat([])
        self.assertEqual(MockHandler.calls, 0)

    def test_unfinished_reservation_survives_restore_without_repricing(self):
        p = Provider("mock", self.base, "x", "m")
        p.configure_budget(1, 2, currency="CNY", max_output_tokens=16)
        p._start_attempt(b"{}", 16)
        snapshot = copy.deepcopy(p.usage)
        restored = Provider("mock", self.base, "x", "m")
        restored.configure_budget(10, 20, currency="CNY", max_output_tokens=16)
        restored.restore_usage(snapshot)
        self.assertEqual(restored.usage["estimated_cost"], p.usage["estimated_cost"])
        self.assertEqual(restored.usage["request_log"][0]["status"], "uncertain")
        tokens = restored.usage["total_tokens"]
        restored.restore_usage(copy.deepcopy(restored.usage))
        self.assertEqual(restored.usage["total_tokens"], tokens)
        self.assertEqual(restored.usage["uncertain_attempts"], 1)

    def test_invalid_usage_is_estimated_without_losing_valid_answer(self):
        p = Provider("mock", self.base + "/badusage", "x", "m", retries=1)
        self.assertIn("ok", p.chat([], max_tokens=16))
        self.assertTrue(p.usage["estimated_tokens"])
        self.assertGreater(p.usage["total_tokens"], 0)
        self.assertEqual(MockHandler.calls, 1)

    def test_known_cost_requires_all_used_providers_and_one_currency(self):
        known = {"attempts": 1, "total_tokens": 10, "cost_known": True, "currency": "CNY", "estimated_cost": 0.01}
        unknown = {**known, "cost_known": False}
        self.assertIsNone(usage_summary({"gpt": known, "deepseek": unknown})["cost"])
        self.assertIsNone(usage_summary({"gpt": known, "deepseek": {**known, "currency": "USD"}})["cost"])
        summary = usage_summary({"gpt": known, "claude": {"attempts": 0, "cost_known": False}})
        self.assertEqual((summary["cost"], summary["cost_currency"]), (0.01, "CNY"))

    def test_claude_failure_cannot_bypass_shared_budget_by_fallback(self):
        gpt = Provider("GPT", self.base, "x", "m", retries=1)
        claude = Provider("Claude", self.base + "/always429", "x", "m", retries=1)
        deep = Provider("DeepSeek", self.base, "x", "m", retries=1)
        for p in (gpt, claude, deep):
            p.configure_budget(1, 1, currency="CNY", max_output_tokens=16)
        agent = Agent(gpt, deep, verifier=claude, max_budget=1.5)
        with self.assertRaises(BudgetExceeded):
            agent._verify([])
        self.assertEqual(MockHandler.calls, 1)
        self.assertEqual(gpt.usage["attempts"], 0)
        self.assertEqual(claude.usage["attempts"], 1)

    def test_removed_claude_usage_is_preserved_on_resume(self):
        with tempfile.TemporaryDirectory() as folder:
            checkpoint = Path(folder) / "checkpoint.json"
            write_json(checkpoint, {"task": "hi", "stage": "before_analysis", "next_round": 1,
                "rounds": [], "state": {}, "previous": "", "feedback": "",
                "usage": {"claude": {"attempts": 1, "total_tokens": 5, "estimated_cost": 0.9, "cost_known": True, "currency": "CNY"}}})
            gpt, deep = (Provider(name, self.base, "x", "m") for name in ("GPT", "D"))
            for p in (gpt, deep):
                p.configure_budget(1, 1, currency="CNY", max_output_tokens=16)
            with self.assertRaises(BudgetExceeded):
                Agent(gpt, deep, max_budget=1, checkpoint_path=checkpoint).run("hi")
            self.assertEqual(read_json(checkpoint)["usage"]["claude"]["estimated_cost"], 0.9)
        self.assertEqual(MockHandler.calls, 0)

    def test_optional_memory_extraction_cannot_spend_over_budget(self):
        gpt, deep = (Provider(name, self.base, "x", "m") for name in ("GPT", "D"))
        for p in (gpt, deep):
            p.configure_budget(1, 1, currency="CNY", max_output_tokens=16)
        agent = Agent(gpt, deep, max_budget=0.01, memory_extract=lambda p, stop: p.chat([]))
        agent.saved = {"rounds": [{"execution": '{"summary":"有效回答","actions":[]}'}]}
        self.assertEqual(agent._complete("passed")["final"], "有效回答")
        self.assertEqual(MockHandler.calls, 0)

    def test_image_content_survives_http_serialization(self):
        from io import BytesIO
        import base64
        from PIL import Image
        image = BytesIO()
        Image.new("RGB", (2, 2), "red").save(image, format="PNG")
        content = [{"type": "text", "text": "描述图片"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(image.getvalue()).decode("ascii")}}]
        provider = Provider("mock", self.base, "x", "m", retries=1, timeout=3)
        provider.chat([{"role": "user", "content": content}])
        self.assertEqual(MockHandler.last_payload["messages"][0]["content"], content)

    def test_output_limit_is_validated_and_sent(self):
        provider = Provider("mock", self.base, "x", "m", retries=1, timeout=3)
        provider.chat([{"role": "user", "content": "hi"}], max_tokens=512)
        self.assertEqual(MockHandler.last_payload["max_tokens"], 512)
        for value in (0, -1, True, 1.5, 40000):
            with self.assertRaises(ValueError):
                provider.chat([{"role": "user", "content": "hi"}], max_tokens=value)

    def test_reasoning_fallback(self):
        provider = Provider("mock", self.base + "/reasoning", "x", "m", reasoning_effort="high", retries=2, timeout=3)
        self.assertIn("ok", provider.chat([{"role": "user", "content": "hi"}]))
        self.assertEqual(provider.usage["requests"], 1)

    def test_streaming(self):
        pieces = []
        provider = Provider("mock", self.base, "x", "m", retries=1, timeout=3)
        self.assertEqual(provider.chat([{"role": "user", "content": "hi"}], on_delta=pieces.append), "hello world")
        self.assertEqual("".join(pieces), "hello world")

    def test_sequential_requests_on_same_provider(self):
        provider = Provider("mock", self.base, "x", "m", retries=1, timeout=3)
        self.assertIn("ok", provider.chat([{"role": "user", "content": "one"}]))
        self.assertIn("ok", provider.chat([{"role": "user", "content": "two"}]))

    def test_timeout_is_reported(self):
        provider = Provider("mock", self.base + "/slow", "x", "m", retries=1, timeout=1)
        with self.assertRaises(RuntimeError):
            provider.chat([{"role": "user", "content": "hi"}])

    def test_bad_sse_is_rejected(self):
        provider = Provider("mock", self.base + "/badstream", "x", "m", retries=1, timeout=3)
        with self.assertRaises(RuntimeError):
            provider.chat([{"role": "user", "content": "hi"}], on_delta=lambda _: None)

    def test_truncated_stream_is_not_accepted_as_complete(self):
        provider = Provider("mock", self.base + "/truncated", "x", "m", retries=1, timeout=3)
        with self.assertRaises(RuntimeError):
            provider.chat([{"role": "user", "content": "hi"}], on_delta=lambda _: None)

    def test_usage_chunk_with_empty_choices_is_supported(self):
        provider = Provider("mock", self.base + "/usagechunk", "x", "m", retries=1, timeout=3)
        self.assertEqual(provider.chat([{"role": "user", "content": "hi"}], on_delta=lambda _: None), "OK")
        self.assertEqual(provider.usage["total_tokens"], 3)
        self.assertFalse(provider.usage["estimated_tokens"])

    def test_cancel_during_retry_wait(self):
        stop = threading.Event()
        provider = Provider("mock", self.base + "/always429", "x", "m", retries=5, timeout=3)
        provider.on_retry = lambda *_: stop.set()
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "stopped"):
            provider.chat([{"role": "user", "content": "hi"}], stop_check=stop.is_set)
        self.assertEqual(provider.usage["attempts"], 1)
        self.assertLess(time.monotonic() - started, 2)

    def test_cancel_stops_request_process(self):
        stop = threading.Event()
        provider = Provider("mock", self.base + "/slow", "x", "m", retries=1, timeout=10)
        provider.configure_budget(1, 1, currency="CNY", max_output_tokens=16)
        errors = []
        worker = threading.Thread(target=lambda: self._capture(provider, stop, errors), daemon=True)
        worker.start(); time.sleep(0.2); stop.set(); worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertTrue(errors and "stopped" in str(errors[0]))
        self.assertGreater(provider.usage["estimated_cost"], 0)
        self.assertEqual(provider.usage["request_log"][0]["status"], "cancelled")

    @staticmethod
    def _capture(provider, stop, errors):
        try:
            provider.chat([{"role": "user", "content": "hi"}], stop_check=stop.is_set)
        except Exception as exc:
            errors.append(exc)


if __name__ == "__main__":
    unittest.main()
