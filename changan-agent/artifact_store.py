"""按文件类型留存每次工具写入的产物，不移动实际项目文件。"""

import hashlib
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from persistence import read_json, write_json


def _checksum(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def artifact_category(path):
    extension = Path(path).suffix.lower()
    if extension in (".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".c", ".cpp", ".h", ".sql", ".sh", ".ps1"):
        return "code"
    if extension in (".md", ".txt", ".rst"):
        return "reports"
    if extension in (".json", ".csv", ".tsv", ".yaml", ".yml", ".xml"):
        return "data"
    if extension in (".pdf", ".docx", ".xlsx", ".pptx", ".xls", ".doc"):
        return "documents"
    if extension in (".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif"):
        return "images"
    return "other"


def capture_artifact(project_dir, artifact_dir, path, tool):
    root, source, target_root = Path(project_dir).resolve(), Path(path).resolve(), Path(artifact_dir).resolve()
    if not source.is_relative_to(root) or not source.is_file():
        raise ValueError("归档文件必须属于当前项目且已经存在")
    relative = source.relative_to(root)
    checksum = _checksum(source)
    target = target_root / artifact_category(source) / relative.parent / (source.stem + "-" + checksum[:12] + source.suffix)
    if not target.resolve().is_relative_to(target_root):
        raise ValueError("产物路径越界")
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        fd, temporary = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
        os.close(fd)
        try:
            shutil.copyfile(source, temporary)
            if _checksum(temporary) != checksum:
                raise OSError("文件在归档过程中发生变化，未保存不一致版本")
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
    elif _checksum(target) != checksum:
        raise OSError("已有产物副本校验失败，未覆盖原文件")
    index_path = target_root / "index.json"
    entries = read_json(index_path, [])
    if not isinstance(entries, list) or not all(isinstance(item, dict) for item in entries):
        raise ValueError("产物索引结构无效，已保留原文件")
    entry = {"source": relative.as_posix(), "file": target.relative_to(target_root).as_posix(), "sha256": checksum,
             "bytes": target.stat().st_size, "tool": tool, "time": datetime.now().isoformat(timespec="seconds")}
    if not any(item.get("source") == entry["source"] and item.get("sha256") == checksum for item in entries):
        entries.append(entry)
        write_json(index_path, entries, backup=True)
    return {**entry, "artifact_path": str(target)}
