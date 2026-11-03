"""本地启动保护与脱敏诊断，不保存密钥或完整模型输入。"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
import threading
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path


def resolve_app_paths(script_path, executable_path, frozen, bundle_path=None, environ=None):
    environ = os.environ if environ is None else environ
    app_dir = Path(executable_path if frozen else script_path).resolve().parent
    resources = Path(bundle_path).resolve() if frozen and bundle_path else app_dir
    override = str(environ.get("CHANGAN_DATA_DIR", "")).strip()
    if override:
        data_dir = Path(override).resolve()
    elif frozen:
        project_data = next((candidate / "data" for candidate in (app_dir, *list(app_dir.parents)[:7])
                             if (candidate / "desktop_app.py").is_file()
                             and (candidate / "data" / "workspace.json").is_file()), None)
        if project_data is not None:
            data_dir = project_data.resolve()
        else:
            local_appdata = Path(environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
            data_dir = (local_appdata / "ChangAn" / "data").resolve()
    else:
        data_dir = (app_dir / "data").resolve()
    return app_dir, resources, data_dir


def prepare_data_layout(app_dir: Path, data_dir: Path):
    """旧根目录配置移入独立数据目录；失败可重复执行，不覆盖冲突数据。"""
    app_dir, data_dir = Path(app_dir).resolve(), Path(data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    for name, relative in (
        ("agent_config.json", "config/agent_config.json"),
        ("agent_config.json.bak", "config/agent_config.json.bak"),
        ("workspace.json", "workspace.json"), ("workspace.json.bak", "workspace.json.bak"),
        ("memory.sqlite3", "memory/memory.sqlite3"),
        ("memory.sqlite3-wal", "memory/memory.sqlite3-wal"),
        ("memory.sqlite3-shm", "memory/memory.sqlite3-shm"),
        ("memory.sqlite3-journal", "memory/memory.sqlite3-journal"),
    ):
        source, target = app_dir / name, data_dir / relative
        if not source.exists() or source == target:
            continue
        if target.exists():
            if source.read_bytes() != target.read_bytes():
                raise RuntimeError(f"新旧数据同时存在且不同，已保留两份，请先检查：{source.name}")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
    logs = app_dir / "logs"
    if logs.is_dir() and logs != data_dir / "logs":
        target = data_dir / "logs"
        target.mkdir(parents=True, exist_ok=True)
        for source in logs.iterdir():
            if source.is_file() and not (target / source.name).exists():
                shutil.move(str(source), str(target / source.name))
        if not any(logs.iterdir()):
            logs.rmdir()


def redact(value: str) -> str:
    value = re.sub(r"sk-[A-Za-z0-9_-]+", "[已隐藏密钥]", str(value))
    return re.sub(r"(?i)(bearer\s+)[^\s\"']+", r"\1[已隐藏密钥]", value)


class RedactingFormatter(logging.Formatter):
    def format(self, record):
        return redact(super().format(record))


def configure_logging(data_dir: Path):
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_dir / "changan.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger().addHandler(handler)
    threading.excepthook = lambda args: logging.error("后台线程异常", exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


class InstanceLock:
    """同一个数据目录只允许一个主窗口，进程退出后操作系统自动释放锁。"""
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.handle = (directory / ".changan.lock").open("a+b")
        try:
            if os.fstat(self.handle.fileno()).st_size == 0:
                self.handle.write(b"0")
                self.handle.flush()
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise RuntimeError("长安已经在运行，请切换到已有窗口。") from None

    def close(self):
        self.handle.close()


class RunJournal:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.delta_parts = []

    def append(self, kind, value):
        with self.lock:
            if kind == "delta":
                self.delta_parts.append(value)
                if sum(len(part) for part in self.delta_parts) < 4096:
                    return
                value = "".join(self.delta_parts)
                self.delta_parts.clear()
            elif self.delta_parts:
                pending = "".join(self.delta_parts)
                self.delta_parts.clear()
                self._write("delta", pending)
            self._write(kind, value)

    def _write(self, kind, value):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"time": datetime.now().isoformat(timespec="seconds"), "kind": kind, "value": redact(value)}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            handle.flush()
