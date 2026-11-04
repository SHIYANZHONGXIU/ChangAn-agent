"""并发运行独立 Agent：项目快照、阶段恢复、差异预览和冲突保护合并。"""

from __future__ import annotations

import difflib
import os
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from app_runtime import RunJournal, redact
from network_safety import normalize_allowed_domains
from orchestrator import Agent, Provider, TaskStopped, ToolExecutor
from persistence import atomic_write, read_json, write_json
from workspace_panel import SKIP_DIRECTORIES, _is_link, is_private_name


SKIP = SKIP_DIRECTORIES | {".venv-workbench", "data", "outputs", "artifacts", "runs", "dist", "build"}


def project_files(root, limit=5000, max_bytes=100_000_000):
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError("项目目录不存在")
    found, size = {}, 0
    for directory, children, names in os.walk(root):
        children[:] = [n for n in children if n not in SKIP and not is_private_name(n) and not _is_link(Path(directory) / n)]
        for name in names:
            path = Path(directory) / name
            if is_private_name(name) or _is_link(path) or name == ".changan.lock":
                continue
            size += path.stat().st_size
            if len(found) >= limit or size > max_bytes:
                raise ValueError("快照上限为5000个文件/100MB，请缩小项目目录")
            found[path.relative_to(root).as_posix()] = path
    return found


def snapshot(source, destination, should_stop=lambda: False):
    files, baseline = project_files(source), {}
    for relative, path in files.items():
        if should_stop():
            raise TaskStopped("创建快照已取消")
        target = Path(destination) / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        before = ToolExecutor._hash(path)
        shutil.copy2(path, target)
        if before != ToolExecutor._hash(target) or before != ToolExecutor._hash(path):
            raise RuntimeError(f"创建快照时文件发生变化：{relative}，请重试")
        baseline[relative] = before
    Path(destination).mkdir(parents=True, exist_ok=True)
    return baseline


class ParallelAgents:
    def __init__(self, data_dir, approval=None, notify=None, max_workers=3, runner=None, memory_store=None, project_id=""):
        self.directory = Path(data_dir).resolve() / "parallel"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.approval = approval or (lambda *_: False)
        self.notify = notify or (lambda *_: None)
        self.runner = runner or self._run_agent
        self.memory_store, self.project_id = memory_store, project_id
        self.pool = ThreadPoolExecutor(max_workers=max(1, min(4, int(max_workers))), thread_name_prefix="changan-agent")
        self.lock = threading.RLock()
        self.tasks, self.stops, self.futures = {}, {}, {}
        self.closed = False
        for path in self.directory.glob("*/task.json"):
            row = read_json(path, {})
            if row.get("id") != path.parent.name or not row.get("source"):
                continue
            if row.get("status") in {"running", "queued", "preparing"}:
                row.update(status="interrupted", stage="上次退出时中断，可恢复")
                write_json(path, row, backup=True)
            self.tasks[row["id"]] = row

    def _update(self, task_id, **changes):
        with self.lock:
            row = self.tasks[task_id]
            row.update(changes, updated=datetime.now().isoformat(timespec="seconds"))
            write_json(self.directory / task_id / "task.json", row, backup=True)
        self.notify(task_id, "state", changes)

    def rows(self):
        with self.lock:
            return [dict(value) for value in self.tasks.values()]

    def submit(self, source, task, settings, project_id=None):
        if self.closed:
            raise RuntimeError("并行工作台已关闭")
        if not isinstance(task, str) or not task.strip() or len(task) > 40000:
            raise ValueError("请输入任务，长度不超过40000字符")
        task_id = uuid.uuid4().hex
        self.tasks[task_id] = {"id": task_id, "source": str(Path(source).resolve()), "project_id": project_id or self.project_id, "task": task.strip(), "created": datetime.now().isoformat(timespec="seconds"), "status": "queued", "stage": "等待并行工作槽"}
        self._update(task_id)
        self._schedule(task_id, settings)
        return task_id

    def _schedule(self, task_id, settings):
        self.stops[task_id] = threading.Event()
        self.futures[task_id] = self.pool.submit(self._worker, task_id, dict(settings))

    def resume(self, task_id, settings):
        row = self.tasks[task_id]
        checkpoint = read_json(self.directory / task_id / "checkpoint.json", {})
        retry_memory = row["status"] == "completed" and checkpoint.get("result", {}).get("status") == "passed" and not checkpoint.get("memory_extraction_done")
        if self.closed or row["status"] not in {"interrupted", "stopped", "error", "failed"} and not retry_memory:
            raise ValueError("仅中断、停止、异常或未通过任务可以恢复")
        if task_id in self.futures and not self.futures[task_id].done():
            raise RuntimeError("任务仍在停止中")
        self._update(task_id, status="queued", stage="等待恢复")
        self._schedule(task_id, settings)

    def _worker(self, task_id, settings):
        root = self.directory / task_id
        stop = self.stops[task_id]
        journal = RunJournal(root / "events.jsonl")
        started = time.monotonic()
        def emit(kind, value):
            journal.append(kind, redact(str(value)))
            self.notify(task_id, kind, value)
            if kind == "stage":
                self._update(task_id, stage=str(value))
        try:
            if stop.is_set():
                raise TaskStopped("任务已停止")
            self._update(task_id, status="preparing", stage="创建独立项目快照")
            if not (root / "baseline.json").exists():
                # 不复用半成品快照，防止将上次复制中断的文件误算为新文件。
                workspace = root / "workspace"
                if workspace.exists():
                    shutil.rmtree(workspace)
                baseline = snapshot(self.tasks[task_id]["source"], workspace, stop.is_set)
                write_json(root / "baseline.json", baseline)
            self._update(task_id, status="running", stage="正在运行独立 Agent")
            result = self.runner(self.tasks[task_id], root, settings, stop, emit)
            write_json(root / "result.json", result)
            atomic_write(root / "artifacts" / "answer.md", result.get("final", ""))
            self._update(task_id, status="completed" if result.get("status") == "passed" else "failed", stage="任务完成，等待查看差异", usage=result.get("usage", {}))
        except Exception as exc:
            self._update(task_id, status="stopped" if stop.is_set() or isinstance(exc, TaskStopped) else "error", stage=redact(str(exc))[:2000])
            emit("error", redact(str(exc)))
        finally:
            elapsed = self.tasks[task_id].get("elapsed", 0) + time.monotonic() - started
            self._update(task_id, elapsed=round(elapsed, 2))

    def _run_agent(self, row, root, settings, stop, emit):
        from mcp_integration import MCPRegistry, MCPTools
        providers = {}
        tools = MCPTools(MCPRegistry(self.directory.parent / "config" / "mcp_servers.json"), root / "workspace", stop.is_set)
        try:
            for prefix, name in (("gpt", "GPT"), ("deepseek", "DeepSeek"), ("claude", "Claude")):
                if prefix == "claude" and not settings.get("claude_key"):
                    continue
                provider = Provider(name, settings[prefix + "_base_url"], settings[prefix + "_key"], settings[prefix + "_model"], settings[prefix + "_reasoning"], retries=3)
                provider.configure_budget(float(settings.get(prefix + "_input_cost_per_1k") or 0), float(settings.get(prefix + "_output_cost_per_1k") or 0), float(settings.get("max_budget") or 0), settings.get("cost_currency", "未设置"), int(settings.get("max_output_tokens", 4096)))
                providers[prefix] = provider
            approval = lambda name, args: self.approval(row["id"], name, args, stop)
            executor = ToolExecutor(root / "workspace", settings["permission_mode"], approval=approval, allowed_domains=normalize_allowed_domains(settings.get("allowed_domains", "")), should_stop=stop.is_set, artifact_dir=root / "artifacts", mcp_tools=tools, internal_workspace=True)
            project_id = row.get("project_id", "")
            memory_lookup = lambda: self.memory_store.context("local", project_id, row["task"]) if self.memory_store and project_id and settings.get("memory_enabled") == "1" else []
            memory_extract = None
            if self.memory_store and project_id and settings.get("memory_auto_extract") == "1":
                memory_extract = lambda provider, stop_check: self.memory_store.extract(provider, user_id="local", project_id=project_id, source_chat=f"parallel:{row['id']}", source_message=row["id"], text=row["task"], stop_check=stop_check)
            agent = Agent(providers["gpt"], providers["deepseek"], int(settings.get("max_rounds", 3)), emit, stop.is_set, executor, root / "checkpoint.json", float(settings.get("max_budget") or 0), verifier=providers.get("claude"), memory_lookup=memory_lookup, memory_extract=memory_extract)
            return agent.run(row["task"])
        finally:
            tools.close()

    def stop(self, task_id):
        if task_id in self.stops:
            self.stops[task_id].set()
        if task_id in self.futures and self.futures[task_id].cancel():
            self._update(task_id, status="stopped", stage="排队已取消")

    def changes(self, task_id):
        row, root = self.tasks[task_id], self.directory / task_id
        if row["status"] in {"queued", "preparing", "running"}:
            raise RuntimeError("请先等待任务完成或停止，再查看稳定差异")
        baseline = read_json(root / "baseline.json", {})
        current = project_files(root / "workspace")
        output = []
        for relative in sorted(baseline.keys() | current.keys()):
            modified = ToolExecutor._hash(current[relative]) if relative in current else None
            if baseline.get(relative) != modified:
                source = ToolExecutor(Path(row["source"]), "full access")._safe_path(relative)
                conflict = ToolExecutor._hash(source) != baseline.get(relative)
                output.append({"path": relative, "change": "deleted" if modified is None else "added" if relative not in baseline else "modified", "conflict": conflict, "before": baseline.get(relative), "after": modified})
        return output

    def diff(self, task_id, path=None):
        row, root = self.tasks[task_id], self.directory / task_id
        text = []
        changes = self.changes(task_id)
        if path is not None:
            changes = [change for change in changes if change["path"] == path]
            if not changes:
                return "这个任务没有该文件的变更。"
        for change in changes:
            relative = change["path"]
            text.append(f"\n{relative} · {change['change']}" + (" · 冲突，禁止合并" if change["conflict"] else ""))
            paths = (Path(row["source"]) / relative, root / "workspace" / relative)
            data = [p.read_bytes() if p.is_file() and p.stat().st_size <= 1_000_000 else b"" for p in paths]
            try:
                if any(b"\x00" in value for value in data):
                    raise UnicodeError()
                text.extend(difflib.unified_diff(data[0].decode("utf-8-sig").splitlines(True), data[1].decode("utf-8-sig").splitlines(True), fromfile="当前项目/" + relative, tofile="Agent快照/" + relative))
            except UnicodeError:
                text.append("二进制或大文件，仅显示文件变化信息")
        return "\n".join(text)[:500000] or "没有项目文件变更。"

    def compare(self, task_ids):
        if isinstance(task_ids, (str, bytes)):
            raise ValueError("请选择2到3个并行任务")
        task_ids = list(dict.fromkeys(task_ids))
        if not 2 <= len(task_ids) <= 3 or any(task_id not in self.tasks for task_id in task_ids):
            raise ValueError("请选择2到3个有效的并行任务")
        sources = {str(Path(self.tasks[task_id]["source"]).resolve()) for task_id in task_ids}
        if len(sources) != 1:
            raise ValueError("只能对照同一项目的并行任务")
        changes = {task_id: {item["path"]: item for item in self.changes(task_id)} for task_id in task_ids}
        paths = sorted(set().union(*(set(items) for items in changes.values())))
        return {
            "tasks": [{"id": task_id, "task": self.tasks[task_id]["task"], "status": self.tasks[task_id]["status"],
                       "stage": self.tasks[task_id].get("stage", ""), "change_count": len(changes[task_id])}
                      for task_id in task_ids],
            "files": [{"path": path, "changes": {task_id: changes[task_id].get(path) for task_id in task_ids}} for path in paths],
        }

    def merge(self, task_id):
        row, root = self.tasks[task_id], self.directory / task_id
        changes = self.changes(task_id)
        if any(change["conflict"] for change in changes):
            raise RuntimeError("原项目已有其他修改，未合并任何文件。请先解决差异。")
        executor = ToolExecutor(Path(row["source"]), "full access")
        applied = []
        # 合并日志逐文件落盘；崩溃后不盲目重复覆盖已变更的原项目。
        try:
            for change in changes:
                relative = change["path"]
                target = executor._safe_path(relative)
                if _is_link(target) or ToolExecutor._hash(target) != change["before"]:
                    raise RuntimeError(f"文件在合并期间发生变化：{relative}")
                backup = None
                if target.exists():
                    backup = Path(".agent-backups") / f"parallel-{task_id}-{uuid.uuid4().hex}.bak"
                    atomic_write(executor.project_dir / backup, target.read_bytes())
                if change["change"] == "deleted":
                    target.unlink(missing_ok=True)
                else:
                    source = root / "workspace" / relative
                    if _is_link(source) or ToolExecutor._hash(source) != change["after"]:
                        raise RuntimeError(f"快照发生变化：{relative}")
                    atomic_write(target, source.read_bytes())
                executor._record_version(relative, "parallel_merge", change["before"], change["after"], str(backup) if backup else None)
                applied.append(relative)
                write_json(root / "merge.json", {"applied": applied, "status": "merging"})
        except Exception:
            write_json(root / "merge.json", {"applied": applied, "status": "partial"})
            raise
        write_json(root / "merge.json", {"applied": applied, "status": "completed"})
        self._update(task_id, merged=applied, stage="已合并到原项目（保留备份）")
        return applied

    def close(self):
        self.closed = True
        for task_id in list(self.stops):
            self.stop(task_id)
        self.pool.shutdown(wait=False, cancel_futures=True)

    def active(self):
        return any(not future.done() for future in self.futures.values())
