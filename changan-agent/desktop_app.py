"""Agent 工作台桌面客户端。需要 customtkinter：python -m pip install customtkinter"""

from __future__ import annotations

import json
import queue
import logging
import math
import copy
import os
import re
import difflib
import shutil
import threading
import tkinter as tk
import time
import traceback
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog

import customtkinter as ctk

from orchestrator import Agent, Provider, ToolExecutor, TaskStopped, ProviderRequestError
from secret_store import protect, unprotect
from persistence import WorkspaceStore, atomic_write, write_json, read_json
from app_runtime import RunJournal, InstanceLock, configure_logging, redact
from message_format import CODE_FENCES

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("CHANGAN_DATA_DIR", str(APP_DIR))).resolve()
ROOT = DATA_DIR / "agent_config.json"
WORKSPACE = DATA_DIR / "workspace.json"
DEFAULTS = {
    "base_url": "https://api.lumivia.cn/v1", "gpt_base_url": "https://api.lumivia.cn/v1", "deepseek_base_url": "https://api.lumivia.cn/v1", "gpt_key": "", "deepseek_key": "",
    "gpt_model": "gpt-5.6-sol", "deepseek_model": "deepseek-v4-pro",
    "gpt_reasoning": "medium", "deepseek_reasoning": "medium", "max_rounds": "3", "permission_mode": "ask for approve", "gpt_cost_per_1k": "0", "deepseek_cost_per_1k": "0",
    "gpt_input_cost_per_1k": "0", "gpt_output_cost_per_1k": "0", "deepseek_input_cost_per_1k": "0", "deepseek_output_cost_per_1k": "0", "claude_base_url": "https://api.lumivia.cn/v1", "claude_key": "", "claude_model": "claude-sonnet-4-5", "claude_reasoning": "medium", "claude_input_cost_per_1k": "0", "claude_output_cost_per_1k": "0", "max_budget": "0", "allowed_domains": "", "appearance": "Light",
}
THINKING = ["none", "low", "medium", "high", "xhigh", "max", "ultra"]


def timestamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("长安 · Agent 工作台")
        self.geometry("1420x900")
        self.minsize(1120, 700)
        if os.name == "nt":
            self.after(100, lambda: self.state("zoomed"))
        self.dark = False
        self.running = False
        self.pending_tasks = []
        self.run_context = None
        self.current_journal = None
        self.current_stage = "就绪"
        self._last_layout_width = 0
        self.history_limit = 60
        self.ui_queue = queue.SimpleQueue()
        self.store = WorkspaceStore(WORKSPACE)
        self.worker = None
        self.trace_refresh_job = None
        self.approval_dialog = None
        self._typing_states = {}
        self._run_lock = threading.RLock()
        self.stop_event = threading.Event()
        self.vars = {key: tk.StringVar(value=value) for key, value in DEFAULTS.items()}
        self.projects = []
        self.active_project_id = ""
        self.active_chat_id = ""
        self.trace_window = None
        self.trace_box = None
        self.live_stream = ""
        self.resume_checkpoint_path = None
        self.typing_job = None
        self.message_labels = {}
        self.chat_layout_job = None
        self.run_started_at = None
        self.live_message = None
        self.live_chat_id = None
        self.show_archived_chats = False
        self.show_archived_projects = False
        self.artifact_window = None
        self.closing = False
        self.current_run_id = None
        self.current_run_meta_path = None
        self.task_settings = None
        self.task_project_id = None
        self.task_chat_id = None
        self.runtime_job = None
        self.runtime_label = None
        self.gpt_models = ["gpt-5.6-sol", "gpt-5.6", "gpt-5.6-luna", "gpt-5.6-terra"]
        self.deepseek_models = ["deepseek-v4-pro", "deepseek-v4.1-flash"]
        self.claude_models = ["claude-sonnet-4-5", "claude-opus-4-1", "claude-haiku-4-5"]
        self._load_config()
        self.dark = self.vars["appearance"].get() == "Dark"
        ctk.set_appearance_mode("Dark" if self.dark else "Light")
        self._save_config()
        self._load_workspace()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._refresh_sidebar()
        self._show_chat()
        self._restore_draft()
        if self.unreadable_keys:
            self.status.configure(text="部分密钥需重新填写，原密文已保留")
        self.bind("<Configure>", self._on_window_resize, add="+")
        self.after(40, self._drain_ui_queue)
        if not os.environ.get("CHANGAN_OFFLINE"):
            self.after(400, self.refresh_models)

    def _load_config(self):
        data = read_json(ROOT, {})
        self.unreadable_keys = {}
        for key, variable in self.vars.items():
            if key in data:
                variable.set(str(data[key]))
        for prefix in ("gpt", "deepseek", "claude"):
            encrypted = data.get(prefix + "_key_enc")
            if encrypted:
                try:
                    self.vars[prefix + "_key"].set(unprotect(encrypted))
                except Exception:
                    self.vars[prefix + "_key"].set("")
                    self.unreadable_keys[prefix] = encrypted
                    logging.warning("%s 密钥无法在当前 Windows 账号解密；保留原密文", prefix)
        if data.get("allow_commands") is not None and "permission_mode" not in data:
            self.vars["permission_mode"].set("full access" if str(data["allow_commands"]).lower() == "true" else "ask for approve")

    def _save_config(self):
        data = {key: value.get() for key, value in self.vars.items() if not key.endswith("_key")}
        for prefix in ("gpt", "deepseek", "claude"):
            value = self.vars[prefix + "_key"].get().strip()
            data[prefix + "_key_enc"] = protect(value) if value else self.unreadable_keys.get(prefix, "")
            if value:
                self.unreadable_keys.pop(prefix, None)
        write_json(ROOT, data, backup=True)

    def _load_workspace(self):
        data = self.store.load()
        self.pending_tasks = data.get("pending_tasks", [])
        self.projects = data["projects"]
        if not self.projects:
            self.projects = [self._project_data("我的项目")]
        for project in self.projects:
            if not project.get("chats"):
                project["chats"] = [self._project_data("")["chats"][0]]
        available = [p for p in self.projects if not p.get("archived")] or self.projects
        project = next((p for p in available if p["id"] == data.get("active_project_id")), available[0])
        self.active_project_id = project["id"]
        chats = [c for c in project["chats"] if not c.get("archived")] or project["chats"]
        self.active_chat_id = next((c["id"] for c in chats if c["id"] == data.get("active_chat_id")), chats[0]["id"])
        for project in self.projects:
            for chat in project["chats"]:
                for message in chat.get("messages", []):
                    meta = message.get("meta", {})
                    if meta.get("status_label") in ("运行中", "已中断"):
                        result = None
                        run_path = Path(meta["run_path"]) if meta.get("run_path") else None
                        if run_path and run_path.resolve().is_relative_to(self._project_root(project)):
                            result = read_json(run_path / "result.json")
                        if isinstance(result, dict) and result.get("status") in ("passed", "failed"):
                            message["content"] = result.get("final", "任务已结束。")
                            meta.update(status_label="已完成" if result["status"] == "passed" else "未通过", outputs=["result.json", "answer.md"])
                        else:
                            meta["status_label"] = "已中断"
                            message["content"] = "上次任务已中断，可通过恢复任务继续。"
        self._save_workspace()

    def _save_workspace(self):
        self.store.save(self.projects, active_project_id=self.active_project_id, active_chat_id=self.active_chat_id,
                        pending_tasks=self.pending_tasks)

    def _chat_dir(self, project, chat):
        return self.store.chat_dir(project, chat)

    def _save_chat_file(self, project, chat):
        self.store.save_chat(project, chat)

    def _load_chat_file(self, project, chat):
        self.store.load_chat(project, chat)

    @staticmethod
    def _atomic_json_write(path, data):
        write_json(Path(path), data)

    def _write_run_meta(self, status=None, **extra):
        if self.run_context:
            self._write_context_meta(self.run_context, status, **extra)

    def _on_close(self):
        if self.closing:
            return
        self._remember_draft()
        self.closing = True
        self.stop_event.set()
        if self.approval_dialog is not None and self.approval_dialog.winfo_exists():
            self.approval_dialog.destroy()
        if self.running and self.run_context:
            self._write_context_meta(self.run_context, "interrupted", duration_seconds=self._elapsed(self.run_context), last_error="窗口已关闭")
            if self.live_message:
                self.live_message["meta"]["status_label"] = "已中断"
                self.live_message["content"] = "任务进度已保存，可在下次启动后恢复。"
        try:
            self._save_config()
            self._save_workspace()
        except Exception:
            logging.exception("关闭时保存失败")
            self.closing = False
            messagebox.showerror("保存失败", "数据尚未成功保存，请检查磁盘空间与文件夹权限后重试关闭。", parent=self)
            return
        for identifier in (self.runtime_job, self.typing_job, self.chat_layout_job, self.trace_refresh_job):
            if identifier:
                self.after_cancel(identifier)
        self.status.configure(text="正在安全退出…")
        self._wait_for_shutdown(time.monotonic() + 5)

    def _wait_for_shutdown(self, deadline):
        if self.worker is not None and self.worker.is_alive() and time.monotonic() < deadline:
            self.after(80, self._wait_for_shutdown, deadline)
            return
        for identifier in self.tk.call("after", "info"):
            self.tk.call("after", "cancel", identifier)
        self.destroy()

    def _ui_after(self, delay, callback, *args):
        # 工作线程只入队；所有 Tk 调用由主线程统一执行。
        if not self.closing:
            self.ui_queue.put((delay, callback, args))

    def _drain_ui_queue(self):
        if self.closing:
            return
        for _ in range(150):
            try:
                delay, callback, args = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            if delay:
                self.after(delay, callback, *args)
            else:
                try:
                    callback(*args)
                except Exception:
                    self.report_callback_exception(*__import__("sys").exc_info())
        self.after(40, self._drain_ui_queue)

    def report_callback_exception(self, exc_type, exc_value, exc_traceback):
        logging.error("桌面回调异常", exc_info=(exc_type, exc_value, exc_traceback))
        if not self.closing:
            self.status.configure(text="操作未完成，请查看诊断日志")

    def _project_data(self, name, folder=""):
        chat = {"id": uuid.uuid4().hex, "title": "新聊天", "created": timestamp(), "archived": False, "messages": []}
        return {"id": uuid.uuid4().hex, "name": name, "folder": folder, "created": timestamp(), "archived": False, "chats": [chat]}

    def _project_root(self, project=None):
        project = project or self._project()
        folder = str(project.get("folder", "")).strip()
        if folder:
            return Path(folder).expanduser().resolve()
        return self.store.project_root(project)

    def _project(self):
        return next((p for p in self.projects if p["id"] == self.active_project_id), self.projects[0])

    def _chat(self):
        project = self._project()
        return next((c for c in project["chats"] if c["id"] == self.active_chat_id), project["chats"][0])

    def _build(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.configure(fg_color=("#eef1f4", "#141518"))
        self.sidebar = ctk.CTkFrame(self, width=248, corner_radius=0, fg_color=("#f1f3f5", "#191a1e"))
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_propagate(False)
        self.sidebar.grid_rowconfigure(5, weight=0)
        ctk.CTkLabel(self.sidebar, text="长安", font=ctk.CTkFont(size=21, weight="bold"), text_color=("#17202b", "#f4f4f5")).grid(row=0, column=0, padx=22, pady=(25, 18), sticky="w")
        ctk.CTkButton(self.sidebar, text="＋  新建聊天", height=40, corner_radius=12, fg_color=("#273746", "#e7eef5"), hover_color=("#34495b", "#d4dee8"), text_color=("#ffffff", "#18222c"), command=self.new_chat).grid(row=1, column=0, padx=16, pady=(0, 20), sticky="ew")
        ctk.CTkLabel(self.sidebar, text="项目", anchor="w", text_color=("#6b7280", "#a1a1aa"), font=ctk.CTkFont(size=11, weight="bold")).grid(row=2, column=0, padx=22, sticky="ew")
        self.project_list = ctk.CTkScrollableFrame(self.sidebar, fg_color="transparent", corner_radius=0)
        self.project_list.grid(row=3, column=0, padx=10, pady=(5, 10), sticky="nsew")
        self.project_list.grid_columnconfigure(0, weight=1)
        ctk.CTkButton(self.sidebar, text="＋  新建项目", height=34, corner_radius=10, fg_color="transparent", border_width=1, border_color=("#d5dce3", "#383b42"), text_color=("#536273", "#cbd1d8"), hover_color=("#e7ebef", "#25272d"), command=self.new_project).grid(row=4, column=0, padx=16, pady=(0, 18), sticky="ew")
        chat_tools = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        chat_tools.grid(row=5, column=0, padx=16, pady=(0, 5), sticky="ew")
        ctk.CTkLabel(chat_tools, text="聊天记录", anchor="w", text_color=("#6b7280", "#a1a1aa"), font=ctk.CTkFont(size=11, weight="bold")).pack(fill="x", padx=6)
        self.chat_search = tk.StringVar(value="")
        ctk.CTkEntry(chat_tools, textvariable=self.chat_search, placeholder_text="搜索聊天名称或内容", height=30, corner_radius=10).pack(fill="x", pady=(5, 0))
        self.chat_search.trace_add("write", lambda *_: self._refresh_sidebar())
        self.chat_list = ctk.CTkScrollableFrame(self.sidebar, fg_color="transparent", corner_radius=0)
        self.chat_list.grid(row=6, column=0, padx=10, pady=(0, 10), sticky="nsew")
        self.sidebar.grid_rowconfigure(3, weight=1)
        self.sidebar.grid_rowconfigure(6, weight=2)
        ctk.CTkButton(self.sidebar, text="⌘  设置", height=34, corner_radius=8, fg_color="transparent", text_color=("#6b7280", "#a1a1aa"), hover_color=("#e5e7eb", "#27272a"), command=self.focus_settings).grid(row=7, column=0, padx=16, pady=(4, 16), sticky="ew")

        self.main = ctk.CTkFrame(self, corner_radius=0, fg_color=("#fbfcfd", "#202126"))
        self.main.grid(row=0, column=1, sticky="nsew")
        self.main.grid_columnconfigure(0, weight=1)
        self.main.grid_rowconfigure(1, weight=1)
        header = ctk.CTkFrame(self.main, fg_color="transparent", height=70)
        header.grid(row=0, column=0, padx=42, pady=(20, 8), sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        self.chat_title = ctk.CTkLabel(header, text="新聊天", font=ctk.CTkFont(size=20, weight="bold"), anchor="w", text_color=("#202a35", "#f0f2f4"))
        self.chat_title.grid(row=0, column=0, sticky="w")
        self.status = ctk.CTkLabel(header, text="就绪", text_color=("#6b7280", "#a1a1aa"), anchor="e")
        self.status.grid(row=0, column=1, padx=(10, 15), sticky="e")
        self.runtime_label = ctk.CTkLabel(header, text="", text_color=("#557d72", "#8bc7ad"), anchor="e", font=ctk.CTkFont(size=11, weight="bold"))
        self.runtime_label.grid(row=0, column=2, padx=(0, 12), sticky="e")
        self.trace_btn = ctk.CTkButton(header, text="运行详情", width=94, height=32, corner_radius=10, fg_color=("#edf2f4", "#30323a"), hover_color=("#e2e9ec", "#3f414b"), text_color=("#475b62", "#dbe3e7"), command=self.open_trace)
        self.trace_btn.grid(row=0, column=3, padx=(0, 12), sticky="e")
        self.theme_switch = ctk.CTkSwitch(header, text="夜间", width=60, command=self.toggle_theme, onvalue=1, offvalue=0)
        self.theme_switch.grid(row=0, column=4, sticky="e")
        if self.dark:
            self.theme_switch.select()
        self.messages = ctk.CTkScrollableFrame(self.main, fg_color="transparent", corner_radius=0)
        self.messages.grid(row=1, column=0, padx=34, pady=(0, 14), sticky="nsew")
        self.messages.grid_columnconfigure(0, weight=1)
        self.bottom_btn = ctk.CTkButton(self.main, text="↓  最新", width=82, height=30, corner_radius=15, border_width=1, border_color=("#e2e7e9", "#3a3d44"), fg_color=("#ffffff", "#30323a"), hover_color=("#f0f4f5", "#3f414b"), text_color=("#475b62", "#e4e4e7"), command=self._scroll_to_bottom)
        self.bottom_btn.place(relx=0.94, rely=0.78, anchor="se")
        composer = ctk.CTkFrame(self.main, fg_color=("#ffffff", "#292b31"), corner_radius=18, border_width=1, border_color=("#e1e6e9", "#3a3d44"))
        composer.grid(row=2, column=0, padx=34, pady=(0, 8), sticky="ew")
        composer.grid_columnconfigure(0, weight=1)
        self.prompt = ctk.CTkTextbox(composer, height=92, corner_radius=14, border_width=0, fg_color="transparent", font=ctk.CTkFont(size=14), wrap="word")
        self.prompt.grid(row=0, column=0, padx=17, pady=13, sticky="nsew")
        buttons = ctk.CTkFrame(composer, fg_color="transparent")
        buttons.grid(row=0, column=1, padx=12, pady=11, sticky="se")
        self.run_btn = ctk.CTkButton(buttons, text="发送  ⏎", width=100, height=36, corner_radius=10, command=self.start)
        self.run_btn.pack(pady=(0, 7))
        self.stop_btn = ctk.CTkButton(buttons, text="停止", width=100, height=32, corner_radius=10, fg_color="transparent", border_width=1, border_color=("#d1d5db", "#52525b"), text_color=("#4b5563", "#e4e4e7"), command=self.stop, state="disabled")
        self.stop_btn.pack()
        ctk.CTkLabel(self.main, text="Enter 发送 · Shift+Enter 换行", text_color=("#9ca3af", "#71717a"), font=ctk.CTkFont(size=10)).grid(row=3, column=0, padx=32, pady=(0, 12), sticky="w")
        self.prompt.bind("<Return>", self._enter_send)

        self.settings = ctk.CTkScrollableFrame(self, width=306, fg_color=("#f3f5f6", "#1a1b20"), corner_radius=0)
        self.settings.grid(row=0, column=2, padx=(0, 0), sticky="nsew")
        ctk.CTkLabel(self.settings, text="运行设置", font=ctk.CTkFont(size=16, weight="bold"), anchor="w").pack(fill="x", padx=18, pady=(26, 20))
        self._entry(self.settings, "GPT API 地址", "gpt_base_url")
        self.gpt_combo = self._select(self.settings, "GPT 模型", self.vars["gpt_model"], self.gpt_models)
        self.gpt_thinking = self._select(self.settings, "GPT 思考强度", self.vars["gpt_reasoning"], THINKING)
        self._entry(self.settings, "GPT 密钥", "gpt_key", secret=True)
        self._entry(self.settings, "DeepSeek API 地址", "deepseek_base_url")
        self.deepseek_combo = self._select(self.settings, "DeepSeek 模型", self.vars["deepseek_model"], self.deepseek_models)
        self.deepseek_thinking = self._select(self.settings, "DeepSeek 思考强度", self.vars["deepseek_reasoning"], THINKING)
        self._entry(self.settings, "DeepSeek 密钥", "deepseek_key", secret=True)
        self._entry(self.settings, "Claude 审核 API 地址", "claude_base_url")
        self.claude_combo = self._select(self.settings, "Claude 审核模型", self.vars["claude_model"], self.claude_models)
        self.claude_thinking = self._select(self.settings, "Claude 审核思考强度", self.vars["claude_reasoning"], THINKING)
        self._entry(self.settings, "Claude 密钥（可选，留空用 GPT 审核）", "claude_key", secret=True)
        self._entry(self.settings, "GPT 每千 token 价格", "gpt_cost_per_1k")
        self._entry(self.settings, "DeepSeek 每千 token 价格", "deepseek_cost_per_1k")
        self._entry(self.settings, "GPT 输入每千 token 价格", "gpt_input_cost_per_1k")
        self._entry(self.settings, "GPT 输出每千 token 价格", "gpt_output_cost_per_1k")
        self._entry(self.settings, "DeepSeek 输入每千 token 价格", "deepseek_input_cost_per_1k")
        self._entry(self.settings, "DeepSeek 输出每千 token 价格", "deepseek_output_cost_per_1k")
        self._entry(self.settings, "Claude 输入每千 token 价格", "claude_input_cost_per_1k")
        self._entry(self.settings, "Claude 输出每千 token 价格", "claude_output_cost_per_1k")
        self._entry(self.settings, "本次任务预算上限（0 表示不限）", "max_budget")
        self._entry(self.settings, "允许访问的域名（逗号分隔，空白表示不限制公网域名）", "allowed_domains")
        self._select(self.settings, "最大核对轮次", self.vars["max_rounds"], [str(i) for i in range(1, 11)])
        self._select(self.settings, "命令权限", self.vars["permission_mode"], ["ask for approve", "full access"])
        ctk.CTkButton(self.settings, text="刷新模型列表", height=36, corner_radius=9, fg_color=("#e2e8f0", "#30323a"), hover_color=("#cbd5e1", "#3f414b"), text_color=("#334155", "#f4f4f5"), command=self.refresh_models).pack(fill="x", padx=18, pady=(18, 8))
        ctk.CTkButton(self.settings, text="打开项目产物", height=36, corner_radius=9, fg_color=("#e2e8f0", "#30323a"), hover_color=("#cbd5e1", "#3f414b"), text_color=("#334155", "#f4f4f5"), command=self.open_artifacts).pack(fill="x", padx=18, pady=(0, 8))
        ctk.CTkButton(self.settings, text="恢复中断任务", height=36, corner_radius=9, fg_color=("#e2e8f0", "#30323a"), hover_color=("#cbd5e1", "#3f414b"), text_color=("#334155", "#f4f4f5"), command=self.resume_checkpoint).pack(fill="x", padx=18, pady=(0, 8))
        ctk.CTkButton(self.settings, text="保存设置", height=36, corner_radius=9, command=self._save_config).pack(fill="x", padx=18, pady=(0, 28))

    def _entry(self, parent, label, key, secret=False):
        ctk.CTkLabel(parent, text=label, anchor="w", text_color=("#687780", "#a1a1aa"), font=ctk.CTkFont(size=11)).pack(fill="x", padx=20, pady=(9, 4))
        ctk.CTkEntry(parent, textvariable=self.vars[key], height=36, corner_radius=10, border_width=1, border_color=("#dfe5e8", "#393c43"), fg_color=("#ffffff", "#24262b"), show="•" if secret else "").pack(fill="x", padx=20, pady=(0, 7))

    def _select(self, parent, label, variable, values):
        ctk.CTkLabel(parent, text=label, anchor="w", text_color=("#687780", "#a1a1aa"), font=ctk.CTkFont(size=11)).pack(fill="x", padx=20, pady=(9, 4))
        widget = ctk.CTkOptionMenu(parent, variable=variable, values=values, height=36, corner_radius=10, dynamic_resizing=False, fg_color=("#e7edef", "#2b2d33"), button_color=("#dce5e7", "#373a41"), button_hover_color=("#d1dddf", "#44474f"), text_color=("#33464a", "#eceff1"))
        widget.pack(fill="x", padx=20, pady=(0, 7))
        return widget

    def _refresh_sidebar(self):
        for child in self.project_list.winfo_children(): child.destroy()
        active_projects = [project for project in self.projects if not project.get("archived", False)]
        archived_projects = [project for project in self.projects if project.get("archived", False)]
        for project in active_projects:
            active = project["id"] == self.active_project_id
            self._project_sidebar_row(project, active, archived=False)
        if archived_projects:
            ctk.CTkButton(self.project_list, text=("⌄  " if self.show_archived_projects else "›  ") + f"已归档 · {len(archived_projects)}", anchor="w", height=30, corner_radius=8, fg_color="transparent", text_color=("#64748b", "#a1a1aa"), hover_color=("#e5e7eb", "#27272a"), command=self.toggle_archived_projects).pack(fill="x", pady=(8, 2))
            if self.show_archived_projects:
                for project in archived_projects:
                    self._project_sidebar_row(project, project["id"] == self.active_project_id, archived=True)
        for child in self.chat_list.winfo_children(): child.destroy()
        active_chats = [chat for chat in self._project()["chats"] if not chat.get("archived", False)]
        archived_chats = [chat for chat in self._project()["chats"] if chat.get("archived", False)]
        query = self.chat_search.get().strip().casefold()
        if query:
            active_chats = [chat for chat in active_chats if query in chat.get("title", "").casefold() or any(query in str(m.get("content", "")).casefold() for m in chat.get("messages", []) if m.get("role") in ("user", "assistant"))]
        for chat in active_chats:
            active = chat["id"] == self.active_chat_id
            self._chat_sidebar_row(chat, active, archived=False)
        if archived_chats:
            ctk.CTkButton(self.chat_list, text=("⌄  " if self.show_archived_chats else "›  ") + f"已归档 · {len(archived_chats)}", anchor="w", height=30, corner_radius=8, fg_color="transparent", text_color=("#64748b", "#a1a1aa"), hover_color=("#e5e7eb", "#27272a"), command=self.toggle_archived_chats).pack(fill="x", pady=(8, 2))
            if self.show_archived_chats:
                for chat in archived_chats:
                    self._chat_sidebar_row(chat, chat["id"] == self.active_chat_id, archived=True)

    def _project_sidebar_row(self, project, active, archived=False):
        row = ctk.CTkFrame(self.project_list, fg_color=("#e2e8f0", "#30323a") if active else "transparent", corner_radius=8)
        row.pack(fill="x", pady=2)
        row.grid_columnconfigure(0, weight=1)
        name = ("●  " if active else "○  ") + project.get("name", "项目")
        ctk.CTkButton(row, text=name, anchor="w", height=33, corner_radius=8, fg_color="transparent", text_color=("#1f2937", "#f4f4f5"), hover_color=("#e5e7eb", "#27272a"), command=lambda p=project: self.select_project(p)).grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(row, text="⋯", width=28, height=26, corner_radius=7, fg_color="transparent", hover_color=("#dbeafe", "#3f414b"), text_color=("#64748b", "#a1a1aa"), command=lambda p=project, a=archived: self.open_project_menu(p, a)).grid(row=0, column=1, padx=(0, 4))

    def toggle_archived_projects(self):
        self.show_archived_projects = not self.show_archived_projects
        self._refresh_sidebar()

    def open_project_menu(self, project, archived=False):
        menu = tk.Menu(self, tearoff=False)
        menu.add_command(label="恢复归档" if archived else "归档项目", command=lambda: self.archive_project(project, not archived))
        menu.add_command(label="重命名", command=lambda: self.rename_project(project))
        menu.add_separator()
        menu.add_command(label="从列表移除项目", command=lambda: self.remove_project(project))
        try:
            menu.tk_popup(self.winfo_pointerx(), self.winfo_pointery())
        finally:
            menu.grab_release()

    def archive_project(self, project, archived=True):
        if archived and project["id"] == self.active_project_id:
            candidates = [item for item in self.projects if item["id"] != project["id"] and not item.get("archived", False)]
            if not candidates:
                messagebox.showinfo("无法归档", "至少需要保留一个未归档项目。", parent=self)
                return
            self.active_project_id = candidates[0]["id"]
            chats = [chat for chat in candidates[0]["chats"] if not chat.get("archived", False)] or candidates[0]["chats"]
            self.active_chat_id = chats[0]["id"]
        project["archived"] = archived
        self._save_workspace(); self._refresh_sidebar(); self._show_chat()

    def rename_project(self, project):
        name = simpledialog.askstring("重命名项目", "项目名称：", initialvalue=project.get("name", "项目"), parent=self)
        if name and name.strip():
            project["name"] = name.strip()[:80]
            self._save_workspace(); self._refresh_sidebar()

    def remove_project(self, project):
        if (self.running and self.run_context["project_id"] == project["id"]) or any(t["project_id"] == project["id"] for t in self.pending_tasks):
            messagebox.showinfo("任务未结束", "请停止或清空任务后再移除项目。", parent=self)
            return
        if len(self.projects) <= 1:
            messagebox.showinfo("无法移除", "至少需要保留一个项目。", parent=self)
            return
        folder = self._project_root(project)
        if not messagebox.askyesno("从列表移除项目", f"将从长安列表移除“{project.get('name', '项目')}”。\n本地文件夹不会被删除：\n{folder}", parent=self):
            return
        self.projects.remove(project)
        if project["id"] == self.active_project_id:
            candidates = [item for item in self.projects if not item.get("archived", False)] or self.projects
            self.active_project_id = candidates[0]["id"]
            chats = [chat for chat in candidates[0]["chats"] if not chat.get("archived", False)] or candidates[0]["chats"]
            self.active_chat_id = chats[0]["id"]
        self._save_workspace(); self._refresh_sidebar(); self._show_chat()

    def _chat_sidebar_row(self, chat, active, archived=False):
        row = ctk.CTkFrame(self.chat_list, fg_color=("#e2e8f0", "#30323a") if active else "transparent", corner_radius=8)
        row.pack(fill="x", pady=2)
        row.grid_columnconfigure(0, weight=1)
        created = str(chat.get("created", ""))[5:16] if chat.get("created") else ""
        title = ("▸  " if active else "   ") + chat.get("title", "新聊天") + (f"  · {created}" if created else "")
        ctk.CTkButton(row, text=title, anchor="w", height=31, corner_radius=8, fg_color="transparent", text_color=("#374151", "#e4e4e7"), hover_color=("#e5e7eb", "#27272a"), command=lambda c=chat: self.select_chat(c)).grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(row, text="⋯", width=28, height=26, corner_radius=7, fg_color="transparent", hover_color=("#dbeafe", "#3f414b"), text_color=("#64748b", "#a1a1aa"), command=lambda c=chat, a=archived: self.open_chat_menu(c, a)).grid(row=0, column=1, padx=(0, 4))

    def toggle_archived_chats(self):
        self.show_archived_chats = not self.show_archived_chats
        self._refresh_sidebar()

    def open_chat_menu(self, chat, archived=False):
        menu = tk.Menu(self, tearoff=False)
        menu.add_command(label="恢复归档" if archived else "归档聊天", command=lambda: self.archive_chat(chat, not archived))
        menu.add_command(label="重命名", command=lambda: self.rename_chat(chat))
        targets = [p for p in self.projects if p["id"] != self.active_project_id]
        if targets:
            move_menu = tk.Menu(menu, tearoff=False)
            for project in targets:
                move_menu.add_command(label=project.get("name", "项目"), command=lambda p=project, c=chat: self.move_chat(c, p))
            menu.add_cascade(label="移动到项目", menu=move_menu)
        menu.add_separator()
        menu.add_command(label="删除聊天", command=lambda: self.delete_chat(chat))
        try:
            menu.tk_popup(self.winfo_pointerx(), self.winfo_pointery())
        finally:
            menu.grab_release()

    def archive_chat(self, chat, archived=True):
        if archived and chat["id"] == self.active_chat_id:
            candidates = [item for item in self._project()["chats"] if item["id"] != chat["id"] and not item.get("archived", False)]
            if not candidates:
                messagebox.showinfo("无法归档", "项目至少需要保留一个未归档聊天。", parent=self)
                return
            self.active_chat_id = candidates[0]["id"]
        chat["archived"] = archived
        self._save_workspace(); self._refresh_sidebar(); self._show_chat()

    def _chat_busy(self, chat):
        return self.running and self.run_context and self.run_context["chat"]["id"] == chat["id"]

    def rename_chat(self, chat):
        if self._chat_busy(chat):
            messagebox.showinfo("任务运行中", "请停止任务后再重命名聊天。", parent=self)
            return
        name = simpledialog.askstring("重命名聊天", "聊天名称：", initialvalue=chat.get("title", "新聊天"), parent=self)
        if name and name.strip():
            self.store.relocate(self._project(), chat, title=name.strip()[:80])
            self._save_workspace(); self._refresh_sidebar(); self._show_chat()

    def move_chat(self, chat, target_project):
        source = self._project()
        if self._chat_busy(chat):
            messagebox.showinfo("任务运行中", "请停止任务后再移动聊天。", parent=self)
            return
        self.store.relocate(source, chat, target_project)
        source["chats"].remove(chat)
        if not source["chats"]:
            source["chats"] = [self._project_data("")["chats"][0]]
        target_project.setdefault("chats", []).insert(0, chat)
        for queued in self.pending_tasks:
            if queued["chat_id"] == chat["id"]:
                queued["project_id"] = target_project["id"]
        self.active_project_id, self.active_chat_id = target_project["id"], chat["id"]
        self._save_workspace(); self._refresh_sidebar(); self._show_chat()

    def delete_chat(self, chat):
        if self._chat_busy(chat) or any(t["chat_id"] == chat["id"] for t in self.pending_tasks):
            messagebox.showinfo("任务未结束", "请停止或清空任务后再删除聊天。", parent=self)
            return
        if len(self._project()["chats"]) <= 1:
            messagebox.showinfo("无法删除", "项目至少需要保留一个聊天。", parent=self)
            return
        if not messagebox.askyesno("删除聊天", f"确定删除“{chat.get('title', '新聊天')}”？\n删除后无法恢复。", parent=self):
            return
        chats = self._project()["chats"]
        chats.remove(chat)
        if chat["id"] == self.active_chat_id:
            available = [item for item in chats if not item.get("archived", False)] or chats
            self.active_chat_id = available[0]["id"]
        self._save_workspace(); self._refresh_sidebar(); self._show_chat()

    def select_project(self, project):
        self._remember_draft()
        self.history_limit = 60
        self.active_project_id = project["id"]
        available = [chat for chat in project["chats"] if not chat.get("archived", False)] or project["chats"]
        self.active_chat_id = available[0]["id"]
        self._refresh_sidebar(); self._show_chat()
        self._restore_draft()

    def select_chat(self, chat):
        self._remember_draft()
        self.history_limit = 60
        self.active_chat_id = chat["id"]
        self._refresh_sidebar(); self._show_chat()
        self._restore_draft()

    def _remember_draft(self):
        if hasattr(self, "prompt"):
            self._chat()["draft"] = self.prompt.get("1.0", "end-1c")
            self._save_workspace()

    def _restore_draft(self):
        self.prompt.delete("1.0", "end")
        self.prompt.insert("1.0", self._chat().get("draft", ""))

    def _show_chat(self, scroll=True):
        self.chat_title.configure(text=self._chat().get("title", "新聊天"))
        self.message_labels = {}
        canvas = self.messages._parent_canvas
        previous = canvas.yview()[0]
        for child in self.messages.winfo_children():
            child.destroy()
        visible = [m for m in self._chat().get("messages", []) if m.get("role") in ("user", "assistant")]
        if len(visible) > self.history_limit:
            ctk.CTkButton(self.messages, text="加载更早的消息", command=self._load_older_messages, fg_color="transparent", text_color=("#64748b", "#a1a1aa")).pack(pady=8)
        for message in visible[-self.history_limit:]:
            content, meta = self._display_message(message)
            self.message_labels[id(message)] = self._bubble(message["role"], content, {**message, "meta": meta})
        if scroll:
            self.after_idle(self._scroll_to_bottom)
        else:
            self.after_idle(lambda: canvas.yview_moveto(previous))
        self._schedule_trace_refresh()

    def _load_older_messages(self):
        self.history_limit += 60
        self._show_chat(scroll=False)

    def _on_window_resize(self, event):
        if event.widget is not self or event.width == self._last_layout_width:
            return
        self._last_layout_width = event.width
        if self.chat_layout_job is not None:
            self.after_cancel(self.chat_layout_job)
        self.chat_layout_job = self.after(250, self._relayout_chat)

    def _relayout_chat(self):
        self.chat_layout_job = None
        self._show_chat(scroll=False)

    @staticmethod
    def _display_message(message):
        content = str(message.get("content", ""))
        meta = dict(message.get("meta") or {})
        if message.get("role") == "assistant" and content.lstrip().startswith("{"):
            try:
                payload = json.loads(content)
                if isinstance(payload, dict) and "actions" in payload and payload.get("summary"):
                    return str(payload["summary"]).strip(), meta
            except json.JSONDecodeError:
                pass
        marker = "\n\n产物已保存："
        if message.get("role") == "assistant" and marker in content:
            visible, details = content.split(marker, 1)
            names = []
            for line_index, line in enumerate(details.splitlines()):
                if line_index == 0 and line.strip():
                    names.append(Path(line.strip()).name)
                if line.startswith("Markdown：") or line.startswith("Markdown:"):
                    names.append(Path(line.split("：", 1)[-1].split(":", 1)[-1].strip()).name)
                elif line.startswith("产物已保存："):
                    names.append(Path(line.split("：", 1)[-1].strip()).name)
                elif line.startswith("运行编号："):
                    meta.setdefault("run_id", line.split("：", 1)[-1].strip())
                elif line.startswith("用量："):
                    meta.setdefault("legacy_usage_note", line.removeprefix("用量：").strip())
            meta.setdefault("outputs", names[:2])
            meta.setdefault("status_label", "已完成")
            return visible.strip(), meta
        return content, meta

    def _bubble(self, role, content, message=None):
        outer = ctk.CTkFrame(self.messages, fg_color="transparent")
        outer.pack(fill="x", padx=28, pady=(10, 12))
        available_width = max(260, self.messages.winfo_width() - 84)
        card_width = min(980, available_width)
        if role == "user":
            bubble = ctk.CTkFrame(outer, corner_radius=17, fg_color=("#e5f2f4", "#24434a"))
            bubble.pack(anchor="e", padx=(110, 0))
            ctk.CTkLabel(bubble, text="你", anchor="w", text_color=("#42727a", "#a6d3d6"), font=ctk.CTkFont(size=10, weight="bold")).pack(fill="x", padx=16, pady=(10, 0))
        else:
            bubble = ctk.CTkFrame(outer, corner_radius=16, border_width=1, border_color=("#e8ecee", "#34373f"), fg_color=("#ffffff", "#25272d"))
            bubble.configure(width=card_width)
            bubble.pack(anchor="w", padx=(0, 28))
            header = ctk.CTkFrame(bubble, fg_color="transparent")
            header.pack(fill="x", padx=18, pady=(13, 0))
            ctk.CTkLabel(header, text="长安", anchor="w", text_color=("#557d72", "#8bc7ad"), font=ctk.CTkFont(size=11, weight="bold")).pack(side="left")
            meta = (message or {}).get("meta", {})
            status = meta.get("status_label", "")
            if status:
                ctk.CTkLabel(header, text=status, anchor="e", text_color=("#64748b", "#a1a1aa"), font=ctk.CTkFont(size=10)).pack(side="right")
        body = None
        blocks = list(CODE_FENCES.finditer(str(content or ""))) if role != "user" else []
        if blocks:
            cursor = 0
            for block in blocks:
                plain = str(content)[cursor:block.start()]
                if plain.strip():
                    ctk.CTkLabel(bubble, text=self._present_text(plain), anchor="w", justify="left", width=max(180, card_width - 48), wraplength=max(180, card_width - 48), font=ctk.CTkFont(size=14), padx=18, pady=10).pack(fill="x")
                language = block.group(2).strip().split()[0] if block.group(2).strip() else "代码"
                code = block.group(3)
                code_frame = ctk.CTkFrame(bubble, corner_radius=10, fg_color=("#f4f6f8", "#17191d"), border_width=1, border_color=("#e2e8ec", "#363a42"))
                code_frame.pack(fill="x", padx=14, pady=(6, 8))
                top = ctk.CTkFrame(code_frame, fg_color="transparent", height=28)
                top.pack(fill="x", padx=10, pady=(5, 0))
                ctk.CTkLabel(top, text=language.lower(), anchor="w", text_color=("#64748b", "#a1a1aa"), font=ctk.CTkFont(size=10, weight="bold")).pack(side="left")
                ctk.CTkButton(top, text="复制代码", width=72, height=24, corner_radius=7, command=lambda value=code: self._copy_code(value)).pack(side="right")
                lines = max(2, min(16, code.count("\n") + 1))
                code_box = ctk.CTkTextbox(code_frame, height=lines * 20 + 12, wrap="none", corner_radius=7, border_width=0, fg_color="transparent", font=ctk.CTkFont(family="Consolas", size=12))
                code_box.pack(fill="x", padx=8, pady=(2, 8))
                code_box.insert("1.0", code)
                code_box.configure(state="disabled")
                cursor = block.end()
            tail = str(content)[cursor:]
            if tail.strip():
                body = ctk.CTkLabel(bubble, text=self._present_text(tail), anchor="w", justify="left", width=max(180, card_width - 48), wraplength=max(180, card_width - 48), font=ctk.CTkFont(size=14), padx=18, pady=10)
                body.pack(fill="x")
            else:
                body = ctk.CTkLabel(bubble, text="", width=1, height=1)
        else:
            body = ctk.CTkLabel(bubble, text=self._present_text(content), anchor="w", justify="left", width=max(180, card_width - 48) if role != "user" else 0, wraplength=max(180, card_width - 48) if role != "user" else max(160, min(680, card_width - 110)), font=ctk.CTkFont(size=14), padx=18 if role != "user" else 16, pady=10)
            body.pack(fill="x")
        if role != "user":
            self._message_meta(bubble, message or {})
        return body

    def _copy_code(self, code):
        try:
            self.clipboard_clear()
            self.clipboard_append(code)
            self.update_idletasks()
            self.status.configure(text="已复制")
        except tk.TclError:
            pass

    @staticmethod
    def _present_text(content):
        """为界面显示增加可断行位置，不改变实际保存的消息内容。"""
        text = str(content or "")
        for marker in ("/", "\\", "_", ".", ":"):
            text = text.replace(marker, marker + "\u200b")
        return text

    def _message_meta(self, bubble, message):
        meta = message.get("meta", {})
        if not meta:
            return
        if meta.get("status_label") == "运行中":
            return
        divider = ctk.CTkFrame(bubble, height=1, fg_color=("#edf0f1", "#34373f"))
        divider.pack(fill="x", padx=18, pady=(4, 0))
        info = ctk.CTkFrame(bubble, fg_color="transparent")
        info.pack(fill="x", padx=18, pady=(9, 8))
        bits = []
        if meta.get("duration_seconds") is not None:
            seconds = float(meta["duration_seconds"])
            bits.append(f"思考/执行 {seconds:.1f} 秒")
        if meta.get("rounds"):
            bits.append(f"核对 {meta['rounds']} 轮")
        if meta.get("models"):
            bits.append(" + ".join(meta["models"]))
        if meta.get("usage"):
            bits.append(("约 " if meta.get("usage_estimated") else "") + f"{meta['usage']} tokens")
        elif meta.get("legacy_usage_note"):
            bits.append(meta["legacy_usage_note"])
        if meta.get("cost") is not None:
            bits.append(f"估算 {float(meta['cost']):.4f}")
        if meta.get("outputs"):
            bits.append(f"产物 {len(meta['outputs'])} 项")
        summary = "  ·  ".join(bits)
        if summary:
            ctk.CTkLabel(info, text=summary, wraplength=max(200, int(bubble.cget("width")) - 40), justify="left", anchor="w", text_color=("#77838c", "#a1a1aa"), font=ctk.CTkFont(size=10)).pack(side="left", fill="x", expand=True)
        files = meta.get("files", [])
        outputs = meta.get("outputs", [])
        if files:
            file_row = ctk.CTkFrame(bubble, fg_color="transparent")
            file_row.pack(fill="x", padx=18, pady=(0, 8))
            files_text = "；".join(files[:3]) + (f" 等 {len(files)} 个文件" if len(files) > 3 else "")
            ctk.CTkLabel(file_row, text="已编辑", anchor="w", text_color=("#475569", "#cbd5e1"), font=ctk.CTkFont(size=10, weight="bold")).pack(side="left")
            ctk.CTkLabel(file_row, text=files_text, anchor="w", justify="left", text_color=("#64748b", "#a1a1aa"), font=ctk.CTkFont(size=10)).pack(side="left", padx=(10, 0), fill="x", expand=True)
        if outputs:
            output_row = ctk.CTkFrame(bubble, fg_color="transparent")
            output_row.pack(fill="x", padx=18, pady=(0, 8))
            ctk.CTkLabel(output_row, text="结果文件", anchor="w", text_color=("#475569", "#cbd5e1"), font=ctk.CTkFont(size=10, weight="bold")).pack(side="left")
            ctk.CTkLabel(output_row, text="；".join(outputs), anchor="w", text_color=("#64748b", "#a1a1aa"), font=ctk.CTkFont(size=10)).pack(side="left", padx=(10, 0))
        actions = ctk.CTkFrame(bubble, fg_color="transparent")
        actions.pack(fill="x", padx=14, pady=(0, 9))
        ctk.CTkButton(actions, text="复制回答", width=76, height=26, corner_radius=9, fg_color="transparent", hover_color=("#f0f4f4", "#34373f"), text_color=("#64777b", "#cbd5e1"), command=lambda: self._copy_code(self._display_message(message)[0])).pack(side="left")
        ctk.CTkButton(actions, text="查看运行详情", width=104, height=26, corner_radius=9, fg_color="transparent", hover_color=("#f0f4f4", "#34373f"), text_color=("#64777b", "#cbd5e1"), command=lambda run=meta.get("run_id"): self.open_trace(run)).pack(side="left")
        if files or outputs:
            ctk.CTkButton(actions, text="打开产物", width=76, height=26, corner_radius=9, fg_color="transparent", hover_color=("#f0f4f4", "#34373f"), text_color=("#64777b", "#cbd5e1"), command=lambda run_path=meta.get("run_path"): self.open_artifacts(run_path)).pack(side="left", padx=(5, 0))

    def _scroll_to_bottom(self):
        if not self.messages or not self.messages.winfo_exists():
            return
        self.messages.update_idletasks()
        canvas = getattr(self.messages, "_parent_canvas", None)
        if canvas is not None:
            canvas.yview_moveto(1.0)

    def _append(self, role, content, animate=False, meta=None, chat=None):
        chat = chat if chat is not None else self._chat()
        message = {"id": uuid.uuid4().hex, "role": role, "content": str(content), "time": timestamp(), "meta": meta or {}}
        chat.setdefault("messages", []).append(message)
        if role in ("user", "assistant") and chat["id"] == self.active_chat_id:
            self.message_labels[id(message)] = self._bubble(role, message["content"], message)
            self.after_idle(self._scroll_to_bottom)
        self._save_workspace()
        self._schedule_trace_refresh()
        return message

    def _create_live_message(self):
        self.live_chat_id = self.active_chat_id
        self.live_message = self._append("assistant", "正在准备任务…", meta={"status_label": "运行中"})

    def _update_live_status(self, status):
        if not self.live_message:
            return
        # 主对话保留简短状态，详细操作进入运行详情。
        self.live_message["content"] = status
        label = self.message_labels.get(id(self.live_message))
        if label is not None and label.winfo_exists():
            label.configure(text=status)
        self._save_workspace()

    def _finish_live_message(self, content, meta):
        message, chat_id = self.live_message, self.live_chat_id
        self.live_message = self.live_chat_id = None
        if message is None:
            return
        message["content"] = str(content)
        message["meta"] = meta
        # 先完整保存再播放显示动画，退出不会只留下半句回答。
        self._save_workspace()
        if chat_id == self.active_chat_id:
            self._show_chat()
            if "```" not in content and len(content) < 6000:
                self._type_message(chat_id, message, str(content))

    def _type_message(self, chat_id, message, content, index=0):
        if self.closing or chat_id != self.active_chat_id:
            return
        label = self.message_labels.get(id(message))
        if label is None or not label.winfo_exists():
            return
        index = min(len(content), index + max(2, len(content) // 150))
        label.configure(text=self._present_text(content[:index]))
        if index < len(content):
            self.typing_job = self.after(20, self._type_message, chat_id, message, content, index)
        else:
            self.typing_job = None
            self._scroll_to_bottom()

    def _schedule_trace_refresh(self):
        if self.trace_window is not None and self.trace_refresh_job is None:
            self.trace_refresh_job = self.after(120, self._refresh_trace)

    def open_trace(self, run_id=None):
        self.trace_run_id = run_id
        if self.trace_window is not None and self.trace_window.winfo_exists():
            self._refresh_trace()
            self.trace_window.focus_set()
            return
        self.trace_window = ctk.CTkToplevel(self)
        self.trace_window.title("运行详情 · 目标 / 执行 / 核验")
        self.trace_window.geometry("720x720")
        self.trace_window.minsize(560, 420)
        self.trace_window.transient(self)
        self.trace_window.grid_columnconfigure(0, weight=1)
        self.trace_window.grid_rowconfigure(2, weight=1)
        ctk.CTkLabel(self.trace_window, text="运行详情", anchor="w", font=ctk.CTkFont(size=17, weight="bold")).grid(row=0, column=0, padx=22, pady=(18, 4), sticky="ew")
        ctk.CTkLabel(self.trace_window, text="这里只显示模型输出、工具操作和审核记录；主对话只保留回答。", anchor="w", text_color=("#64748b", "#a1a1aa")).grid(row=1, column=0, padx=22, pady=(0, 12), sticky="ew")
        self.trace_box = ctk.CTkTextbox(self.trace_window, wrap="word", corner_radius=12, fg_color=("#f8fafc", "#18191d"), font=ctk.CTkFont(size=12))
        self.trace_box.grid(row=2, column=0, padx=18, pady=(0, 18), sticky="nsew")
        self.trace_box.configure(state="disabled")
        self.trace_window.protocol("WM_DELETE_WINDOW", self._close_trace)
        self._refresh_trace()

    def _close_trace(self):
        if self.trace_refresh_job is not None:
            self.after_cancel(self.trace_refresh_job)
            self.trace_refresh_job = None
        if self.trace_window is not None:
            self.trace_window.destroy()
        self.trace_window = None
        self.trace_box = None

    def _refresh_trace(self):
        self.trace_refresh_job = None
        if self.trace_box is None or not self.trace_box.winfo_exists():
            return
        self.trace_box.configure(state="normal")
        self.trace_box.delete("1.0", "end")
        run_id = getattr(self, "trace_run_id", None)
        traces = [m for m in self._chat().get("messages", []) if m.get("role") == "trace" and (not run_id or m.get("meta", {}).get("run_id") == run_id)]
        if not traces:
            self.trace_box.insert("end", "当前聊天还没有运行详情。\n")
        else:
            for item in traces:
                self.trace_box.insert("end", item.get("content", "") + "\n\n")
        if not traces and run_id:
            message = next((m for m in self._chat()["messages"] if m.get("meta", {}).get("run_id") == run_id), {})
            run_path = message.get("meta", {}).get("run_path")
            if run_path:
                path = Path(run_path) / "events.jsonl"
                if path.exists():
                    with path.open("r", encoding="utf-8") as handle:
                        self.trace_box.insert("end", handle.read(200000))
        if self.live_stream and self.live_chat_id == self.active_chat_id and (not run_id or run_id == self.current_run_id):
            self.trace_box.insert("end", "实时模型输出\n" + self.live_stream + "\n")
        self.trace_box.configure(state="disabled")
        self.trace_box.see("end")

    def new_project(self):
        folder = filedialog.askdirectory(title="选择项目文件夹", mustexist=True, parent=self)
        if not folder:
            return
        selected = Path(folder).expanduser().resolve()
        for existing in self.projects:
            if self._project_root(existing) == selected:
                existing["archived"] = False
                self.active_project_id = existing["id"]
                self.active_chat_id = next((chat["id"] for chat in existing["chats"] if not chat.get("archived", False)), existing["chats"][0]["id"])
                self._save_workspace(); self._refresh_sidebar(); self._show_chat()
                messagebox.showinfo("项目已存在", "这个文件夹已经添加到项目列表，已为你切换过去。", parent=self)
                return
        default_name = selected.name or "新项目"
        name = simpledialog.askstring("新建项目", "项目名称：", initialvalue=default_name, parent=self)
        if name is None:
            return
        project = self._project_data(name.strip() or default_name, str(selected))
        self.projects.append(project)
        self.active_project_id = project["id"]; self.active_chat_id = project["chats"][0]["id"]
        self._save_workspace(); self._refresh_sidebar(); self._show_chat()

    def new_chat(self):
        self._remember_draft()
        chat = {"id": uuid.uuid4().hex, "title": "新聊天", "created": timestamp(), "messages": [], "archived": False}
        self._project()["chats"].insert(0, chat); self.active_chat_id = chat["id"]
        self._save_workspace(); self._refresh_sidebar(); self._show_chat()
        self._restore_draft()

    def focus_settings(self): self.settings.focus_set()

    def _ask_tool(self, name, args, result, event):
        if self.closing or self.stop_event.is_set():
            event.set()
            return
        dialog = ctk.CTkToplevel(self)
        self.approval_dialog = dialog
        dialog.title("长安需要你的批准")
        dialog.geometry("660x500")
        dialog.transient(self)
        ctk.CTkLabel(dialog, text=f"准备执行：{name}", font=ctk.CTkFont(size=17, weight="bold")).pack(pady=16)
        box = ctk.CTkTextbox(dialog, wrap="word")
        box.pack(fill="both", expand=True, padx=18, pady=8)
        box.insert("1.0", redact(json.dumps(args, ensure_ascii=False, indent=2))[:12000])
        box.configure(state="disabled")
        row = ctk.CTkFrame(dialog, fg_color="transparent")
        row.pack(pady=14)
        def decide(accepted):
            result[0] = accepted and not self.stop_event.is_set()
            event.set()
            dialog.destroy()
            self.approval_dialog = None
        ctk.CTkButton(row, text="拒绝本次", command=lambda: decide(False)).pack(side="left", padx=8)
        ctk.CTkButton(row, text="允许本次", command=lambda: decide(True)).pack(side="left", padx=8)
        dialog.protocol("WM_DELETE_WINDOW", lambda: decide(False))
        dialog.lift()

    def approve_tool(self, name, args):
        decision, event = [False], threading.Event()
        self._ui_after(0, self._ask_tool, name, args, decision, event)
        while not event.wait(0.1):
            if self.stop_event.is_set() or self.closing:
                return False
        return decision[0] and not self.stop_event.is_set()

    def open_artifacts(self, run_path=None):
        if self.artifact_window is not None and self.artifact_window.winfo_exists():
            self.artifact_window.focus_set()
            return
        root = self._project_root()
        artifact_dir = root / "artifacts"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.artifact_window = ctk.CTkToplevel(self)
        self.artifact_window.title("长安 · 项目产物")
        self.artifact_window.geometry("980x680")
        self.artifact_window.grid_columnconfigure(1, weight=1)
        self.artifact_window.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(self.artifact_window, text="项目产物", font=ctk.CTkFont(size=17, weight="bold"), anchor="w").grid(row=0, column=0, columnspan=2, padx=20, pady=(18, 10), sticky="ew")
        files = ctk.CTkScrollableFrame(self.artifact_window, width=260, fg_color=("#f4f5f7", "#1f2025"))
        files.grid(row=1, column=0, padx=(16, 8), pady=(0, 16), sticky="nsew")
        files.grid_columnconfigure(0, weight=1)
        preview = ctk.CTkTextbox(self.artifact_window, wrap="word", corner_radius=10, font=ctk.CTkFont(size=12))
        preview.grid(row=1, column=1, padx=(8, 16), pady=(0, 16), sticky="nsew")
        self.artifact_preview = preview
        artifact_files = [p for p in artifact_dir.rglob("*") if p.is_file()]
        for message in self._chat().get("messages", []):
            meta = message.get("meta", {})
            directory = Path(meta["run_path"]) if meta.get("run_path") else None
            if directory and directory.resolve().is_relative_to(root) and (run_path is None or str(directory) == str(run_path)):
                artifact_files.extend(p for p in directory.glob("*") if p.name in ("answer.md", "result.json"))
            for relative in meta.get("files", []):
                path = (root / relative).resolve()
                if path.is_relative_to(root) and path.is_file():
                    artifact_files.append(path)
        artifact_files = list(dict.fromkeys(artifact_files))[-300:]
        if not artifact_files:
            ctk.CTkLabel(files, text="暂无产物", text_color=("#64748b", "#a1a1aa")).pack(padx=10, pady=20)
        for file_path in artifact_files:
            row = ctk.CTkFrame(files, fg_color="transparent")
            row.pack(fill="x", pady=3)
            ctk.CTkButton(row, text=file_path.name, anchor="w", height=30, fg_color="transparent", text_color=("#334155", "#e4e4e7"), hover_color=("#e2e8f0", "#30323a"), command=lambda p=file_path: self.preview_artifact(p)).pack(fill="x")
            ctk.CTkButton(row, text="差异", width=45, height=24, corner_radius=6, command=lambda p=file_path: self.diff_artifact(p)).pack(anchor="e", padx=4)
            ctk.CTkButton(row, text="历史", width=45, height=24, corner_radius=6, command=lambda p=file_path: self.history_artifact(p)).pack(anchor="e", padx=4)
            ctk.CTkButton(row, text="恢复", width=45, height=24, corner_radius=6, command=lambda p=file_path: self.restore_artifact(p)).pack(anchor="e", padx=4)
        self.artifact_window.protocol("WM_DELETE_WINDOW", self._close_artifacts)

    def resume_checkpoint(self):
        if self.running:
            messagebox.showinfo("已有任务运行", "请先停止当前任务再恢复。", parent=self)
            return
        items = []
        if self.pending_tasks:
            if messagebox.askyesno("恢复等待队列", f"有 {len(self.pending_tasks)} 个已保存的等待任务，是否继续队列？\n选“否”可查看中断任务。", parent=self):
                self.stop_event.clear()
                self._start_queued_task()
                return
        for project in self.projects:
            roots = [self._project_root(project) / "runs"]
            roots.extend(self._chat_dir(project, chat) / "runs" for chat in project["chats"])
            for root in roots:
                for checkpoint in root.glob("*/checkpoint.json"):
                    try:
                        data = read_json(checkpoint, {})
                        if not data.get("task"):
                            continue
                        meta = read_json(checkpoint.with_name("run.json"), {})
                        if (data.get("stage") == "completed" and data.get("result", {}).get("status") != "failed") or meta.get("status") in ("passed", "completed"):
                            continue
                        meta.setdefault("run_id", checkpoint.parent.name)
                        meta["project_id"] = project["id"]
                        chat = next((c for c in project["chats"] if self._chat_dir(project, c) / "runs" == root), None)
                        if chat is None:
                            chat = next((c for c in project["chats"] if c["id"] == meta.get("chat_id")), None)
                        if chat is None:
                            chat = next((c for c in project["chats"] if any(m.get("meta", {}).get("run_id") == meta["run_id"] for m in c["messages"])), None)
                        meta["chat_id"] = chat["id"] if chat else project["chats"][0]["id"]
                        items.append((checkpoint, data, meta, project["name"]))
                    except (OSError, RuntimeError, ValueError, TypeError):
                        logging.exception("检查点读取失败：%s", checkpoint)
        items.sort(key=lambda item: item[1].get("updated", ""), reverse=True)
        if not items:
            messagebox.showinfo("恢复任务", "没有未完成的检查点。", parent=self)
            return
        labels = [f"{i + 1}. {item[3]} · 第{item[1].get('next_round', 1)}轮 · {item[1]['task'][:60]}" for i, item in enumerate(items)]
        choice = simpledialog.askinteger("恢复任务", "选择要恢复的编号：\n\n" + "\n".join(labels), minvalue=1, maxvalue=len(items), parent=self)
        if choice is None:
            return
        checkpoint, data, meta, _ = items[choice - 1]
        if int(data.get("next_round", 1)) > int(self.vars["max_rounds"].get()):
            messagebox.showinfo("需要更多轮次", "这个任务已达到最大轮次，请先增加最大核对轮次再恢复。", parent=self)
            return
        self.active_project_id, self.active_chat_id = meta["project_id"], meta["chat_id"]
        self.resume_checkpoint_path = checkpoint
        self._launch_task(data["task"], resumed=True, run_meta=meta)

    def _close_artifacts(self):
        if self.artifact_window is not None:
            self.artifact_window.destroy()
        self.artifact_window = None

    def preview_artifact(self, path):
        try:
            if path.suffix.lower() in (".pdf", ".docx", ".xlsx", ".png", ".jpg", ".zip", ".exe"):
                content = f"{path.name}\n文件大小：{path.stat().st_size:,} 字节\n此类型请在系统中打开预览。"
            else:
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    content = handle.read(200000)
        except OSError as exc:
            content = str(exc)
        self.artifact_preview.configure(state="normal")
        self.artifact_preview.delete("1.0", "end")
        self.artifact_preview.insert("end", content)
        self.artifact_preview.configure(state="disabled")

    def _backup_entries(self, path):
        root = self._project_root()
        if not path.resolve().is_relative_to(root):
            return []
        relative = path.resolve().relative_to(root).as_posix()
        entries = read_json(root / ".agent-backups" / "index.json", [])
        return [item for item in entries if str(item.get("path", "")).replace("\\", "/") == relative and item.get("backup")]

    def _latest_backup(self, path):
        root = self._project_root()
        for item in reversed(self._backup_entries(path)):
            backup = (root / item["backup"]).resolve()
            if backup.is_relative_to(root / ".agent-backups") and backup.is_file():
                return backup
        return None

    def _restore_backup(self, path, backup):
        root = self._project_root()
        if not backup.resolve().is_relative_to(root / ".agent-backups") or not path.resolve().is_relative_to(root):
            raise ValueError("版本文件不属于当前项目")
        executor = ToolExecutor(root, "full access")
        before = executor._hash(path)
        relative = path.relative_to(root).as_posix()
        saved = root / ".agent-backups" / (uuid.uuid4().hex + ".bak")
        atomic_write(saved, path.read_bytes())
        atomic_write(path, backup.read_bytes())
        executor._record_version(relative, "restore", before, executor._hash(path), saved.relative_to(root).as_posix())
        self.preview_artifact(path)

    def diff_artifact(self, path):
        backup = self._latest_backup(path)
        if not backup:
            self.preview_artifact(path)
            return
        before = backup.read_text(encoding="utf-8", errors="replace").splitlines()
        after = path.read_text(encoding="utf-8", errors="replace").splitlines()
        diff = "\n".join(difflib.unified_diff(before, after, fromfile=str(backup), tofile=str(path), lineterm=""))
        self.artifact_preview.configure(state="normal")
        self.artifact_preview.delete("1.0", "end")
        self.artifact_preview.insert("end", diff or "没有差异")
        self.artifact_preview.configure(state="disabled")

    def history_artifact(self, path):
        entries = self._backup_entries(path)
        if not entries:
            messagebox.showinfo("版本历史", "这个文件还没有历史版本。", parent=self.artifact_window)
            return
        labels = [f"{i + 1}. {item.get('time', '')} · {item.get('operation', '')}" for i, item in enumerate(entries)]
        choice = simpledialog.askinteger("版本历史", "选择要恢复的版本：\n\n" + "\n".join(labels), parent=self.artifact_window, minvalue=1, maxvalue=len(entries))
        if choice is None:
            return
        backup = (self._project_root() / entries[choice - 1]["backup"]).resolve()
        if backup.exists() and messagebox.askyesno("恢复版本", f"恢复第 {choice} 个版本？当前内容将先备份。", parent=self.artifact_window):
            self._restore_backup(path, backup)

    def restore_artifact(self, path):
        backup = self._latest_backup(path)
        if not backup:
            messagebox.showinfo("恢复产物", "这个文件没有可恢复的历史版本。", parent=self.artifact_window)
            return
        if messagebox.askyesno("恢复产物", f"恢复最近备份？当前内容会先备份。\n{path.name}", parent=self.artifact_window):
            self._restore_backup(path, backup)

    def _enter_send(self, event):
        if event.state & 0x0001: return
        self.start(); return "break"

    def _activity(self, text):
        self.status.configure(text=text)
        self._append("trace", text)

    def toggle_theme(self):
        self.dark = bool(self.theme_switch.get())
        self.vars["appearance"].set("Dark" if self.dark else "Light")
        ctk.set_appearance_mode(self.vars["appearance"].get())
        self._save_config()

    def refresh_models(self):
        gpt_base = self.vars["gpt_base_url"].get().strip().rstrip("/")
        deep_base = self.vars["deepseek_base_url"].get().strip().rstrip("/")
        claude_base = self.vars["claude_base_url"].get().strip().rstrip("/")
        if not any((gpt_base, deep_base, claude_base)):
            self.status.configure(text="请先填写 API 地址"); return
        self.status.configure(text="正在读取模型列表…")
        threading.Thread(target=self._fetch_models, args=(gpt_base, deep_base, claude_base, self.vars["gpt_key"].get().strip(), self.vars["deepseek_key"].get().strip(), self.vars["claude_key"].get().strip()), daemon=True).start()

    def _fetch_models(self, gpt_base, deep_base, claude_base, gpt_key, deep_key, claude_key):
        result, errors = {}, []
        for name, base, key in (("gpt", gpt_base, gpt_key), ("deepseek", deep_base, deep_key), ("claude", claude_base, claude_key)):
            if not key: continue
            try:
                request = urllib.request.Request(base + "/models", headers={"Authorization": "Bearer " + key, "Accept": "application/json"})
                with urllib.request.build_opener(__import__("orchestrator")._NoRedirect).open(request, timeout=15) as response:
                    data = json.loads(response.read(4_000_000).decode("utf-8"))
                models = sorted({m["id"] for m in data.get("data", []) if isinstance(m, dict) and isinstance(m.get("id"), str) and name in m["id"].lower()})
                if models: result[name] = models
                else: errors.append(name + "返回空列表")
            except Exception as exc: errors.append(name + "：" + str(exc))
        self._ui_after(0, self._apply_models, result, errors)

    def _apply_models(self, result, errors):
        if "gpt" in result:
            self.gpt_models = result["gpt"]; self.gpt_combo.configure(values=self.gpt_models)

        if "deepseek" in result:
            self.deepseek_models = result["deepseek"]; self.deepseek_combo.configure(values=self.deepseek_models)

        if "claude" in result:
            self.claude_models = result["claude"]; self.claude_combo.configure(values=self.claude_models)

        self._save_config(); self.status.configure(text=f"已加载 GPT {len(result.get('gpt', []))} 个，DeepSeek {len(result.get('deepseek', []))} 个，Claude {len(result.get('claude', []))} 个")
        if errors:
            logging.warning("模型列表：%s", "；".join(errors))
            self.status.configure(text="部分模型列表加载失败，保留已选模型")

    def start(self):
        task = self.prompt.get("1.0", "end").strip()
        if not task:
            return
        settings = self._validated_settings()
        if settings is None:
            return
        self._chat()["draft"] = ""
        if self.running:
            message = self._append("user", task)
            self.pending_tasks.append({"id": uuid.uuid4().hex, "project_id": self.active_project_id, "chat_id": self.active_chat_id, "task": task, "message_id": message["id"], "created": timestamp()})
            self._save_workspace()
            self.status.configure(text=f"任务已排队 · 等待 {len(self.pending_tasks)} 项")
            self.prompt.delete("1.0", "end")
            return
        self._launch_task(task, settings=settings)

    def _validated_settings(self):
        from urllib.parse import urlparse
        values = {key: variable.get().strip() for key, variable in self.vars.items()}
        try:
            for name in ("gpt", "deepseek"):
                if not values[name + "_key"]:
                    raise ValueError("请填写 GPT 和 DeepSeek 密钥。Claude 可留空。")
            for name in ("gpt", "deepseek", "claude"):
                if name == "claude" and not values["claude_key"]:
                    continue
                parsed = urlparse(values[name + "_base_url"])
                if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query:
                    raise ValueError(f"{name} API 地址必须为有效的 http/https 根地址")
                if not values[name + "_model"]:
                    raise ValueError(f"请选择 {name} 模型")
            for key in values:
                if "cost_per_1k" in key or key == "max_budget":
                    number = float(values[key] or 0)
                    if not math.isfinite(number) or number < 0:
                        raise ValueError("价格与预算必须是有限的非负数字")
            if not 1 <= int(values["max_rounds"]) <= 10:
                raise ValueError("最大核对轮次必须在 1 到 10 之间")
            if float(values["max_budget"] or 0) > 0:
                for prefix in ("gpt", "deepseek", "claude"):
                    if prefix == "claude" and not values["claude_key"]:
                        continue
                    prices = [float(values.get(prefix + suffix) or 0) for suffix in ("_input_cost_per_1k", "_output_cost_per_1k", "_cost_per_1k")]
                    if not any(prices):
                        raise ValueError(f"设置预算前请先填写 {prefix} 价格，价格为零时无法有效限制花费")
        except ValueError as exc:
            messagebox.showwarning("设置需要调整", str(exc), parent=self)
            return None
        return values

    def _launch_task(self, task, resumed=False, run_meta=None, settings=None, queued=False):
        if self.running or self.closing:
            return
        settings = settings or self._validated_settings()
        if settings is None:
            return
        self._save_config()
        project, chat = self._project(), self._chat()
        if not resumed and not queued:
            self._append("user", task)
        if chat.get("title") == "新聊天":
            self.store.relocate(project, chat, title=task.replace("\n", " ")[:24])
        run_meta = run_meta or {}
        run_id = run_meta.get("run_id") or (self.resume_checkpoint_path.parent.name if self.resume_checkpoint_path else uuid.uuid4().hex)
        run_dir = self.resume_checkpoint_path.parent if self.resume_checkpoint_path else self._chat_dir(project, chat) / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        existing = next((m for m in reversed(chat["messages"]) if m.get("role") == "assistant" and m.get("meta", {}).get("run_id") == run_id), None) if resumed else None
        if existing is None:
            self._create_live_message()
        else:
            self.live_message, self.live_chat_id = existing, chat["id"]
            existing["content"] = "正在恢复上次任务…"
            existing["meta"]["status_label"] = "运行中"
        self.live_message.setdefault("meta", {})["run_id"] = run_id
        self.live_message["meta"]["run_path"] = str(run_dir)
        past_messages = chat["messages"]
        if isinstance(queued, dict):
            index = next((i for i, m in enumerate(past_messages) if m.get("id") == queued.get("message_id")), len(past_messages))
            past_messages = past_messages[:index]
        history = [{"role": m["role"], "content": m["content"][:8000]} for m in past_messages if m.get("role") in ("user", "assistant") and m is not self.live_message]
        if history and history[-1]["role"] == "user" and history[-1]["content"] == task:
            history.pop()
        self.stop_event.clear()
        self.running = True
        self.current_run_id = run_id
        self.current_run_meta_path = run_dir / "run.json"
        self.run_started_at = time.perf_counter()
        self.live_stream = ""
        self.run_context = {"id": run_id, "task": task, "settings": settings, "project_id": project["id"], "chat": chat, "project_dir": self._project_root(project), "run_dir": run_dir, "checkpoint": run_dir / "checkpoint.json", "started": self.run_started_at, "elapsed_before": float(run_meta.get("duration_seconds", 0) or 0), "history": history[-12:]}
        self.current_journal = RunJournal(run_dir / "events.jsonl")
        self.resume_checkpoint_path = None
        write_json(self.current_run_meta_path, {**run_meta, "run_id": run_id, "project_id": project["id"], "chat_id": chat["id"], "task": task, "status": "running", "created": run_meta.get("created", timestamp()), "updated": timestamp(), "checkpoint": "checkpoint.json", "duration_seconds": self.run_context["elapsed_before"]})
        self._save_workspace()
        self._refresh_sidebar(); self._show_chat()
        self.prompt.delete("1.0", "end")
        self.run_btn.configure(state="normal", text="加入队列")
        self.stop_btn.configure(state="normal")
        self._update_runtime()
        self.worker = threading.Thread(target=self._run, args=(self.run_context, self.current_journal), daemon=True, name="changan-task")
        self.worker.start()

    def stop(self):
        self.stop_event.set()
        self.pending_tasks.clear()
        if self.approval_dialog is not None and self.approval_dialog.winfo_exists():
            self.approval_dialog.destroy()
            self.approval_dialog = None
        self._save_workspace()
        self.status.configure(text="正在停止，队列已清空…")

    def _event(self, run_id, kind, value):
        if self.closing or run_id != self.current_run_id:
            return
        chat = self.run_context["chat"]
        labels = {"analysis": "目标清单", "execution": "执行结果", "verification": "审核结果", "verifier": "审核模型", "verifier_fallback": "审核自动降级", "error": "执行错误", "deterministic": "机器校验", "resume": "任务恢复", "tool": "工具操作", "tool_result": "工具结果", "retry": "网络重试"}
        if kind == "delta":
            self.live_stream = (self.live_stream + value)[-60000:]
            self._schedule_trace_refresh()
            return
        if kind == "stage":
            self.current_stage = value
            self._write_run_meta(last_stage=value)
            self.status.configure(text=value)
            self._update_live_status("正在处理你的请求…")
        if kind == "round":
            value = f"第 {value} 轮开始"
        if kind in labels or kind in ("round", "stage"):
            self._append("trace", labels.get(kind, "进度") + "\n" + redact(str(value))[:16000], meta={"run_id": run_id}, chat=chat)

    def _run(self, context, journal):
        settings = context["settings"]
        run_dir = context["run_dir"]
        def emit(kind, value):
            journal.append(kind, str(value))
            self._ui_after(0, self._event, context["id"], kind, str(value))
        try:
            providers = {}
            for prefix, name in (("gpt", "GPT"), ("deepseek", "DeepSeek"), ("claude", "Claude")):
                if prefix == "claude" and not settings["claude_key"]:
                    continue
                provider = Provider(name, settings[prefix + "_base_url"], settings[prefix + "_key"], settings[prefix + "_model"], settings[prefix + "_reasoning"], retries=3)
                fallback = settings.get(prefix + "_cost_per_1k", "0")
                provider.configure_budget(float(settings.get(prefix + "_input_cost_per_1k") or fallback), float(settings.get(prefix + "_output_cost_per_1k") or fallback), float(settings["max_budget"] or 0))
                providers[prefix] = provider
            domains = {x.strip().lower() for x in settings["allowed_domains"].split(",") if x.strip()}
            executor = ToolExecutor(context["project_dir"], settings["permission_mode"], approval=self.approve_tool, allowed_domains=domains, should_stop=self.stop_event.is_set)
            agent = Agent(providers["gpt"], providers["deepseek"], int(settings["max_rounds"]), emit, self.stop_event.is_set, executor, context["checkpoint"], float(settings["max_budget"] or 0), verifier=providers.get("claude"))
            result = agent.run(context["task"], context=context["history"])
            write_json(run_dir / "result.json", result)
            atomic_write(run_dir / "answer.md", result["final"] + "\n")
            usage = result.get("usage", {})
            actual_verifier = json.loads(result["rounds"][-1]["verification"]).get("verifier", "GPT") if result["rounds"] else "GPT"
            files = list(dict.fromkeys(a["path"] for r in result["rounds"] for a in r.get("artifacts", []) if a.get("path")))
            meta = {"status_label": "已完成" if result["status"] == "passed" else "未通过", "duration_seconds": self._elapsed(context), "rounds": len(result["rounds"]), "models": [settings["gpt_model"], settings["deepseek_model"], actual_verifier + " 审核"], "usage": sum(x.get("total_tokens", 0) for x in usage.values()), "files": files, "outputs": ["result.json", "answer.md"], "cost": sum(x.get("estimated_cost", 0) for x in usage.values()) if any(float(v or 0) > 0 for k, v in settings.items() if "cost_per_1k" in k) else None, "usage_estimated": any(x.get("estimated_tokens") for x in usage.values()), "run_id": context["id"], "run_path": str(run_dir)}
            self._write_context_meta(context, result["status"], duration_seconds=meta["duration_seconds"], last_stage="完成")
            self._ui_after(0, self._finish_live_message, result["final"], meta)
            self._ui_after(0, self._set_status, "已完成" if result["status"] == "passed" else "未通过审核，请查看详情")
        except Exception as exc:
            stopped = self.stop_event.is_set() or isinstance(exc, TaskStopped)
            detail = redact(str(exc))
            journal.append("error", detail + "\n" + traceback.format_exc(limit=8))
            logging.exception("运行 %s 失败", context["id"])
            text = "任务已停止，进度已保存，可从恢复任务继续。" if stopped else "暂时无法完成请求，进度已保存。请查看运行详情或恢复任务。"
            if isinstance(exc, ProviderRequestError):
                text = f"{exc}。进度已保存，可稍后恢复任务。"
            meta = {"status_label": "已停止" if stopped else "运行失败", "run_id": context["id"], "run_path": str(run_dir), "duration_seconds": self._elapsed(context)}
            self._write_context_meta(context, "interrupted" if stopped else "failed", duration_seconds=meta["duration_seconds"], last_error=detail)
            self._ui_after(0, self._event, context["id"], "error", detail)
            self._ui_after(0, self._finish_live_message, text, meta)
            self._ui_after(0, self._set_status, "已停止" if stopped else "运行失败，可恢复")
        finally:
            journal.append("ended", "运行结束")
            self._ui_after(0, self._task_finished_ui)

    @staticmethod
    def _elapsed(context):
        return context["elapsed_before"] + time.perf_counter() - context["started"]

    def _set_status(self, text):
        self.current_stage = text
        self.status.configure(text=text)

    def _write_context_meta(self, context, status=None, **extra):
        with self._run_lock:
            path = context["run_dir"] / "run.json"
            data = read_json(path, {})
            if status:
                data["status"] = status
            data.update(extra, updated=datetime.now().isoformat(timespec="seconds"))
            write_json(path, data)

    def _task_finished_ui(self):
        self.running = False
        if self.runtime_job is not None:
            self.after_cancel(self.runtime_job)
            self.runtime_job = None
        self.run_btn.configure(state="normal", text="发送  ⏎")
        self.stop_btn.configure(state="disabled")
        self.runtime_label.configure(text="")
        self._save_workspace()
        if self.pending_tasks and not self.stop_event.is_set() and not self.closing:
            self.after(100, self._start_queued_task)

    def _update_runtime(self):
        if self.closing or not self.running or not self.run_context:
            return
        elapsed = int(self._elapsed(self.run_context))
        self.runtime_label.configure(text=f"运行 {elapsed // 60:02d}:{elapsed % 60:02d}")
        if elapsed % 10 == 0:
            self._write_context_meta(self.run_context, duration_seconds=elapsed)
        self.runtime_job = self.after(1000, self._update_runtime)

    def _start_queued_task(self):
        if self.running or self.closing or not self.pending_tasks:
            return
        queued = self.pending_tasks.pop(0)
        project = next((p for p in self.projects if p["id"] == queued["project_id"]), None)
        if not project or not any(c["id"] == queued["chat_id"] for c in project["chats"]):
            self._save_workspace()
            self.status.configure(text="队列关联的聊天不存在，请重新提交")
            return
        self.active_project_id, self.active_chat_id = queued["project_id"], queued["chat_id"]
        self._refresh_sidebar(); self._show_chat()
        settings = self._validated_settings()
        if settings is None:
            self.pending_tasks.insert(0, queued)
            self._save_workspace()
            return
        self._launch_task(queued["task"], settings=settings, queued=queued)


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    configure_logging(DATA_DIR)
    lock = None
    try:
        lock = InstanceLock(DATA_DIR)
        ctk.set_appearance_mode("Light")
        ctk.set_default_color_theme("blue")
        logging.info("长安启动，数据目录：%s", DATA_DIR)
        App().mainloop()
    except Exception as exc:
        logging.exception("启动失败")
        error_root = tk.Tk()
        error_root.withdraw()
        messagebox.showerror("长安无法启动", redact(str(exc)) + "\n日志：" + str(DATA_DIR / "logs" / "changan.log"), parent=error_root)
        error_root.destroy()
    finally:
        if lock:
            lock.close()
