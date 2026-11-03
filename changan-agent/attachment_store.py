"""用户主动添加的附件快照；路径随聊天移动，原始文件不改动。"""

import base64
import hashlib
import io
from datetime import datetime
from pathlib import Path

from persistence import atomic_write, safe_name

MAX_BYTES = 20 * 1024 * 1024
MAX_FILES = 8
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
TEXT_SUFFIXES = {".txt", ".md", ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".json", ".csv", ".tsv", ".yaml", ".yml", ".xml", ".log", ".sql", ".c", ".cpp", ".h", ".ps1", ".sh", ".tex", ".rst"}


def attachment_path(chat_dir, entry):
    root = Path(chat_dir).resolve()
    relative = Path(str(entry["path"]))
    target = (root / relative).resolve()
    if relative.is_absolute() or not relative.parts or relative.parts[0] != "attachments" or not target.is_relative_to(root / "attachments"):
        raise ValueError("附件路径不属于当前聊天")
    return target


def save_attachment(chat_dir, source=None, image=None):
    if image is not None:
        if image.width * image.height > 40_000_000:
            raise ValueError("图片过大，请缩小后再添加")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        raw = buffer.getvalue()
        name = "粘贴图片-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".png"
    else:
        source = Path(source)
        if not source.is_file():
            raise ValueError("请添加文件，暂不支持整个文件夹")
        if source.stat().st_size > MAX_BYTES:
            raise ValueError("单个附件上限为 20 MB")
        with source.open("rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
        name = source.name
    if len(raw) > MAX_BYTES:
        raise ValueError("单个附件上限为 20 MB")
    digest = hashlib.sha256(raw).hexdigest()
    filename = safe_name(Path(name).stem) + Path(name).suffix[:16]
    entry = {"id": digest[:20], "name": name, "path": "attachments/" + digest[:16] + "-" + filename, "bytes": len(raw), "sha256": digest, "kind": "image" if Path(name).suffix.lower() in IMAGE_SUFFIXES else "file", "created": datetime.now().isoformat(timespec="seconds")}
    target = attachment_path(chat_dir, entry)
    if not target.exists():
        atomic_write(target, raw)
    elif hashlib.sha256(target.read_bytes()).hexdigest() != digest:
        raise ValueError("已有附件内容不一致，未覆盖原文件")
    return entry


def prepare_attachments(chat_dir, entries):
    """解析发生在工作线程。图片仅交给 GPT，文字资料提供给三个阶段。"""
    from specialized_tools import docx_extract, pdf_extract, xlsx_read
    from PIL import Image, ImageOps

    if len(entries) > MAX_FILES:
        raise ValueError("每次最多发送 8 个附件")
    files, images = [], []
    remaining = 48000
    for entry in entries:
        path = attachment_path(chat_dir, entry)
        if path.stat().st_size > MAX_BYTES:
            raise ValueError("附件超过大小限制：" + entry["name"])
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            raise ValueError("附件已变更，请重新添加：" + entry["name"])
        suffix = Path(entry["name"]).suffix.lower()
        item = {"name": entry["name"], "bytes": len(raw)}
        if suffix in IMAGE_SUFFIXES:
            with Image.open(io.BytesIO(raw)) as original:
                if original.width * original.height > 40_000_000:
                    raise ValueError("图片过大，请缩小后再发送：" + entry["name"])
                image = ImageOps.exif_transpose(original)
                image.thumbnail((1600, 1600))
                buffer = io.BytesIO()
                image.convert("RGB").save(buffer, format="JPEG", quality=85)
            images.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")}})
            item["note"] = "图片已提供给 GPT 分析，后续执行依据目标清单中的图片描述。"
        else:
            try:
                if suffix == ".pdf":
                    text = pdf_extract({"path": str(path), "end_page": 20})["text"]
                elif suffix == ".docx":
                    result = docx_extract({"path": str(path)})
                    text = result["text"] + "\n" + str(result.get("tables", []))
                elif suffix == ".xlsx":
                    text = str(xlsx_read({"path": str(path), "max_row": 100, "max_col": 30})["sheets"])
                elif suffix in TEXT_SUFFIXES:
                    sample = raw[:200000]
                    try:
                        text = sample.decode("utf-16" if sample.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig")
                    except UnicodeError:
                        text = sample.decode("gb18030", errors="replace")
                else:
                    text = "此文件类型暂不支持提取内容；仅提供文件名和大小，不能声称已经阅读。"
                limit = min(16000, remaining)
                item["text"] = text[:limit]
                if len(text) > limit:
                    item["note"] = "仅提供部分内容，超出附件文本长度限制。"
                remaining -= len(item["text"])
            except (OSError, ValueError, RuntimeError, KeyError) as exc:
                item["error"] = "附件内容读取失败：" + str(exc)[:300]
        files.append(item)
    return {"files": files, "images": images}
