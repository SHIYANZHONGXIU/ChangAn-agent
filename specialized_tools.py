"""可选的浏览器和文档工具；依赖缺失时返回可操作的提示。"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
import select
import socket
import socketserver
import os
import zipfile
import math
import threading


from network_safety import resolve_public_url


MAX_DOCUMENT_BYTES = 100 * 1024 * 1024
MAX_EXCEL_CELLS_PER_WRITE = 10_000
MAX_BROWSER_CONNECTION_BYTES = 100 * 1024 * 1024
MAX_BROWSER_TOTAL_BYTES = 200 * 1024 * 1024


def validate_document_path(raw_path):
    path = Path(raw_path)
    if not path.is_file():
        raise RuntimeError("文档不存在或不是普通文件")
    if path.stat().st_size > MAX_DOCUMENT_BYTES:
        raise RuntimeError("文档超过 100 MB 的读取上限")
    if path.suffix.lower() in (".docx", ".xlsx"):
        try:
            with zipfile.ZipFile(path) as archive:
                files = archive.infolist()
                expanded = sum(item.file_size for item in files)
                if len(files) > 20_000 or expanded > 500 * 1024 * 1024 or any(
                    item.file_size > 250 * 1024 * 1024 or item.file_size > 10_000_000 and item.compress_size == 0
                    or item.compress_size and item.file_size / item.compress_size > 1000
                    for item in files
                ):
                    raise RuntimeError("Office 文档展开体积或压缩比例超过安全限制")
        except zipfile.BadZipFile as exc:
            raise RuntimeError("Office 文档格式损坏") from exc
    return path


def validate_browser_actions(actions):
    if not isinstance(actions, list) or len(actions) > 20:
        raise ValueError("浏览器每次最多执行 20 个操作")
    allowed = {"click", "fill", "wait", "wait_for", "screenshot", "download"}
    for action in actions:
        if not isinstance(action, dict) or action.get("type") not in allowed:
            raise ValueError("浏览器操作类型无效")
        if action["type"] in ("click", "fill", "wait_for", "download"):
            selector = action.get("selector")
            if not isinstance(selector, str) or not selector or len(selector) > 2048:
                raise ValueError("浏览器操作需要有效的 CSS 选择器")
        if action["type"] == "fill" and (not isinstance(action.get("value", ""), str) or len(action.get("value", "")) > 10000):
            raise ValueError("浏览器填写内容超过限制")
        if action["type"] == "wait":
            milliseconds = action.get("milliseconds", 500)
            if not isinstance(milliseconds, int) or isinstance(milliseconds, bool) or not 0 <= milliseconds <= 30000:
                raise ValueError("浏览器等待时间须在 0 至 30000 毫秒之间")
        if action["type"] == "wait_for" and action.get("state", "visible") not in ("attached", "detached", "visible", "hidden"):
            raise ValueError("浏览器等待状态无效")
        if action["type"] == "screenshot" and "full_page" in action and not isinstance(action["full_page"], bool):
            raise ValueError("截图 full_page 必须为布尔值")


def validate_xlsx_values(values, row, column):
    if not isinstance(values, list) or not values or not all(isinstance(item, list) for item in values):
        raise RuntimeError("values 必须是二维数组")
    cells = sum(len(item) for item in values)
    if not cells:
        raise RuntimeError("至少需要写入一个单元格")
    if cells > MAX_EXCEL_CELLS_PER_WRITE:
        raise RuntimeError(f"单次 Excel 修改最多 {MAX_EXCEL_CELLS_PER_WRITE} 个单元格")
    if any(isinstance(value, str) and len(value) > 32767 for item in values for value in item):
        raise RuntimeError("单元格文本超过 Excel 的 32767 字符限制")
    if any(value is not None and not isinstance(value, (str, int, float, bool)) for item in values for value in item):
        raise RuntimeError("Excel 单元格只接受文本、数字、布尔值或空值")
    if any(isinstance(value, float) and not math.isfinite(value) for item in values for value in item):
        raise RuntimeError("Excel 数值必须是有限数字")
    if row < 1 or column < 1 or row + len(values) - 1 > 1_048_576 or any(column + len(item) - 1 > 16_384 for item in values if item):
        raise RuntimeError("Excel 写入范围超出工作表边界")


def _launch_browser(playwright, proxy_url):
    options = {"headless": True, "proxy": {"server": proxy_url, "bypass": "<-loopback>"}, "args": ["--host-resolver-rules=MAP * ~NOTFOUND", "--disable-quic", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"]}
    executable = Path(playwright.chromium.executable_path)
    if executable.is_file():
        try:
            return playwright.chromium.launch(executable_path=str(executable), **options)
        except Exception:
            if os.name != "nt":
                raise
    if os.name == "nt":
        return playwright.chromium.launch(channel="msedge", **options)
    raise RuntimeError("没有找到 Playwright Chromium；请先安装对应浏览器运行时")


def validate_external_url(raw_url, allowed_domains=None):
    parsed, _ = resolve_public_url(raw_url, allowed_domains)
    return parsed.hostname


def _open_pinned_connection(url, allowed_domains):
    parsed, addresses = resolve_public_url(url, allowed_domains)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    last_error = None
    for address in addresses:
        try:
            return socket.create_connection((address, port), timeout=15)
        except OSError as exc:
            last_error = exc
    raise RuntimeError("无法连接已验证的公网地址") from last_error


class _BrowserProxyHandler(socketserver.BaseRequestHandler):
    def _relay(self, remote):
        client = self.request
        sockets = (client, remote)
        transferred = 0
        while True:
            readable, _, _ = select.select(sockets, (), (), 60)
            if not readable:
                return
            for source in readable:
                data = source.recv(65536)
                if not data:
                    return
                transferred += len(data)
                if transferred > MAX_BROWSER_CONNECTION_BYTES or not self.server.reserve_bytes(len(data)):
                    return
                (remote if source is client else client).sendall(data)

    def handle(self):
        client = self.request
        remote = None
        reader = None
        if not self.server.semaphore.acquire(blocking=False):
            client.sendall(b"HTTP/1.1 503 Busy\r\nConnection: close\r\n\r\n")
            return
        try:
            if self.server.bytes_transferred >= MAX_BROWSER_TOTAL_BYTES:
                client.sendall(b"HTTP/1.1 509 Bandwidth Limit Exceeded\r\nConnection: close\r\n\r\n")
                return
            client.settimeout(30)
            reader = client.makefile("rb", buffering=0)
            first = reader.readline(8193)
            if len(first) > 8192 or not first.endswith(b"\r\n"):
                return
            method, target, version = first.decode("latin-1").strip().split(" ", 2)
            headers = []
            size = 0
            while True:
                line = reader.readline(8193)
                size += len(line)
                if size > 65536 or not line:
                    return
                if line == b"\r\n":
                    break
                headers.append(line)
            reader.close()
            reader = None
            if method.upper() == "CONNECT":
                authority = urlsplit("//" + target)
                if not authority.hostname:
                    raise ValueError("CONNECT host is missing")
                port = authority.port or 443
                host = f"[{authority.hostname}]" if ":" in authority.hostname else authority.hostname
                url = f"https://{host}:{port}/"
                remote = _open_pinned_connection(url, self.server.allowed_domains)
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                self._relay(remote)
                return
            parsed = urlsplit(target)
            if parsed.scheme != "http" or not parsed.hostname:
                return
            remote = _open_pinned_connection(target, self.server.allowed_domains)
            path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
            outgoing = [f"{method} {path} {version}\r\n".encode("latin-1")]
            outgoing.extend(line for line in headers if not line.lower().startswith((b"proxy-authorization:", b"proxy-connection:", b"connection:", b"host:")))
            outgoing.append(f"Host: {parsed.netloc}\r\n".encode("latin-1"))
            outgoing.append(b"Connection: close\r\n\r\n")
            remote.sendall(b"".join(outgoing))
            self._relay(remote)
        except (OSError, ValueError, RuntimeError, UnicodeError):
            try:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
        finally:
            if reader:
                reader.close()
            if remote:
                remote.close()
            self.server.semaphore.release()


class PinnedBrowserProxy:
    """Browser proxy that resolves each destination once and connects only to validated IPs."""
    def __init__(self, allowed_domains):
        handler = _BrowserProxyHandler
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler, bind_and_activate=True)
        self.server.daemon_threads = True
        self.server.allowed_domains = allowed_domains
        self.server.semaphore = threading.BoundedSemaphore(64)
        self.server.bytes_lock = threading.Lock()
        self.server.bytes_transferred = 0
        def reserve_bytes(amount):
            with self.server.bytes_lock:
                if self.server.bytes_transferred + amount > MAX_BROWSER_TOTAL_BYTES:
                    return False
                self.server.bytes_transferred += amount
                return True
        self.server.reserve_bytes = reserve_bytes
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def browser_automate(args: dict[str, Any]) -> dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("browser tool requires playwright; run: pip install playwright && playwright install chromium") from exc
    url = str(args.get("url", ""))
    validate_browser_actions(args.get("actions", []))
    allowed = {str(item).lower().rstrip(".") for item in args.get("allowed_domains", [])}
    validate_external_url(url, allowed)
    with sync_playwright() as playwright, PinnedBrowserProxy(allowed) as proxy:
        browser = _launch_browser(playwright, proxy.url)
        context = None
        try:
            context = browser.new_context(accept_downloads=True, service_workers="block")
            def route_guard(route):
                try:
                    validate_external_url(route.request.url, allowed)
                    route.continue_()
                except Exception:
                    route.abort()
            context.route("**/*", route_guard)
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            validate_external_url(page.url, allowed)
            downloads = []
            for action in args.get("actions", []):
                kind = action.get("type")
                if kind == "click":
                    page.locator(action["selector"]).click(timeout=10000)
                elif kind == "fill":
                    page.locator(action["selector"]).fill(action.get("value", ""))
                elif kind == "wait":
                    page.wait_for_timeout(action.get("milliseconds", 500))
                elif kind == "wait_for":
                    page.locator(action["selector"]).wait_for(state=action.get("state", "visible"), timeout=10000)
                elif kind == "screenshot":
                    target = Path(str(action.get("path", "screenshot.png")))
                    target.parent.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(target), full_page=action.get("full_page", False))
                elif kind == "download":
                    with page.expect_download(timeout=30000) as download_info:
                        page.locator(action["selector"]).click(timeout=10000)
                    download = download_info.value
                    temporary = Path(download.path())
                    if temporary.stat().st_size > MAX_DOCUMENT_BYTES:
                        raise RuntimeError("浏览器下载超过 100 MB 的保存上限")
                    target = Path(str(action.get("path", download.suggested_filename)))
                    target.parent.mkdir(parents=True, exist_ok=True)
                    download.save_as(str(target))
                    downloads.append(str(target))
            validate_external_url(page.url, allowed)
            result = {"title": page.title(), "url": page.url, "text": page.locator("body").evaluate("element => element.innerText.slice(0, 50000)"), "html": page.locator("html").evaluate("element => element.outerHTML.slice(0, 50000)"), "downloads": downloads}
            return {"ok": True, **result}
        finally:
            if context is not None:
                context.close()
            browser.close()


def pdf_extract(args: dict[str, Any]) -> dict[str, Any]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("PDF tool requires pypdf; run: pip install pypdf") from exc
    path = validate_document_path(args["path"])
    reader = PdfReader(str(path))
    start_page = int(args.get("start_page", 1))
    end_page = int(args.get("end_page", min(len(reader.pages), 20)))
    if start_page < 1 or end_page < start_page or end_page > len(reader.pages):
        raise ValueError("PDF 页码范围无效")
    if end_page - start_page >= 20:
        raise ValueError("每次最多提取 20 页，请分段读取更长的 PDF")
    start, end = start_page - 1, end_page
    texts = [(page.extract_text() or "") for page in reader.pages[start:end]]
    return {"ok": True, "pages": len(reader.pages), "selected_pages": [start + 1, end], "text": "\n".join(texts)[:100000]}


def docx_extract(args: dict[str, Any]) -> dict[str, Any]:
    try:
        from docx import Document
    except ImportError as exc:
        raise RuntimeError("Word tool requires python-docx; run: pip install python-docx") from exc
    document = Document(str(validate_document_path(args["path"])))
    tables = [[cell.text for cell in row.cells] for table in document.tables for row in table.rows]
    return {"ok": True, "text": "\n".join(paragraph.text for paragraph in document.paragraphs)[:100000], "tables": tables[:500]}


def xlsx_read(args: dict[str, Any]) -> dict[str, Any]:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise RuntimeError("Excel tool requires openpyxl; run: pip install openpyxl") from exc
    workbook = load_workbook(str(validate_document_path(args["path"])), read_only=True, data_only=True)
    sheets = {}
    requested = str(args.get("sheet", ""))
    try:
        for sheet in workbook.worksheets:
            if requested and sheet.title != requested:
                continue
            sheets[sheet.title] = [[value.isoformat() if hasattr(value, "isoformat") else value for value in row] for row in sheet.iter_rows(values_only=True, max_row=max(1, min(int(args.get("max_row", 200)), 1000)), max_col=max(1, min(int(args.get("max_col", 50)), 200)))]
        if requested and not sheets:
            raise ValueError(f"工作表不存在：{requested}")
    finally:
        workbook.close()
    return {"ok": True, "sheets": sheets}


def xlsx_write(args: dict[str, Any]) -> dict[str, Any]:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise RuntimeError("Excel tool requires openpyxl; run: pip install openpyxl") from exc
    path = Path(args["path"])
    values = args.get("values")
    start_row, start_col = max(1, int(args.get("row", 1))), max(1, int(args.get("column", 1)))
    validate_xlsx_values(values, start_row, start_col)
    path = validate_document_path(path)
    import io
    from persistence import atomic_write
    workbook = load_workbook(str(path))
    try:
        sheet = workbook[str(args.get("sheet", workbook.sheetnames[0]))]
        for row_index, row in enumerate(values, start_row):
            for col_index, value in enumerate(row, start_col):
                sheet.cell(row_index, col_index).value = value
        title = sheet.title
        buffer = io.BytesIO()
        workbook.save(buffer)
        if buffer.tell() > MAX_DOCUMENT_BYTES:
            raise RuntimeError("修改后的工作簿超过 100 MB 的处理上限")
        atomic_write(path, buffer.getvalue())
    finally:
        workbook.close()
    return {"ok": True, "path": str(path), "sheet": title, "rows": len(values)}
