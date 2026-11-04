"""项目文件、网页文本、Git 状态和等待任务的侧边工作区。"""

from __future__ import annotations

import os
import subprocess
import threading
import tkinter as tk
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import customtkinter as ctk

from network_safety import safe_http_get


SKIP_DIRECTORIES = {".agent-backups", ".git", ".venv", ".venv-lora", ".venv-workbench", "archive", "logs", "node_modules", "__pycache__", "venv"}
TEXT_EXTENSIONS = {".bat", ".cfg", ".css", ".csv", ".html", ".ini", ".js", ".json", ".log", ".md", ".py", ".ps1", ".sh", ".sql", ".toml", ".ts", ".tsx", ".txt", ".xml", ".yaml", ".yml"}
MAX_PREVIEW_BYTES = 1_000_000


def is_private_name(name: str) -> bool:
    lowered = name.lower()
    return (
        lowered in {".env", "agent_config.json", "workspace.json"}
        or lowered.startswith(".env.") and lowered != ".env.example"
        or lowered.startswith("memory.sqlite3")
        or Path(lowered).suffix in {".key", ".pem", ".p12", ".pfx"}
    )


def _is_link(path: Path) -> bool:
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


class _PageTextParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title_parts = []
        self.text_parts = []
        self.links = []
        self.in_title = False
        self.skip_depth = 0
        self.anchor = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag in ("script", "style", "noscript", "svg"):
            self.skip_depth += 1
        if self.skip_depth:
            return
        if tag == "title":
            self.in_title = True
        if tag == "a" and values.get("href"):
            self.anchor = [urljoin(self.base_url, values["href"]), []]
        if tag in {"br", "div", "h1", "h2", "h3", "li", "p", "section", "tr"}:
            self.text_parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg") and self.skip_depth:
            self.skip_depth -= 1
        if self.skip_depth:
            return
        if tag == "title":
            self.in_title = False
        if tag == "a" and self.anchor:
            label = " ".join("".join(self.anchor[1]).split())
            target = self.anchor[0]
            if label and urlsplit(target).scheme in ("http", "https"):
                self.links.append((label[:100], target))
            self.anchor = None
        if tag in {"div", "h1", "h2", "h3", "li", "p", "section", "tr"}:
            self.text_parts.append("\n")

    def handle_data(self, data):
        if self.skip_depth:
            return
        if self.in_title:
            self.title_parts.append(data)
        self.text_parts.append(data)
        if self.anchor:
            self.anchor[1].append(data)


class WorkspacePanel(ctk.CTkFrame):
    def __init__(self, master, app):
        super().__init__(master, width=390, corner_radius=0, fg_color=("#f6f6f5", "#1c1c1c"))
        self.app = app
        self.grid_propagate(False)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self.current_project_id = ""
        self.project_root = None
        self.current_dir = None
        self.selected_file = None
        self.browser_history = []
        self.browser_index = -1
        self.browser_request = 0
        self.browser_busy = False
        self.git_request = 0

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, padx=16, pady=(14, 8), sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(header, text="工作区", anchor="w", font=ctk.CTkFont(size=16, weight="bold")).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(header, text="展开工作台", width=96, height=28, command=self.app.open_workbench).grid(row=0, column=1, padx=6, sticky="e")
        ctk.CTkButton(header, text="×", width=30, height=28, fg_color="transparent", command=self.app.toggle_workspace).grid(row=0, column=2, sticky="e")

        self.tabs = ctk.CTkTabview(self, corner_radius=10, segmented_button_selected_color=("#5b5b5b", "#555555"))
        self.tabs.grid(row=1, column=0, padx=12, pady=(0, 12), sticky="nsew")
        self.file_tab = self.tabs.add("文件")
        self.web_tab = self.tabs.add("网页")
        self.git_tab = self.tabs.add("Git")
        self.queue_tab = self.tabs.add("队列")
        self.tabs.configure(command=self._tab_changed)
        self._build_files()
        self._build_web()
        self._build_git()
        self._build_queue()

    def _build_files(self):
        tab = self.file_tab
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(2, weight=1)
        tab.grid_rowconfigure(4, weight=2)
        self.file_location = ctk.CTkLabel(tab, text="选择一个项目以浏览文件", anchor="w", text_color=("#777777", "#aaaaaa"))
        self.file_location.grid(row=0, column=0, padx=10, pady=(8, 4), sticky="ew")
        controls = ctk.CTkFrame(tab, fg_color="transparent")
        controls.grid(row=1, column=0, padx=6, pady=(0, 4), sticky="ew")
        ctk.CTkButton(controls, text="上一级", width=64, height=28, fg_color="transparent", text_color=("#626262", "#c9c9c9"), command=self.go_up).pack(side="left")
        ctk.CTkButton(controls, text="刷新", width=54, height=28, fg_color="transparent", text_color=("#626262", "#c9c9c9"), command=self.refresh_files).pack(side="right")
        ctk.CTkButton(controls, text="编辑", width=54, height=28, command=lambda: self.app.open_workbench("代码编辑器", self.selected_file) if self.selected_file else self.app.open_workbench("代码编辑器")).pack(side="right", padx=4)
        self.file_list = ctk.CTkScrollableFrame(tab, fg_color="transparent", corner_radius=8)
        self.file_list.grid(row=2, column=0, padx=6, pady=2, sticky="nsew")
        self.file_list.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(tab, text="预览 · 仅查看，不会修改项目文件", anchor="w", font=ctk.CTkFont(size=11), text_color=("#777777", "#aaaaaa")).grid(row=3, column=0, padx=10, pady=(8, 3), sticky="ew")
        self.file_preview = ctk.CTkTextbox(tab, wrap="none", corner_radius=8, font=ctk.CTkFont(size=12))
        self.file_preview.grid(row=4, column=0, padx=6, pady=(2, 4), sticky="nsew")
        self.file_preview.configure(state="disabled")
        self.open_file_button = ctk.CTkButton(tab, text="在系统中打开", height=30, state="disabled", command=self.open_selected_file)
        self.open_file_button.grid(row=5, column=0, padx=6, pady=(4, 8), sticky="ew")

    def _build_web(self):
        tab = self.web_tab
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(2, weight=1)
        bar = ctk.CTkFrame(tab, fg_color="transparent")
        bar.grid(row=0, column=0, padx=5, pady=(8, 4), sticky="ew")
        self.url_entry = ctk.CTkEntry(bar, placeholder_text="输入网页地址", height=34, corner_radius=9)
        self.url_entry.pack(fill="x", expand=True, side="left", padx=(0, 5))
        self.load_button = ctk.CTkButton(bar, text="打开", width=56, height=34, command=self.load_url)
        self.load_button.pack(side="right")
        nav = ctk.CTkFrame(tab, fg_color="transparent")
        nav.grid(row=1, column=0, padx=6, pady=(0, 5), sticky="ew")
        ctk.CTkButton(nav, text="‹", width=32, height=28, fg_color="transparent", command=self.go_back).pack(side="left")
        ctk.CTkButton(nav, text="›", width=32, height=28, fg_color="transparent", command=self.go_forward).pack(side="left")
        self.web_status = ctk.CTkLabel(nav, text="安全读取网页文本，不运行网页脚本", anchor="e", font=ctk.CTkFont(size=10), text_color=("#777777", "#aaaaaa"))
        self.web_status.pack(side="right", fill="x", expand=True)
        body = ctk.CTkFrame(tab, fg_color="transparent")
        body.grid(row=2, column=0, padx=6, pady=2, sticky="nsew")
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(1, weight=1)
        self.web_title = ctk.CTkLabel(body, text="", anchor="w", font=ctk.CTkFont(size=13, weight="bold"), wraplength=330)
        self.web_title.grid(row=0, column=0, padx=4, pady=(4, 6), sticky="ew")
        self.web_text = ctk.CTkTextbox(body, wrap="word", corner_radius=8, font=ctk.CTkFont(size=12))
        self.web_text.grid(row=1, column=0, sticky="nsew")
        self.web_text.configure(state="disabled")
        ctk.CTkLabel(tab, text="网页链接", anchor="w", font=ctk.CTkFont(size=11), text_color=("#777777", "#aaaaaa")).grid(row=3, column=0, padx=10, pady=(8, 2), sticky="ew")
        self.web_links = ctk.CTkScrollableFrame(tab, height=110, fg_color="transparent")
        self.web_links.grid(row=4, column=0, padx=6, pady=(0, 6), sticky="ew")
        self.web_links.grid_columnconfigure(0, weight=1)
        self.url_entry.bind("<Return>", lambda _: self.load_url())

    def _build_git(self):
        tab = self.git_tab
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(tab, text="只读查看本地 Git 状态，不会切换分支或提交。", anchor="w", wraplength=330, font=ctk.CTkFont(size=11), text_color=("#777777", "#aaaaaa")).grid(row=0, column=0, padx=10, pady=(8, 4), sticky="ew")
        self.git_text = ctk.CTkTextbox(tab, wrap="none", corner_radius=8, font=ctk.CTkFont(size=12))
        self.git_text.grid(row=1, column=0, padx=6, pady=4, sticky="nsew")
        self.git_text.configure(state="disabled")
        ctk.CTkButton(tab, text="刷新 Git 状态", height=32, command=self.refresh_git).grid(row=2, column=0, padx=6, pady=(4, 8), sticky="ew")

    def _build_queue(self):
        tab = self.queue_tab
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(tab, text="任务按顺序执行；可以取消尚未开始的单项。", anchor="w", wraplength=330, font=ctk.CTkFont(size=11), text_color=("#777777", "#aaaaaa")).grid(row=0, column=0, padx=10, pady=(8, 4), sticky="ew")
        self.queue_list = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        self.queue_list.grid(row=1, column=0, padx=6, pady=3, sticky="nsew")
        self.queue_list.grid_columnconfigure(0, weight=1)

    def _tab_changed(self):
        tab = self.tabs.get()
        if tab == "文件":
            self.refresh_files()
        elif tab == "Git":
            self.refresh_git()
        elif tab == "队列":
            self.refresh_queue()

    def sync_project(self):
        project = self.app._project()
        root = self.app._project_root(project).resolve()
        if project["id"] != self.current_project_id or root != self.project_root:
            self.current_project_id = project["id"]
            self.project_root = root
            self.current_dir = root
            self.selected_file = None
            self._set_file_preview("选择文件以预览内容。")
            self.refresh_files()

    def _set_file_preview(self, text):
        self.file_preview.configure(state="normal")
        self.file_preview.delete("1.0", "end")
        self.file_preview.insert("1.0", text)
        self.file_preview.configure(state="disabled")

    def refresh_files(self):
        self.sync_project_if_ready()
        if self.current_dir is None or self.project_root is None:
            return
        try:
            if not self.app._project().get("folder"):
                self.project_root.mkdir(parents=True, exist_ok=True)
            current = self.current_dir.resolve()
            if not current.is_relative_to(self.project_root) or _is_link(self.current_dir) or not current.is_dir():
                current = self.project_root
                self.current_dir = current
            entries = [item for item in current.iterdir() if not _is_link(item) and not is_private_name(item.name)]
            entries = [item for item in entries if item.name not in SKIP_DIRECTORIES or not item.is_dir()]
            entries.sort(key=lambda item: (not item.is_dir(), item.name.casefold()))
        except OSError as exc:
            entries = []
            self._set_file_preview(f"無法读取目录：{exc}")
        self.file_location.configure(text=self._relative(self.current_dir))
        for child in self.file_list.winfo_children():
            child.destroy()
        if not entries:
            ctk.CTkLabel(self.file_list, text="此目录没有可显示的文件。", anchor="w", text_color=("#777777", "#aaaaaa")).grid(row=0, column=0, padx=8, pady=12, sticky="ew")
        for index, path in enumerate(entries[:400]):
            label = ("▸  " if path.is_dir() else "   ") + path.name[:64]
            ctk.CTkButton(self.file_list, text=label, anchor="w", height=29, corner_radius=7, fg_color="transparent", text_color=("#3e3e3e", "#d7d7d7"), hover_color=("#e8e8e7", "#333333"), command=lambda item=path: self.open_entry(item)).grid(row=index, column=0, padx=2, pady=1, sticky="ew")

    def sync_project_if_ready(self):
        if getattr(self.app, "projects", None):
            self.sync_project()

    def _relative(self, path):
        try:
            relative = Path(path).resolve().relative_to(self.project_root)
            return self.project_root.name if str(relative) == "." else relative.as_posix()
        except (ValueError, OSError):
            return self.project_root.name

    def open_entry(self, path):
        if _is_link(path):
            return
        if path.is_dir():
            resolved = path.resolve()
            if resolved.is_relative_to(self.project_root):
                self.current_dir = resolved
                self.selected_file = None
                self.open_file_button.configure(state="disabled")
                self._set_file_preview("選擇文件以預覽內容。")
                self.refresh_files()
            return
        self.preview_file(path)

    def go_up(self):
        if self.current_dir and self.project_root and self.current_dir != self.project_root:
            self.current_dir = self.current_dir.parent
            self.selected_file = None
            self.open_file_button.configure(state="disabled")
            self.refresh_files()

    def preview_file(self, path):
        try:
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(self.project_root) or _is_link(path) or not resolved.is_file():
                raise ValueError("文件不在当前项目内")
            if is_private_name(resolved.name):
                raise ValueError("密钥与本机配置文件不在侧栏中预览")
            size = resolved.stat().st_size
            self.selected_file = resolved
            self.open_file_button.configure(state="normal")
            if resolved.suffix.lower() not in TEXT_EXTENSIONS:
                content = f"{resolved.name}\n文件大小：{size:,} 字节\n\n此文件类型不在文本预览范围内，可用系统关联程序打开。"
            elif size > MAX_PREVIEW_BYTES:
                content = f"{resolved.name}\n文件大小：{size:,} 字节\n\n超过 {MAX_PREVIEW_BYTES:,} 字节的预览上限。"
            else:
                content = resolved.read_text(encoding="utf-8-sig", errors="replace")
                self.file_location.configure(text=self._relative(resolved))
        except (OSError, ValueError, RuntimeError) as exc:
            self.selected_file = None
            self.open_file_button.configure(state="disabled")
            content = f"无法预览：{exc}"
        self._set_file_preview(content)

    def open_selected_file(self):
        if self.selected_file and self.selected_file.is_file() and self.selected_file.is_relative_to(self.project_root):
            self.app._open_local(self.selected_file)

    def load_url(self, url=None, add_history=True):
        if self.browser_busy:
            return
        target = (url if url is not None else self.url_entry.get()).strip()
        if not target:
            return
        if "://" not in target:
            target = "https://" + target
        parsed = urlsplit(target)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            self._show_web_error("只允许不含账号密码的 HTTP/HTTPS 网页地址。")
            return
        if add_history:
            self.browser_history = self.browser_history[:self.browser_index + 1]
            self.browser_history.append(target)
            self.browser_index = len(self.browser_history) - 1
        self.browser_request += 1
        request_id = self.browser_request
        self.browser_busy = True
        self.load_button.configure(state="disabled")
        self.web_status.configure(text="正在安全读取…")
        domains = {item.strip().lower() for item in self.app.vars["allowed_domains"].get().split(",") if item.strip()}
        threading.Thread(target=self._load_web_worker, args=(request_id, target, domains), daemon=True, name="changan-web-preview").start()

    def _load_web_worker(self, request_id, url, domains):
        try:
            response = safe_http_get(url, domains, max_bytes=500_000)
            content = response["content"]
            title, text, links = "", content, []
            if "<" in content and ">" in content:
                parser = _PageTextParser(response["url"])
                parser.feed(content)
                title = " ".join("".join(parser.title_parts).split())
                text = "\n".join(line.strip() for line in "".join(parser.text_parts).splitlines() if line.strip())
                links = parser.links[:80]
            if response.get("truncated"):
                text += "\n\n（网页内容已达到读取上限，后续内容未显示。）"
            result = (True, response.get("url", url), response.get("status", 200), title, text[:50000], links)
        except Exception as exc:
            result = (False, url, 0, "读取失败", str(exc)[:2000], [])
        self.app._ui_after(0, self._show_web_result, request_id, result)

    def _show_web_error(self, message):
        self.web_status.configure(text="地址无效")
        self.web_title.configure(text="无法打开网页")
        self._set_text(self.web_text, message)

    def _show_web_result(self, request_id, result):
        if request_id != self.browser_request or self.app.closing:
            return
        self.browser_busy = False
        self.load_button.configure(state="normal")
        ok, url, status, title, content, links = result
        self.url_entry.delete(0, "end")
        self.url_entry.insert(0, url)
        self.web_title.configure(text=title or url)
        self.web_status.configure(text=f"HTTP {status} · 文本视图" if ok else "读取失败")
        self._set_text(self.web_text, content or "网页没有可显示的文本。")
        for child in self.web_links.winfo_children():
            child.destroy()
        for index, (label, target) in enumerate(links):
            ctk.CTkButton(self.web_links, text=f"{label}  →", anchor="w", height=27, corner_radius=7, fg_color="transparent", text_color=("#4b4b4b", "#d0d0d0"), hover_color=("#e8e8e7", "#333333"), command=lambda value=target: self.load_url(value)).grid(row=index, column=0, padx=2, pady=1, sticky="ew")

    @staticmethod
    def _set_text(widget, value):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", value)
        widget.configure(state="disabled")

    def go_back(self):
        if self.browser_index > 0:
            self.browser_index -= 1
            self.load_url(self.browser_history[self.browser_index], add_history=False)

    def go_forward(self):
        if self.browser_index + 1 < len(self.browser_history):
            self.browser_index += 1
            self.load_url(self.browser_history[self.browser_index], add_history=False)

    def refresh_git(self):
        self.sync_project_if_ready()
        root = self.project_root
        if root is None:
            return
        self.git_request += 1
        request_id = self.git_request
        self._set_text(self.git_text, "正在读取本地 Git 状态…")
        threading.Thread(target=self._git_worker, args=(request_id, root), daemon=True, name="changan-git-status").start()

    def _git_worker(self, request_id, root):
        environment = os.environ.copy()
        environment["GIT_OPTIONAL_LOCKS"] = "0"
        command = ["git", "-c", "core.fsmonitor=false", "-c", "core.pager=cat", "status", "--short", "--branch"]
        try:
            result = subprocess.run(command, cwd=root, env=environment, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=8, check=False)
            if result.returncode:
                detail = result.stderr.strip()
                text = "当前项目文件夹还没有 Git 仓库。" if "not a git repository" in detail.lower() else detail or "读取 Git 状态失败。"
            else:
                text = result.stdout.strip() or "工作区干净，没有未提交变更。"
                stat = subprocess.run(["git", "-c", "core.fsmonitor=false", "diff", "--no-ext-diff", "--no-textconv", "--stat"], cwd=root, env=environment, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=8, check=False)
                if stat.stdout.strip():
                    text += "\n\n未暂存改动\n" + stat.stdout.strip()
                staged = subprocess.run(["git", "-c", "core.fsmonitor=false", "diff", "--cached", "--no-ext-diff", "--no-textconv", "--stat"], cwd=root, env=environment, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=8, check=False)
                if staged.stdout.strip():
                    text += "\n\n已暂存改动\n" + staged.stdout.strip()
                text = text[:30000]
        except (OSError, subprocess.SubprocessError) as exc:
            text = f"读取 Git 状态失败：{exc}"
        self.app._ui_after(0, self._show_git_result, request_id, text)

    def _show_git_result(self, request_id, text):
        if request_id == self.git_request and not self.app.closing:
            self._set_text(self.git_text, text)

    def refresh_queue(self):
        for child in self.queue_list.winfo_children():
            child.destroy()
        tasks = list(self.app.pending_tasks)
        if not tasks:
            ctk.CTkLabel(self.queue_list, text="当前没有等待任务。", anchor="w", text_color=("#777777", "#aaaaaa")).grid(row=0, column=0, padx=8, pady=12, sticky="ew")
            return
        for index, task in enumerate(tasks):
            project = next((item for item in self.app.projects if item["id"] == task.get("project_id")), {})
            chat = next((item for item in project.get("chats", []) if item["id"] == task.get("chat_id")), {})
            row = ctk.CTkFrame(self.queue_list, fg_color="transparent")
            row.grid(row=index, column=0, padx=3, pady=5, sticky="ew")
            row.grid_columnconfigure(0, weight=1)
            label = f"{project.get('name', '项目')} · {chat.get('title', '聊天')}\n{task.get('task', '')[:140]}"
            ctk.CTkLabel(row, text=label, anchor="w", justify="left", wraplength=245).grid(row=0, column=0, sticky="ew")
            ctk.CTkButton(row, text="取消", width=48, height=27, fg_color="transparent", text_color=("#737373", "#c8c8c8"), command=lambda item=task["id"]: self.app.cancel_pending_task(item)).grid(row=0, column=1, padx=(5, 0))
