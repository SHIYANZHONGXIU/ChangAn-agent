"""不依赖模型的基础产物验证。"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


def validate_execution(project_dir: Path, execution: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    for action in execution.get("actions", []):
        result = action.get("result", {})
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
        suffix = path.suffix.lower()
        try:
            if suffix == ".py":
                # 校验语法不应在用户项目里生成 __pycache__。
                compile(path.read_bytes(), str(path), "exec")
            elif suffix == ".json":
                json.loads(path.read_text(encoding="utf-8"))
            elif suffix == ".csv":
                with path.open("r", encoding="utf-8", newline="") as handle:
                    list(csv.reader(handle))
        except Exception as exc:
            issues.append(f"产物校验失败 {relative}: {exc}")
    return issues
