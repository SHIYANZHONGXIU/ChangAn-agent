"""实际办公文件读写回归，使用临时项目目录。"""

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from orchestrator import ToolExecutor


class DocumentToolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.tools = ToolExecutor(self.root, "full access")

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
