"""本地启动保护与脱敏诊断，不保存密钥或完整模型输入。"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path


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
