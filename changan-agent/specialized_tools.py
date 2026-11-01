"""可选的浏览器和文档工具；依赖缺失时返回可操作的提示。"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse
import ipaddress
import socket


from network_safety import resolve_public_url


def validate_external_url(raw_url, allowed_domains=None):
    parsed, _ = resolve_public_url(raw_url, allowed_domains)
    return parsed.hostname


def browser_automate(args: dict[str, Any]) -> dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("browser tool requires playwright; run: pip install playwright && playwright install chromium") from exc
    url = str(args.get("url", ""))
    allowed = {str(item).lower().rstrip(".") for item in args.get("allowed_domains", [])}
    validate_external_url(url, allowed)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=playwright.chromium.executable_path)
        context = browser.new_context(accept_downloads=True, service_workers="block")
        def route_guard(route):
            try:
                validate_external_url(route.request.url, allowed)
                route.continue_()
            except Exception:
                route.abort()
        context.route("**/*", route_guard)
        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            validate_external_url(page.url, allowed)
            downloads = []
            for action in args.get("actions", []):
                kind = action.get("type")
                if kind == "click":
                    page.locator(action["selector"]).click(timeout=10000)
                elif kind == "fill":
                    page.locator(action["selector"]).fill(str(action.get("value", "")))
                elif kind == "wait":
                    page.wait_for_timeout(min(max(int(action.get("milliseconds", 500)), 0), 30000))
                elif kind == "wait_for":
                    page.locator(action["selector"]).wait_for(state=action.get("state", "visible"), timeout=10000)
                elif kind == "screenshot":
                    target = Path(str(action.get("path", "screenshot.png")))
                    target.parent.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(target), full_page=bool(action.get("full_page", False)))
                elif kind == "download":
                    with page.expect_download(timeout=30000) as download_info:
                        page.locator(action["selector"]).click(timeout=10000)
                    download = download_info.value
                    target = Path(str(action.get("path", download.suggested_filename)))
                    target.parent.mkdir(parents=True, exist_ok=True)
                    download.save_as(str(target))
                    downloads.append(str(target))
            validate_external_url(page.url, allowed)
            result = {"title": page.title(), "url": page.url, "text": page.locator("body").inner_text()[:50000], "html": page.content()[:50000], "downloads": downloads}
            return {"ok": True, **result}
        finally:
            context.close()
            browser.close()


def pdf_extract(args: dict[str, Any]) -> dict[str, Any]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("PDF tool requires pypdf; run: pip install pypdf") from exc
    reader = PdfReader(str(Path(args["path"])))
    start = max(1, int(args.get("start_page", 1))) - 1
    end = min(len(reader.pages), int(args.get("end_page", len(reader.pages))))
    texts = [(page.extract_text() or "") for page in reader.pages[start:end]]
    return {"ok": True, "pages": len(reader.pages), "selected_pages": [start + 1, end], "text": "\n".join(texts)[:100000]}


def docx_extract(args: dict[str, Any]) -> dict[str, Any]:
    try:
        from docx import Document
    except ImportError as exc:
        raise RuntimeError("Word tool requires python-docx; run: pip install python-docx") from exc
    document = Document(str(Path(args["path"])))
    tables = [[cell.text for cell in row.cells] for table in document.tables for row in table.rows]
    return {"ok": True, "text": "\n".join(paragraph.text for paragraph in document.paragraphs)[:100000], "tables": tables[:500]}


def xlsx_read(args: dict[str, Any]) -> dict[str, Any]:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise RuntimeError("Excel tool requires openpyxl; run: pip install openpyxl") from exc
    workbook = load_workbook(str(Path(args["path"])), read_only=True, data_only=True)
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
    if not isinstance(values, list) or not values or not all(isinstance(row, list) for row in values):
        raise RuntimeError("values must be a two-dimensional array")
    start_row, start_col = max(1, int(args.get("row", 1))), max(1, int(args.get("column", 1)))
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
        atomic_write(path, buffer.getvalue())
    finally:
        workbook.close()
    return {"ok": True, "path": str(path), "sheet": title, "rows": len(values)}
