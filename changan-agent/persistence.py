"""工作区与聊天持久化；写入失败时不丢弃内存消息。"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path


def atomic_write(path: Path, content: str | bytes, *, backup: bool = False) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = content.encode("utf-8") if isinstance(content, str) else content
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        if backup and path.exists():
            atomic_write(path.with_suffix(path.suffix + ".bak"), path.read_bytes())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_json(path: Path, data, *, backup: bool = False) -> None:
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), backup=backup)


def read_json(path: Path, default=None):
    """损坏文件先保留，再从上一个有效版本恢复；不能恢复时明确报错。"""
    path = Path(path)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if not path.with_suffix(path.suffix + ".bak").exists():
            return copy.deepcopy(default)
    except (UnicodeError, json.JSONDecodeError):
        broken = path.with_name(path.name + ".corrupt-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
        shutil.copy2(path, broken)
    backup = path.with_suffix(path.suffix + ".bak")
    try:
        value = json.loads(backup.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"数据文件损坏且没有可恢复备份：{path}。原文件已保留，请勿覆盖。") from exc
    write_json(path, value)
    return value


def safe_name(value: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value)).strip(" .")[:48] or "新聊天"


class WorkspaceStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._saved_chats: dict[str, str] = {}

    def project_root(self, project):
        folder = str(project.get("folder", "")).strip()
        return Path(folder).expanduser().resolve() if folder else (self.path.parent / "projects" / project["id"]).resolve()

    def chat_dir(self, project, chat):
        root = self.project_root(project) / ".changan" / "chats"
        folder = chat.get("storage_folder") or Path(str(chat.get("storage_path", "")).replace("\\", "/")).name
        if not folder:
            stamp = re.sub(r"\D", "", str(chat.get("created", "")))[:14] or datetime.now().strftime("%Y%m%d%H%M%S")
            folder = f"{stamp}-{chat['id'][:10]}-{safe_name(chat.get('title', '新聊天'))}"
        candidate = (root / folder).resolve()
        if candidate.parent != root.resolve():
            raise ValueError("聊天目录必须位于项目专属的 chats 文件夹内")
        chat["storage_folder"] = folder
        chat["storage_path"] = candidate.relative_to(self.project_root(project)).as_posix()
        return candidate

    def load_chat(self, project, chat):
        path = self.chat_dir(project, chat) / "chat.json"
        if not path.exists() and not path.with_suffix(".json.bak").exists():
            root = path.parent.parent
            for candidate in root.glob("*/chat.json"):
                value = read_json(candidate)
                if isinstance(value, dict) and value.get("id") == chat["id"]:
                    chat["storage_folder"] = candidate.parent.name
                    path = candidate
                    break
        value = read_json(path)
        if value is not None:
            if not isinstance(value, dict) or value.get("id") != chat["id"] or not isinstance(value.get("messages"), list):
                raise RuntimeError(f"聊天文件内容不合法：{path}")
            if value["messages"] or not chat.get("messages"):
                chat["messages"] = value["messages"]
        elif not chat.get("messages") and chat.get("storage_path") and self.path.exists():
            # 空的新聊天允许首次保存；已有记录由消息数区分。
            if chat.get("message_count", 0):
                raise RuntimeError(f"聊天文件缺失：{path}")
        chat.setdefault("messages", [])

    def load(self):
        data = read_json(self.path, {"projects": []})
        if not isinstance(data, dict) or not isinstance(data.get("projects"), list):
            raise RuntimeError("工作区结构无效，已保留原文件")
        for project in data["projects"]:
            for chat in project.get("chats", []):
                self.load_chat(project, chat)
        return data

    def save_chat(self, project, chat):
        path = self.chat_dir(project, chat) / "chat.json"
        value = {key: value for key, value in chat.items() if key != "storage_path"}
        serialized = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
        if self._saved_chats.get(str(path)) != serialized:
            atomic_write(path, serialized, backup=True)
            self._saved_chats[str(path)] = serialized

    def save(self, projects, **state):
        index = []
        for project in projects:
            entry = {key: value for key, value in project.items() if key != "chats"}
            entry["chats"] = []
            for chat in project.get("chats", []):
                self.save_chat(project, chat)
                item = {key: value for key, value in chat.items() if key != "messages"}
                item["message_count"] = len(chat.get("messages", []))
                entry["chats"].append(item)
            index.append(entry)
        # 只有所有聊天成功保存后才更新索引，不能忽略磁盘错误再清空内联消息。
        write_json(self.path, {"version": 2, "projects": index, **state}, backup=True)

    def relocate(self, source, chat, target=None, title=None):
        target = target or source
        old_dir = self.chat_dir(source, chat)
        previous = dict(chat)
        if title is not None:
            chat["title"] = title
        stamp = re.sub(r"\D", "", str(chat.get("created", "")))[:14] or datetime.now().strftime("%Y%m%d%H%M%S")
        chat["storage_folder"] = f"{stamp}-{chat['id'][:10]}-{safe_name(chat.get('title', '新聊天'))}"
        chat.pop("storage_path", None)
        new_dir = self.chat_dir(target, chat)
        try:
            if old_dir != new_dir and old_dir.exists():
                if new_dir.exists():
                    raise FileExistsError(f"目标聊天目录已存在：{new_dir}")
                new_dir.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old_dir), str(new_dir))
            self.save_chat(target, chat)
            if old_dir != new_dir:
                for message in chat.get("messages", []):
                    meta = message.get("meta", {})
                    if meta.get("run_path"):
                        previous_path = Path(meta["run_path"])
                        if previous_path.is_relative_to(old_dir):
                            meta["run_path"] = str(new_dir / previous_path.relative_to(old_dir))
                self.save_chat(target, chat)
            # 新运行跟随聊天移动，并更新恢复关联。
            for run in (new_dir / "runs").glob("*/run.json"):
                meta = read_json(run, {})
                meta.update(project_id=target["id"], chat_id=chat["id"])
                write_json(run, meta)
        except Exception:
            if old_dir != new_dir and new_dir.exists() and not old_dir.exists():
                old_dir.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(new_dir), str(old_dir))
            chat.clear()
            chat.update(previous)
            raise
        return new_dir
