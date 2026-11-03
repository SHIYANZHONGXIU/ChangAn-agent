"""不依赖模型的基础产物验证。"""

from __future__ import annotations

import csv
import json
import zipfile
from pathlib import Path
from typing import Any


MAX_VALIDATION_BYTES = 100 * 1024 * 1024
MAX_OFFICE_EXPANDED_BYTES = 500 * 1024 * 1024


def validate_execution(project_dir: Path, execution: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    for action in execution.get("actions", []):
        result = action.get("result", {})
        if result.get("artifact_error"):
            issues.append(f"工具已执行，但产物归档失败：{result['artifact_error']}")
        if result.get("ok") is False:
            issues.append(f"工具 {action.get('tool')} 执行失败: {result.get('error', result.get('stderr', 'unknown error'))}")
            continue
        if action.get("tool") not in ("write_file", "xlsx_write"):
            continue
        relative = result.get("path")
        if not relative:
            continue
        path = (project_dir / relative).resolve()
        if not path.exists() or project_dir.resolve() not in path.parents:
            issues.append(f"产物不存在: {relative}")
            continue
        if path.stat().st_size > MAX_VALIDATION_BYTES:
            issues.append(f"产物超过 100 MB 校验上限，未解析：{relative}")
            continue
        suffix = path.suffix.lower()
        try:
            if suffix == ".py":
                # 校验语法不应在用户项目里生成 __pycache__。
                compile(path.read_bytes(), str(path), "exec")
            elif suffix == ".json":
                json.loads(path.read_text(encoding="utf-8"))
            elif suffix == ".jsonl":
                for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                    if line.strip():
                        try:
                            json.loads(line)
                        except json.JSONDecodeError as exc:
                            raise ValueError(f"第 {number} 行不是有效 JSON") from exc
            elif suffix == ".csv":
                with path.open("r", encoding="utf-8", newline="") as handle:
                    list(csv.reader(handle, strict=True))
            elif suffix in (".xlsx", ".docx"):
                with zipfile.ZipFile(path) as archive:
                    entries = archive.infolist()
                    if len(entries) > 20_000 or sum(item.file_size for item in entries) > MAX_OFFICE_EXPANDED_BYTES or any(
                        item.file_size > 250 * 1024 * 1024 or item.file_size > 10_000_000 and item.compress_size == 0
                        or item.compress_size and item.file_size / item.compress_size > 1000
                        for item in entries
                    ):
                        raise ValueError("Office 文档展开体积或压缩比例超过安全限制")
                    if archive.testzip() is not None:
                        raise ValueError("Office 文件压缩包校验失败")
                if suffix == ".xlsx":
                    from openpyxl import load_workbook
                    workbook = load_workbook(path, read_only=True, data_only=False)
                    try:
                        if not workbook.sheetnames:
                            raise ValueError("工作簿没有工作表")
                    finally:
                        workbook.close()
                else:
                    from docx import Document
                    Document(path)
            elif suffix == ".pdf":
                from pypdf import PdfReader
                reader = PdfReader(path, strict=True)
                if not reader.pages:
                    raise ValueError("PDF 没有页面")
        except Exception as exc:
            issues.append(f"产物校验失败 {relative}: {exc}")
    return issues
