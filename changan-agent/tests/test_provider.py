import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from orchestrator import Provider


class MockHandler(BaseHTTPRequestHandler):
    calls = 0
    mode = "ok"

    def log_message(self, *_):
        return

    def do_POST(self):
        MockHandler.calls += 1
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
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
        if "/usagechunk/" in self.path:
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
            self.wfile.write(b'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}\n\ndata: {"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":1,"total_tokens":3}}\n\ndata: [DONE]\n\n'); self.wfile.flush(); return
        if payload.get("stream"):
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
            for text in ("hello", " world"):
                line = json.dumps({"choices": [{"delta": {"content": text}}]}).encode()
                self.wfile.write(b"data: " + line + b"\n\n"); self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush(); return
        body = json.dumps({"choices": [{"message": {"content": json.dumps({"ok": True})}}], "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}).encode()
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
        errors = []
        worker = threading.Thread(target=lambda: self._capture(provider, stop, errors), daemon=True)
        worker.start(); time.sleep(0.2); stop.set(); worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertTrue(errors and "stopped" in str(errors[0]))

    @staticmethod
    def _capture(provider, stop, errors):
        try:
            provider.chat([{"role": "user", "content": "hi"}], stop_check=stop.is_set)
        except Exception as exc:
            errors.append(exc)


if __name__ == "__main__":
    unittest.main()
