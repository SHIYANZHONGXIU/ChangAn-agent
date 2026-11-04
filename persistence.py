"""工作区与聊天持久化；写入失败时不丢弃内存消息。"""

from __future__ import annotations

import copy
import json
import hashlib
import os
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path


def _read_run_summary(path, limit):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


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
    def __init__(self, path: Path, legacy_root: Path | None = None):
        self.path = Path(path)
        self.legacy_root = Path(legacy_root) if legacy_root is not None else self.path.parent
        self._saved_chats: dict[str, str] = {}
        self.forget_intents = []

    def project_data_dir(self, project):
        identifier = str(project["id"])
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identifier):
            raise ValueError("项目编号格式无效")
        root = self.path.parent.resolve()
        target = (root / "projects" / identifier).resolve()
        if not target.is_relative_to(root):
            raise ValueError("项目数据目录越界")
        return target

    def project_root(self, project):
        folder = str(project.get("folder") or "").strip()
        return Path(folder).expanduser().resolve() if folder else self.project_data_dir(project) / "workspace"

    def chat_dir(self, project, chat):
        root = self.project_data_dir(project) / "chats"
        folder = chat.get("storage_folder") or Path(str(chat.get("storage_path", "")).replace("\\", "/")).name
        if not folder:
            stamp = re.sub(r"\D", "", str(chat.get("created", "")))[:14] or datetime.now().strftime("%Y%m%d%H%M%S")
            folder = f"{stamp}-{chat['id'][:10]}-{safe_name(chat.get('title', '新聊天'))}"
        candidate = (root / folder).resolve()
        if candidate.parent != root.resolve():
            raise ValueError("聊天目录必须位于项目专属的 chats 文件夹内")
        chat["storage_folder"] = folder
        chat["storage_path"] = candidate.relative_to(self.path.parent.resolve()).as_posix()
        return candidate

    def project_run_history(self, project, limit=12):
        """List only runs linked to this project's chats; never scans project source files."""
        try:
            limit = max(1, min(50, int(limit)))
        except (TypeError, ValueError, OverflowError):
            raise ValueError("运行记录数量范围无效") from None
        project_data = self.project_data_dir(project).resolve()
        chats_root = project_data / "chats"
        items = []
        for chat in project.get("chats", []):
            try:
                chat_dir = self.chat_dir(project, chat)
            except (KeyError, ValueError):
                continue
            if chat_dir.is_symlink() or chat_dir.resolve().parent != chats_root.resolve():
                continue
            runs_dir = chat_dir / "runs"
            if runs_dir.is_symlink() or getattr(runs_dir, "is_junction", lambda: False)() or not runs_dir.is_dir():
                continue
            linked_runs = {str(message.get("meta", {}).get("run_id")) for message in chat.get("messages", [])
                           if isinstance(message, dict) and isinstance(message.get("meta"), dict)}
            for run_dir in runs_dir.iterdir():
                if run_dir.is_symlink() or not run_dir.is_dir() or run_dir.resolve().parent != runs_dir.resolve():
                    continue
                meta = _read_run_summary(run_dir / "run.json", 128_000)
                if not meta or meta.get("run_id", run_dir.name) != run_dir.name:
                    continue
                if meta.get("chat_id") not in (None, "", chat["id"]) or meta.get("project_id") not in (None, "", project["id"]):
                    continue
                if not meta.get("chat_id") and run_dir.name not in linked_runs:
                    continue
                result = _read_run_summary(run_dir / "result.json", 2_000_000)
                checkpoint = _read_run_summary(run_dir / "checkpoint.json", 2_000_000)
                saved_result = checkpoint.get("result") if isinstance(checkpoint.get("result"), dict) else {}
                items.append({
                    "run_id": run_dir.name,
                    "chat_id": chat["id"],
                    "chat_title": str(chat.get("title", "新聊天"))[:80],
                    "task": str(meta.get("task") or checkpoint.get("task") or "")[:4000],
                    "status": str(meta.get("status") or "unknown")[:40],
                    "stage": str(meta.get("last_stage") or checkpoint.get("stage") or "")[:120],
                    "next_round": checkpoint.get("next_round") if isinstance(checkpoint.get("next_round"), int) else None,
                    "final": str(result.get("final") or saved_result.get("final") or "")[:4000],
                    "updated": str(meta.get("updated") or meta.get("created") or "")[:80],
                    "run_path": str(run_dir),
                })
        items.sort(key=lambda item: (item["updated"], item["run_id"]), reverse=True)
        return items[:limit]

    @staticmethod
    def _copy_tree(source, target):
        """迁移复制逐文件校验；保留源目录，提交索引后才由清理步骤处理。"""
        if not source.exists() or source.resolve() == target.resolve():
            return
        if source.is_symlink() or getattr(source, "is_junction", lambda: False)():
            raise ValueError(f"迁移不跟随目录链接：{source.name}")
        target_root = target.resolve()
        if target.is_symlink() or getattr(target, "is_junction", lambda: False)():
            raise ValueError("迁移目标不能是目录链接")
        for file in source.rglob("*"):
            if file.is_symlink() or getattr(file, "is_junction", lambda: False)():
                raise ValueError(f"迁移不跟随文件链接：{file.name}")
            if not file.is_file() or "__pycache__" in file.relative_to(source).parts:
                continue
            destination = target / file.relative_to(source)
            if not destination.resolve().is_relative_to(target_root):
                raise ValueError("迁移目标越界")
            if destination.exists():
                if hashlib.sha256(destination.read_bytes()).digest() != hashlib.sha256(file.read_bytes()).digest():
                    raise RuntimeError(f"迁移目标与原文件不同，未覆盖：{destination.name}")
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(file, destination)
            if hashlib.sha256(destination.read_bytes()).digest() != hashlib.sha256(file.read_bytes()).digest():
                raise OSError(f"迁移校验失败：{file.name}")

    @staticmethod
    def _remap_paths(value, mappings):
        if isinstance(value, dict):
            return {key: WorkspaceStore._remap_paths(item, mappings) if isinstance(item, (dict, list)) else
                    WorkspaceStore._mapped_path(item, mappings) if key in ("run_path", "checkpoint", "project_dir", "artifact_path") else item
                    for key, item in value.items()}
        if isinstance(value, list):
            return [WorkspaceStore._remap_paths(item, mappings) for item in value]
        return value

    @staticmethod
    def _mapped_path(value, mappings):
        if isinstance(value, str) and Path(value).is_absolute():
            path = Path(value)
            for old, new in mappings:
                if path.is_relative_to(old):
                    return str(new / path.relative_to(old))
        return value

    def _migrate_project(self, project):
        old_root = Path(project["folder"]).expanduser().resolve() if project.get("folder") else (self.legacy_root / "projects" / project["id"]).resolve()
        target_root = self.project_data_dir(project)
        mappings = []
        indexed = set()
        for chat in project.get("chats", []):
            old_name = chat.get("storage_folder") or Path(str(chat.get("storage_path", "")).replace("\\", "/")).name
            if old_name and (Path(old_name).name != old_name or old_name in (".", "..")):
                raise ValueError("旧聊天目录格式无效")
            old = old_root / ".changan" / "chats" / old_name if old_name else None
            if old is not None:
                indexed.add(old.name)
                content = read_json(old / "chat.json")
                if content is not None:
                    if content.get("id") != chat["id"] or not isinstance(content.get("messages"), list):
                        raise RuntimeError("旧聊天内容与索引不匹配，已保留源文件")
                    chat["messages"] = content["messages"]
                elif chat.get("message_count", 0) and not chat.get("messages"):
                    raise RuntimeError("旧聊天文件缺失，已停止迁移")
            chat.pop("storage_folder", None)
            chat.pop("storage_path", None)
            new = self.chat_dir(project, chat)
            if old is not None and old.exists():
                self._copy_tree(old, new)
                backup = old / "chat.json.bak"
                if backup.exists():
                    saved = target_root / "history" / "migration-backups" / old.name / backup.name
                    self._copy_tree_file(backup, saved)
                    mappings.append((backup, saved))
                mappings.append((old, new))
        orphan_chats = {}
        for orphan in (old_root / ".changan" / "chats").glob("*"):
            if orphan.is_dir() and orphan.name not in indexed:
                new = target_root / "history" / "unindexed-chats" / orphan.name
                self._copy_tree(orphan, new)
                mappings.append((orphan, new))
                value = read_json(orphan / "chat.json", {})
                if value.get("id"):
                    orphan_chats[value["id"]] = new
        for run in (old_root / "runs").glob("*"):
            if not run.is_dir():
                continue
            meta = read_json(run / "run.json", {})
            chat = next((c for c in project["chats"] if c["id"] == meta.get("chat_id") or any(m.get("meta", {}).get("run_id") == run.name for m in c.get("messages", []))), None)
            owner = self.chat_dir(project, chat) if chat else orphan_chats.get(meta.get("chat_id"), target_root / "history")
            new = owner / "runs" / run.name
            self._copy_tree(run, new)
            mappings.append((run, new))
        from artifact_store import artifact_category
        for file in (old_root / "artifacts").rglob("*"):
            if file.is_file():
                new = target_root / "history" / "artifacts" / artifact_category(file) / file.relative_to(old_root / "artifacts")
                self._copy_tree_file(file, new)
                mappings.append((file, new))
        if not project.get("folder") and old_root.exists():
            workspace = self.project_root(project)
            for child in old_root.iterdir():
                if child.name in (".changan", "runs", "artifacts", "__pycache__", "workspace", "chats", "history", "project.json"):
                    continue
                if child.is_dir():
                    self._copy_tree(child, workspace / child.name)
                else:
                    self._copy_tree_file(child, workspace / child.name)
            mappings.append((old_root, workspace))
        return mappings

    @staticmethod
    def _copy_tree_file(source, target):
        if source.is_symlink() or target.is_symlink():
            raise ValueError("迁移不跟随文件链接")
        if target.exists() and target.read_bytes() != source.read_bytes():
            raise RuntimeError(f"迁移文件冲突，未覆盖：{target.name}")
        if not target.exists():
            atomic_write(target, source.read_bytes())

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

    def _migrate(self, data):
        # 先在暂存区完成所有改写，索引最后提交；中断后可从完整暂存副本重试。
        staging = self.path.parent / "migrations" / "v3-staging"
        if not staging.resolve().is_relative_to(self.path.parent.resolve()) or staging.is_symlink():
            raise ValueError("迁移暂存目录越界")
        staged = WorkspaceStore(staging / "workspace.json", self.legacy_root)
        if not staged.path.exists():
            if staging.exists():
                shutil.rmtree(staging)
            migrated = copy.deepcopy(data)
            mappings = []
            for project in migrated["projects"]:
                mappings.extend(staged._migrate_project(project))
            final_mappings = [(old, self.path.parent / new.relative_to(staging)) for old, new in mappings]
            for project in migrated["projects"]:
                for chat in project.get("chats", []):
                    staged.load_chat(project, chat)
                    chat["messages"] = self._remap_paths(chat["messages"], final_mappings)
                    for file in staged.chat_dir(project, chat).glob("chat.json*"):
                        if file.name in ("chat.json", "chat.json.bak"):
                            write_json(file, self._remap_paths(read_json(file), final_mappings))
                for file in staged.project_data_dir(project).rglob("*.json"):
                    if file.name in ("run.json", "checkpoint.json", "result.json") and "runs" in file.relative_to(staged.project_data_dir(project)).parts:
                        value = self._remap_paths(read_json(file), final_mappings)
                        if file.name == "run.json":
                            value["project_id"] = project["id"]
                            owner = next((c for c in project.get("chats", []) if file.is_relative_to(staged.chat_dir(project, c))), None)
                            if owner:
                                value["chat_id"] = owner["id"]
                        write_json(file, value)
                for file in (staged.project_data_dir(project) / "history" / "unindexed-chats").glob("*/chat.json"):
                    write_json(file, self._remap_paths(read_json(file), final_mappings))
            state = {k: v for k, v in migrated.items() if k not in ("projects", "version")}
            write_json(staging / "migration.json", {"version": 3, "time": datetime.now().isoformat(), "projects": len(migrated["projects"]), "source_retained": True, "paths": [{"from": str(old), "to": str(new)} for old, new in final_mappings]})
            staged.save(migrated["projects"], **state)
        migrated = read_json(staged.path)
        for project in migrated["projects"]:
            self._copy_tree(staged.project_data_dir(project), self.project_data_dir(project))
        atomic_write(self.path, staged.path.read_bytes(), backup=True)
        write_json(self.path.parent / "migrations" / "central-storage.json", read_json(staging / "migration.json"))
        shutil.rmtree(staging)
        return migrated

    def load(self):
        data = read_json(self.path, {"projects": []})
        if not isinstance(data, dict) or not isinstance(data.get("projects"), list):
            raise RuntimeError("工作区结构无效，已保留原文件")
        if data["projects"] and data.get("version", 0) < 3:
            data = self._migrate(data)
        self.forget_intents = []
        for project in data["projects"]:
            root = self.project_data_dir(project) / ".forget-intents"
            if not root.exists():
                continue
            if root.is_symlink():
                raise RuntimeError("聊天遗忘记录目录是链接，已保留原文件")
            forgotten = set()
            for path in root.glob("*.json"):
                if path.is_symlink():
                    raise RuntimeError("聊天遗忘记录不能是符号链接，已保留原文件")
                intent = read_json(path)
                if (not isinstance(intent, dict) or intent.get("project_id") != project["id"]
                        or intent.get("chat_id", "") + ".json" != path.name):
                    raise RuntimeError("聊天遗忘记录损坏，已保留原文件")
                self.forget_intents.append((project, intent))
                forgotten.add(intent["chat_id"])
            project["chats"] = [chat for chat in project.get("chats", []) if chat.get("id") not in forgotten]
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
            write_json(self.project_data_dir(project) / "project.json", {k: v for k, v in project.items() if k != "chats"})
            entry = {key: value for key, value in project.items() if key != "chats"}
            entry["chats"] = []
            for chat in project.get("chats", []):
                self.save_chat(project, chat)
                item = {key: value for key, value in chat.items() if key != "messages"}
                item["message_count"] = len(chat.get("messages", []))
                entry["chats"].append(item)
            index.append(entry)
        # 只有所有聊天成功保存后才更新索引，不能忽略磁盘错误再清空内联消息。
        write_json(self.path, {"version": 3, "projects": index, **state}, backup=True)

    def relocate(self, source, chat, target=None, title=None):
        target = target or source
        old_dir = self.chat_dir(source, chat)
        previous = copy.deepcopy(chat)
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
            if old_dir != new_dir:
                chat["messages"] = self._remap_paths(chat.get("messages", []), [(old_dir, new_dir)])
            self.save_chat(target, chat)
            backup = new_dir / "chat.json.bak"
            if old_dir != new_dir and backup.exists():
                write_json(backup, self._remap_paths(read_json(backup), [(old_dir, new_dir)]))
            # 新运行跟随聊天移动，并更新恢复关联。
            for run in (new_dir / "runs").rglob("*.json"):
                if run.name in ("run.json", "checkpoint.json", "result.json"):
                    meta = self._remap_paths(read_json(run, {}), [(old_dir, new_dir)])
                    if run.name == "run.json":
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

    def forget_chat_data(self, project, chat):
        """删除聊天专属目录及历史孤儿副本；只允许命中当前项目的数据根。"""
        if not isinstance(chat.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", chat["id"]):
            raise ValueError("聊天编号格式无效")
        project_root = self.project_data_dir(project).resolve()
        chat_dir = self.chat_dir(project, chat).resolve()
        if chat_dir.parent != (project_root / "chats").resolve() or chat_dir == project_root:
            raise ValueError("聊天目录边界无效")
        removed = []
        if chat_dir.exists():
            shutil.rmtree(chat_dir)
            removed.append(str(chat_dir))
        history = project_root / "history"
        if history.exists():
            storage_folder = chat.get("storage_folder") or Path(str(chat.get("storage_path", "")).replace("\\", "/")).name
            if storage_folder:
                legacy_backup = history / "migration-backups" / storage_folder
                if legacy_backup.exists() and legacy_backup.is_dir():
                    shutil.rmtree(legacy_backup)
                    removed.append(str(legacy_backup))
            for candidate in history.glob("unindexed-chats/*"):
                record = read_json(candidate / "chat.json", {}) if candidate.is_dir() else {}
                if record.get("id") == chat.get("id"):
                    shutil.rmtree(candidate)
                    removed.append(str(candidate))
            for run in history.rglob("run.json"):
                meta = read_json(run, {})
                if meta.get("chat_id") == chat.get("id"):
                    run_dir = run.parent
                    if run_dir.is_relative_to(history) and run_dir != history:
                        shutil.rmtree(run_dir)
                        removed.append(str(run_dir))
        return removed

    def begin_forget_chat(self, project, chat):
        if not isinstance(chat.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", chat["id"]):
            raise ValueError("聊天编号格式无效")
        intent = {"project_id": project["id"], "chat_id": chat["id"],
                  "storage_folder": chat.get("storage_folder", "")}
        root = self.project_data_dir(project).resolve() / ".forget-intents"
        path = root / (chat["id"] + ".json")
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("聊天遗忘记录目录越界")
        write_json(path, intent)
        return path

    def clear_forget_intent(self, project, chat_id):
        if not isinstance(chat_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", chat_id):
            raise ValueError("聊天编号格式无效")
        path = self.project_data_dir(project) / ".forget-intents" / (chat_id + ".json")
        if path.is_symlink():
            raise ValueError("聊天遗忘记录不能是符号链接")
        path.unlink(missing_ok=True)
        try:
            path.parent.rmdir()
        except OSError:
            pass
