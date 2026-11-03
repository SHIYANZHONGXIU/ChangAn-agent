"""实际办公文件读写回归，使用临时项目目录。"""

import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from orchestrator import ToolExecutor
from specialized_tools import validate_browser_actions, validate_xlsx_values


class DocumentToolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.tools = ToolExecutor(self.root, "full access")

    def test_browser_actions_are_bounded_and_typed(self):
        validate_browser_actions([{"type": "click", "selector": "button.save"}])
        for actions in ([{"type": "evaluate", "script": "alert(1)"}], [{"type": "wait", "milliseconds": 30001}], [{"type": "fill", "selector": "#x", "value": "x" * 10001}], [{"type": "click", "selector": "#x"}] * 21):
            with self.assertRaises(ValueError):
                validate_browser_actions(actions)

    def test_excel_write_rejects_oversized_or_out_of_bounds_data(self):
        with self.assertRaisesRegex(RuntimeError, "单元格"):
            validate_xlsx_values([[1] * 101] * 100, 1, 1)
        with self.assertRaisesRegex(RuntimeError, "边界"):
            validate_xlsx_values([[1]], 1, 16385)
        with self.assertRaisesRegex(RuntimeError, "32767"):
            validate_xlsx_values([["x" * 32768]], 1, 1)
        with self.assertRaisesRegex(RuntimeError, "单元格"):
            self.tools.run("xlsx_write", {"path": "new.xlsx", "values": [[1] * 101] * 100})
        self.assertFalse((self.root / "new.xlsx").exists())
        self.assertFalse((self.root / ".agent-backups").exists())

    def test_excel_sheet_dates_write_and_version_backup(self):
        from openpyxl import Workbook, load_workbook
        workbook = Workbook()
        workbook.active.title = "默认页"
        sheet = workbook.create_sheet("数据")
        sheet.append(["时间", "数量"])
        sheet.append([datetime(2026, 10, 30, 9, 30), 2])
        path = self.root / "book.xlsx"
        workbook.save(path)
        workbook.close()
        result = self.tools.run("xlsx_read", {"path": "book.xlsx", "sheet": "数据", "max_row": 2, "max_col": 2})
        self.assertEqual(list(result["sheets"]), ["数据"])
        self.assertIn("2026-10-30T09:30:00", json.dumps(result))
        self.tools.run("xlsx_write", {"path": "book.xlsx", "sheet": "数据", "row": 2, "column": 2, "values": [[7]]})
        current = load_workbook(path)
        try:
            self.assertEqual(current["数据"]["B2"].value, 7)
        finally:
            current.close()
        backup = next((self.root / ".agent-backups").glob("*.bak"))
        with backup.open("rb") as handle:
            original = load_workbook(handle)
            try:
                self.assertEqual(original["数据"]["B2"].value, 2)
            finally:
                original.close()

    def test_pdf_extract_honors_selected_pages(self):
        from pypdf import PdfWriter
        writer = PdfWriter()
        for _ in range(3):
            writer.add_blank_page(width=200, height=200)
        writer.write(self.root / "pages.pdf")
        writer.close()
        result = self.tools.run("pdf_extract", {"path": "pages.pdf", "start_page": 2, "end_page": 2})
        self.assertEqual(result["selected_pages"], [2, 2])
        self.assertEqual(result["pages"], 3)

    def test_pdf_extract_caps_each_request_at_twenty_pages(self):
        from pypdf import PdfWriter
        writer = PdfWriter()
        for _ in range(21):
            writer.add_blank_page(width=200, height=200)
        writer.write(self.root / "long.pdf")
        writer.close()
        with self.assertRaisesRegex(ValueError, "最多提取 20 页"):
            self.tools.run("pdf_extract", {"path": "long.pdf", "start_page": 1, "end_page": 21})

    def test_docx_extract_reads_paragraphs_and_table(self):
        from docx import Document
        document = Document()
        document.add_paragraph("长安文档回归")
        table = document.add_table(rows=1, cols=2)
        table.cell(0, 0).text = "字段"
        table.cell(0, 1).text = "结果"
        document.save(self.root / "report.docx")
        result = self.tools.run("docx_extract", {"path": "report.docx"})
        self.assertIn("长安文档回归", result["text"])
        self.assertEqual(result["tables"], [["字段", "结果"]])

    def test_windows_browser_uses_system_edge_when_playwright_chromium_is_missing(self):
        from unittest.mock import Mock, patch
        from specialized_tools import _launch_browser
        playwright = Mock()
        playwright.chromium.executable_path = str(self.root / "browser-not-installed.exe")
        with patch("specialized_tools.os.name", "nt"):
            _launch_browser(playwright, "http://127.0.0.1:18765")
        options = playwright.chromium.launch.call_args.kwargs
        self.assertEqual(options["channel"], "msedge")
        self.assertEqual(options["proxy"]["server"], "http://127.0.0.1:18765")
        self.assertIn("MAP * ~NOTFOUND", options["args"][0])

    def test_windows_browser_falls_back_to_edge_if_chromium_runtime_is_incompatible(self):
        from unittest.mock import Mock, call, patch
        from specialized_tools import _launch_browser
        executable = self.root / "chromium.exe"
        executable.touch()
        playwright = Mock()
        playwright.chromium.executable_path = str(executable)
        playwright.chromium.launch.side_effect = [RuntimeError("runtime version mismatch"), object()]
        with patch("specialized_tools.os.name", "nt"):
            _launch_browser(playwright, "http://127.0.0.1:18765")
        self.assertEqual(playwright.chromium.launch.call_args_list,
                         [call(executable_path=str(executable), headless=True, proxy={"server": "http://127.0.0.1:18765", "bypass": "<-loopback>"}, args=["--host-resolver-rules=MAP * ~NOTFOUND", "--disable-quic", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"]),
                          call(channel="msedge", headless=True, proxy={"server": "http://127.0.0.1:18765", "bypass": "<-loopback>"}, args=["--host-resolver-rules=MAP * ~NOTFOUND", "--disable-quic", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"])])

    def test_browser_proxy_connects_to_dns_validated_ip(self):
        from unittest.mock import Mock, patch
        from urllib.parse import urlparse
        from specialized_tools import _open_pinned_connection
        connection = Mock()
        with patch("specialized_tools.resolve_public_url", return_value=(urlparse("https://example.com:443/"), ["93.184.216.34"])), patch("specialized_tools.socket.create_connection", return_value=connection) as connect:
            self.assertIs(_open_pinned_connection("https://example.com/", set()), connection)
        self.assertEqual(connect.call_args.args[0], ("93.184.216.34", 443))

    def test_browser_proxy_blocks_direct_loopback_connect(self):
        import socket
        from specialized_tools import PinnedBrowserProxy
        target = socket.socket()
        target.bind(("127.0.0.1", 0))
        target.listen(1)
        target.settimeout(0.2)
        try:
            with PinnedBrowserProxy(set()) as proxy:
                client = socket.create_connection(proxy.server.server_address, timeout=2)
                client.sendall(f"CONNECT 127.0.0.1:{target.getsockname()[1]} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode())
                response = client.recv(1024)
                client.close()
            self.assertTrue(response.startswith(b"HTTP/1.1 502"))
            with self.assertRaises(socket.timeout):
                target.accept()
        finally:
            target.close()

    def test_browser_proxy_enforces_total_transfer_budget(self):
        from specialized_tools import MAX_BROWSER_TOTAL_BYTES, PinnedBrowserProxy
        with PinnedBrowserProxy(set()) as proxy:
            self.assertTrue(proxy.server.reserve_bytes(MAX_BROWSER_TOTAL_BYTES - 5))
            self.assertFalse(proxy.server.reserve_bytes(6))

    def test_browser_proxy_forwards_http_without_exposing_proxy_headers(self):
        import http.server
        import socket
        import threading
        from unittest.mock import patch
        from specialized_tools import PinnedBrowserProxy
        received = {}
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                received["path"] = self.path
                received["host"] = self.headers.get("Host")
                received["proxy_auth"] = self.headers.get("Proxy-Authorization")
                body = b"proxy-ok"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *_):
                pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            with PinnedBrowserProxy(set()) as proxy, patch("specialized_tools._open_pinned_connection", side_effect=lambda *_: socket.create_connection(server.server_address)):
                client = socket.create_connection(proxy.server.server_address, timeout=3)
                client.sendall(b"GET http://example.com/ok?q=1 HTTP/1.1\r\nHost: example.com\r\nProxy-Authorization: secret\r\nProxy-Connection: keep-alive\r\n\r\n")
                chunks = []
                while chunk := client.recv(4096):
                    chunks.append(chunk)
                client.close()
            self.assertIn(b"proxy-ok", b"".join(chunks))
            self.assertEqual(received, {"path": "/ok?q=1", "host": "example.com", "proxy_auth": None})
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)

    def test_browser_proxy_tunnels_https_bytes_without_terminating_tls(self):
        import socket
        import threading
        from unittest.mock import patch
        from specialized_tools import PinnedBrowserProxy
        target = socket.socket()
        target.bind(("127.0.0.1", 0))
        target.listen(1)
        target_address = target.getsockname()
        def echo():
            connection, _ = target.accept()
            with connection:
                connection.sendall(connection.recv(1024)[::-1])
        echo_thread = threading.Thread(target=echo, daemon=True)
        echo_thread.start()
        try:
            with PinnedBrowserProxy(set()) as proxy, patch("specialized_tools._open_pinned_connection", side_effect=lambda *_: socket.create_connection(target_address)):
                client = socket.create_connection(proxy.server.server_address, timeout=3)
                client.sendall(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com\r\n\r\n")
                self.assertTrue(client.recv(1024).startswith(b"HTTP/1.1 200"))
                client.sendall(b"hello")
                self.assertEqual(client.recv(1024), b"olleh")
                client.close()
        finally:
            target.close()
            echo_thread.join(timeout=2)
