"""浏览器、编辑器、并行任务和 MCP 的统一桌面工作台。"""

from __future__ import annotations

import json
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk

from app_runtime import redact
from code_editor import CodeEditor
from mcp_integration import MCPRegistry, MCPTools, validate_config
from network_safety import normalize_allowed_domains
from parallel_agents import ParallelAgents
from persistence import read_json
from visual_browser import BrowserHost


LABELS = {"queued": "排队", "preparing": "准备快照", "running": "运行中", "completed": "已完成", "failed": "未通过核验", "error": "异常，可恢复", "stopped": "已停止", "interrupted": "已中断"}


def demo_server_config(executable, module_dir, frozen=False):
    return {"transport": "stdio", "command": str(executable),
            "args": ["--changan-mcp-demo"] if frozen else [str(Path(module_dir) / "plugins" / "example_server.py")],
            "enabled": False, "timeout": 20}


class Workbench(ctk.CTkToplevel):
    def __init__(self, app, data_dir, root):
        super().__init__(app)
        self.app, self.root, self.data_dir = app, Path(root).resolve(), Path(data_dir)
        self.title("长安 · 完整工作台")
        self.geometry("1180x800")
        self.minsize(950, 620)
        self.protocol("WM_DELETE_WINDOW", self.hide)
        self.closed = False
        self.events = queue.SimpleQueue()
        self.approvals = queue.SimpleQueue()
        self.mcp_stop = threading.Event()
        self.jobs = set()
        self.mcp_busy = False
        self.task_id = None
        self.streams = {}
        self.registry = MCPRegistry(self.data_dir / "config" / "mcp_servers.json")
        self.mcp = MCPTools(self.registry, self.root, self.mcp_stop.is_set)
        self.manager = ParallelAgents(self.data_dir, approval=self.approve, notify=lambda *args: self.events.put(("parallel", args)), memory_store=app.memory_store, project_id=app._project()["id"])
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=18, pady=(14, 0))
        ctk.CTkLabel(header, text="完整工作台", font=ctk.CTkFont(size=22, weight="bold")).pack(side="left")
        ctk.CTkLabel(header, text=str(self.root), anchor="w", text_color=("#666666", "#aaaaaa")).pack(side="left", padx=20)
        ctk.CTkButton(header, text="返回聊天", width=90, command=self.hide).pack(side="right")
        self.tabs = ctk.CTkTabview(self, command=self.changed)
        self.tabs.grid(row=1, column=0, sticky="nsew", padx=14, pady=(4, 14))
        for name in ("浏览器", "代码编辑器", "并行 Agent", "MCP 插件"):
            self.tabs.add(name).grid_columnconfigure(0, weight=1)
            self.tabs.tab(name).grid_rowconfigure(0, weight=1)
        self.browser = BrowserHost(self.tabs.tab("浏览器"), normalize_allowed_domains(app.vars["allowed_domains"].get()))
        self.browser.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        self.editor = CodeEditor(self.tabs.tab("代码编辑器"), self.root)
        self.editor.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        self._build_parallel()
        self._build_mcp()
        self.refresh_tasks()
        self.refresh_servers()
        self.poll_job = self.after(100, self.poll)

    def changed(self):
        if self.tabs.get() == "浏览器":
            self.after(80, self.browser.resize)

    def show(self, tab=None, path=None):
        self.deiconify()
        self.lift()
        if tab:
            self.tabs.set(tab)
        if path:
            self.tabs.set("代码编辑器")
            self.editor.open_file(path)
        self.after(100, self.browser.resize)

    def hide(self):
        self.withdraw()

    def _build_parallel(self):
        tab = self.tabs.tab("并行 Agent")
        pane = ctk.CTkFrame(tab, fg_color="transparent")
        pane.grid(row=0, column=0, sticky="nsew")
        pane.grid_columnconfigure(0, weight=1)
        pane.grid_rowconfigure(2, weight=1)
        ctk.CTkLabel(pane, text="最多同时运行3个独立任务，每项使用独立项目快照；完成后预览差异并合并。预算按每个任务分别计算。", anchor="w", wraplength=1020).grid(row=0, column=0, sticky="ew", padx=8, pady=8)
        inputs = ctk.CTkFrame(pane, fg_color="transparent")
        inputs.grid(row=1, column=0, sticky="ew", padx=8, pady=5)
        self.task_input = ctk.CTkTextbox(inputs, height=76, corner_radius=12)
        self.task_input.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(inputs, text="添加并行任务", command=self.submit, width=120).pack(side="right", padx=(12, 0))
        split = tk.PanedWindow(pane, orient="horizontal", sashwidth=6, bg="#303033", bd=0)
        split.grid(row=2, column=0, sticky="nsew", padx=8, pady=8)
        left, right = ctk.CTkFrame(split), ctk.CTkFrame(split)
        split.add(left, width=330, minsize=260)
        split.add(right, minsize=400)
        self.task_tree = ttk.Treeview(left, columns=("status",), show="tree headings", selectmode="extended")
        self.task_tree.heading("#0", text="任务")
        self.task_tree.heading("status", text="状态")
        self.task_tree.column("#0", width=190)
        self.task_tree.column("status", width=100)
        self.task_tree.pack(fill="both", expand=True, padx=6, pady=6)
        self.task_tree.bind("<<TreeviewSelect>>", self.select_task)
        self.parallel_status = ctk.CTkLabel(right, text="选择任务查看运行结果", anchor="w", wraplength=660)
        self.parallel_status.pack(fill="x", padx=10, pady=6)
        buttons = ctk.CTkFrame(right, fg_color="transparent")
        buttons.pack(fill="x", padx=5)
        for row, commands in enumerate((("停止", self.stop_task), ("恢复", self.resume_task), ("预览差异", self.preview_merge), ("对照所选", self.compare_selected), ("合并变更", self.merge), ("打开产物", self.open_task_files))):
            label, callback = commands
            ctk.CTkButton(buttons, text=label, width=88, height=30, command=callback).grid(row=row // 3, column=row % 3, padx=3, pady=3, sticky="ew")
        self.task_detail = ctk.CTkTextbox(right, wrap="word", font=("Microsoft YaHei UI", 13))
        self.task_detail.pack(fill="both", expand=True, padx=8, pady=8)
        self.task_detail.configure(state="disabled")

    def submit(self):
        task = self.task_input.get("1.0", "end-1c").strip()
        if not task:
            return
        settings = self.app._validated_settings()
        if settings is None:
            return
        try:
            if settings["permission_mode"] == "full access" and not messagebox.askyesno("并行任务 · Full access", "任务在独立快照中运行，但命令和第三方MCP仍拥有本机用户权限，并非系统沙箱。确认按当前权限运行此任务？", parent=self):
                return
            task_id = self.manager.submit(self.root, task, settings, project_id=self.app._project()["id"])
            self.task_input.delete("1.0", "end")
            self.refresh_tasks()
            self.task_tree.selection_set(task_id)
            self.select_task()
        except (OSError, ValueError, RuntimeError) as exc:
            messagebox.showerror("无法创建任务", str(exc), parent=self)

    def refresh_tasks(self):
        for row in self.manager.rows():
            value = (LABELS.get(row["status"], row["status"]),)
            title = row["task"].replace("\n", " ")[:32]
            if self.task_tree.exists(row["id"]):
                self.task_tree.item(row["id"], text=title, values=value)
            else:
                self.task_tree.insert("", 0, iid=row["id"], text=title, values=value)
        if self.task_id in self.manager.tasks:
            row = self.manager.tasks[self.task_id]
            self.parallel_status.configure(text=f"{LABELS.get(row['status'], row['status'])} · {row.get('stage', '')}\n来源：{row['source']} · {row.get('elapsed', 0):.1f} 秒")

    @staticmethod
    def _text(box, content):
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.insert("1.0", str(content))
        box.configure(state="disabled")

    def select_task(self, event=None):
        selection = self.task_tree.selection()
        if not selection:
            return
        self.task_id = selection[0]
        row = self.manager.tasks[self.task_id]
        root = self.manager.directory / self.task_id
        result = read_json(root / "result.json", {})
        content = result.get("final") or self.streams.get(self.task_id)
        if not content and (root / "events.jsonl").exists():
            content = (root / "events.jsonl").read_text(encoding="utf-8", errors="replace")[-60000:]
        self._text(self.task_detail, content or row["stage"])
        self.refresh_tasks()

    def compare_selected(self):
        task_ids = list(self.task_tree.selection())
        try:
            comparison = self.manager.compare(task_ids)
        except (OSError, ValueError, RuntimeError) as exc:
            messagebox.showerror("无法对照任务", str(exc), parent=self)
            return
        previous = getattr(self, "compare_window", None)
        if previous is not None and previous.winfo_exists():
            previous.destroy()
        window = ctk.CTkToplevel(self)
        self.compare_window = window
        window.title("并行任务对照")
        window.geometry("1100x720")
        window.minsize(900, 560)
        window.transient(self)
        window.grid_columnconfigure(0, weight=1)
        window.grid_rowconfigure(1, weight=1)
        summaries = "    ·    ".join(f"{item['task'][:26]} · {LABELS.get(item['status'], item['status'])} · {item['change_count']} 个文件" for item in comparison["tasks"])
        ctk.CTkLabel(window, text=summaries, anchor="w", justify="left", wraplength=1040, text_color=("#555555", "#d0d0d0")).grid(row=0, column=0, padx=18, pady=(16, 8), sticky="ew")
        body = ctk.CTkFrame(window, fg_color="transparent")
        body.grid(row=1, column=0, padx=14, pady=4, sticky="nsew")
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(0, weight=1)
        columns = tuple(f"task_{index}" for index in range(len(comparison["tasks"])))
        matrix = ttk.Treeview(body, columns=columns, show="tree headings", selectmode="browse", height=12)
        matrix.heading("#0", text="文件")
        matrix.column("#0", width=360)
        for index, task in enumerate(comparison["tasks"]):
            key = columns[index]
            matrix.heading(key, text=task["task"][:22] or f"任务 {index + 1}")
            matrix.column(key, width=190, anchor="w")
        matrix.grid(row=0, column=0, sticky="nsew")
        detail = ctk.CTkTextbox(body, wrap="none", height=240, font=("Cascadia Code", 12))
        detail.grid(row=1, column=0, pady=(8, 0), sticky="nsew")
        detail.configure(state="disabled")
        file_map = {}
        for index, item in enumerate(comparison["files"]):
            values = []
            for task in comparison["tasks"]:
                change = item["changes"].get(task["id"])
                if not change:
                    values.append("未改")
                else:
                    label = {"added": "新增", "modified": "修改", "deleted": "删除"}.get(change["change"], change["change"])
                    values.append(label + (" · 冲突" if change["conflict"] else ""))
            matrix.insert("", "end", iid=f"file-{index}", text=item["path"], values=values)
            file_map[f"file-{index}"] = item

        def show_file(_=None):
            keys = matrix.selection()
            item = file_map.get(keys[0]) if keys else None
            parts = []
            if item:
                for task in comparison["tasks"]:
                    change = item["changes"].get(task["id"])
                    if not change:
                        continue
                    parts.append(f"\n{'=' * 76}\n{task['task'][:120]} · {LABELS.get(task['status'], task['status'])}\n")
                    diff = self.manager.diff(task["id"], item["path"])
                    parts.append(diff[:60000] + ("\n…差异预览已截断。" if len(diff) > 60000 else ""))
                if not parts:
                    parts.append("所选文件没有变更。")
            self._text(detail, "\n".join(parts) if parts else "选择一个文件查看各任务差异。")

        matrix.bind("<<TreeviewSelect>>", show_file)
        ctk.CTkButton(window, text="关闭", width=82, command=window.destroy).grid(row=2, column=0, padx=18, pady=(8, 14), sticky="e")
        if not comparison["files"]:
            self._text(detail, "所选任务没有文件变更。")
        window.after(80, window.lift)

    def stop_task(self):
        if self.task_id:
            self.manager.stop(self.task_id)

    def resume_task(self):
        if self.task_id:
            settings = self.app._validated_settings()
            if settings is not None:
                try:
                    self.manager.resume(self.task_id, settings)
                except (OSError, ValueError, RuntimeError) as exc:
                    messagebox.showerror("无法恢复", str(exc), parent=self)

    def preview_merge(self):
        if self.task_id:
            try:
                self._text(self.task_detail, self.manager.diff(self.task_id))
            except (OSError, ValueError, RuntimeError) as exc:
                messagebox.showerror("无法预览", str(exc), parent=self)

    def merge(self):
        if not self.task_id:
            return
        try:
            changes = self.manager.changes(self.task_id)
            if not changes:
                messagebox.showinfo("合并", "没有文件变更", parent=self)
                return
            self._text(self.task_detail, self.manager.diff(self.task_id))
            detail = "\n".join(f"{item['change']}: {item['path']}" for item in changes[:30])
            if messagebox.askyesno("确认合并到原项目", f"将合并 {len(changes)} 个文件变更，包含删除时也会保留修改前备份。\n\n{detail}\n\n确认合并？", parent=self):
                applied = self.manager.merge(self.task_id)
                messagebox.showinfo("合并完成", f"已合并 {len(applied)} 个文件；原版本在项目 .agent-backups 中。", parent=self)
        except (OSError, ValueError, RuntimeError) as exc:
            messagebox.showerror("合并未完成", str(exc) + "\n若中途失败，请查看任务目录 merge.json 中的逐文件记录。", parent=self)

    def open_task_files(self):
        if self.task_id:
            self.app._open_local(self.manager.directory / self.task_id)

    def approve(self, task_id, name, args, stop):
        event, response = threading.Event(), {"allowed": False}
        self.approvals.put((task_id, name, args, stop, event, response))
        while not event.wait(.05):
            if stop.is_set() or self.closed or self.app.closing:
                return False
        return response["allowed"] and not stop.is_set()

    def _build_mcp(self):
        tab = self.tabs.tab("MCP 插件")
        pane = ctk.CTkFrame(tab, fg_color="transparent")
        pane.grid(row=0, column=0, sticky="nsew")
        pane.grid_columnconfigure((0, 1), weight=1)
        pane.grid_rowconfigure(2, weight=1)
        ctk.CTkLabel(pane, text="MCP 服务默认停用。stdio 会运行本机程序；远程服务可能执行外部操作。只启用可信服务，密钥使用环境变量引用。", wraplength=1050, anchor="w").grid(row=0, column=0, columnspan=2, sticky="ew", padx=8, pady=8)
        controls = ctk.CTkFrame(pane, fg_color="transparent")
        controls.grid(row=1, column=0, columnspan=2, sticky="ew", padx=5)
        for label, callback in (("导入配置", self.import_mcp), ("导出配置", self.export_mcp), ("添加本机演示", self.add_demo), ("保存配置", self.save_mcp)):
            ctk.CTkButton(controls, text=label, width=110, height=30, command=callback).pack(side="left", padx=4, pady=5)
        self.config_box = ctk.CTkTextbox(pane, wrap="none", font=("Cascadia Code", 12))
        self.config_box.grid(row=2, column=0, sticky="nsew", padx=8, pady=8)
        self.config_box.insert("1.0", json.dumps(self.registry.load(), ensure_ascii=False, indent=2))
        right = ctk.CTkFrame(pane, fg_color="transparent")
        right.grid(row=2, column=1, sticky="nsew", padx=8, pady=8)
        self.server_choice = ctk.CTkOptionMenu(right, values=["暂无服务"])
        self.server_choice.pack(fill="x", pady=(0, 6))
        buttons = ctk.CTkFrame(right, fg_color="transparent")
        buttons.pack(fill="x")
        for label, callback in (("启用", self.enable_mcp), ("发现能力", self.discover), ("停用", self.disable_mcp)):
            ctk.CTkButton(buttons, text=label, width=90, height=28, command=callback).pack(side="left", padx=3, pady=4)
        self.capabilities = ttk.Treeview(right, columns=("type",), show="tree headings", height=6, selectmode="browse")
        self.capabilities.heading("#0", text="工具 / 资源 / 提示")
        self.capabilities.heading("type", text="类型")
        self.capabilities.column("type", width=70)
        self.capabilities.pack(fill="x", pady=5)
        self.capabilities.bind("<<TreeviewSelect>>", self.select_capability)
        self.capability_data = {}
        self.arguments = ctk.CTkEntry(right, placeholder_text='参数 JSON，例如 {"a":1,"b":2}')
        self.arguments.pack(fill="x", pady=5)
        self.arguments.insert(0, "{}")
        ctk.CTkButton(right, text="调用选中项", command=self.call_capability).pack(anchor="w", pady=4)
        self.mcp_output = ctk.CTkTextbox(right, wrap="word", font=("Cascadia Code", 12))
        self.mcp_output.pack(fill="both", expand=True, pady=5)
        self.mcp_output.configure(state="disabled")

    def config(self):
        return validate_config(json.loads(self.config_box.get("1.0", "end-1c")))

    def refresh_servers(self):
        config = self.registry.load()["mcpServers"]
        values = list(config) or ["暂无服务"]
        old = self.server_choice.get()
        self.server_choice.configure(values=values)
        self.server_choice.set(old if old in values else values[0])

    def save_mcp(self):
        try:
            config = self.config()
            old = self.registry.load()["mcpServers"]
            risky = [name for name, item in config["mcpServers"].items() if item["enabled"] and item != old.get(name)]
            if risky and not messagebox.askyesno("启用第三方 MCP", "即将启用或修改服务：" + "、".join(risky) + "\n这些服务可能运行本机程序、读取文件或访问网络。确认信任配置来源？", parent=self):
                return False
            self.registry.save(config)
            self.mcp.close()
            self.mcp = MCPTools(self.registry, self.root, self.mcp_stop.is_set)
            self._set_config(config)
            self.refresh_servers()
            self._text(self.mcp_output, "配置已保存。已启用的服务将在下次主聊天或并行任务中可用；当前运行会话保持原配置。")
            return True
        except (OSError, ValueError, RuntimeError) as exc:
            messagebox.showerror("配置未保存", redact(str(exc)), parent=self)
            return False

    def _set_config(self, document):
        self.config_box.delete("1.0", "end")
        self.config_box.insert("1.0", json.dumps(document, ensure_ascii=False, indent=2))

    def import_mcp(self):
        path = filedialog.askopenfilename(parent=self, title="导入 MCP 配置（导入后全部停用）", filetypes=[("JSON", "*.json")])
        if path:
            try:
                if Path(path).stat().st_size > 200000:
                    raise ValueError("配置文件超过200KB")
                document = validate_config(json.loads(Path(path).read_text(encoding="utf-8-sig")))
                for item in document["mcpServers"].values():
                    item["enabled"] = False
                self._set_config(document)
            except (OSError, ValueError) as exc:
                messagebox.showerror("无法导入", str(exc), parent=self)

    def export_mcp(self):
        try:
            document = self.config()
            path = filedialog.asksaveasfilename(parent=self, title="导出 MCP 配置（不含实际环境变量密钥）", defaultextension=".json")
            if path:
                from persistence import write_json
                write_json(Path(path), document)
        except (OSError, ValueError) as exc:
            messagebox.showerror("无法导出", str(exc), parent=self)

    def add_demo(self):
        try:
            document = self.config()
            document["mcpServers"]["changan-demo"] = demo_server_config(sys.executable, Path(__file__).parent, getattr(sys, "frozen", False))
            self._set_config(document)
            self.save_mcp()
        except (OSError, ValueError) as exc:
            messagebox.showerror("无法添加", str(exc), parent=self)

    def enable_mcp(self):
        try:
            document = self.registry.load()
            document["mcpServers"][self.server_choice.get()]["enabled"] = True
            self._set_config(document)
            self.save_mcp()
        except KeyError:
            messagebox.showinfo("没有服务", "请先导入或添加配置", parent=self)

    def disable_mcp(self):
        try:
            document = self.registry.load()
            name = self.server_choice.get()
            document["mcpServers"][name]["enabled"] = False
            self._set_config(document)
            self.save_mcp()
        except KeyError:
            pass

    def background_mcp(self, work, callback):
        if self.mcp_busy:
            messagebox.showinfo("服务忙", "请等待当前 MCP 请求结束", parent=self)
            return
        self.mcp_busy = True
        self._text(self.mcp_output, "正在连接服务…")
        def run():
            try:
                result = work()
                self.events.put(("mcp", (callback, result, None)))
            except Exception as exc:
                self.events.put(("mcp", (callback, None, redact(str(exc)))))
        threading.Thread(target=run, daemon=True, name="changan-mcp-ui").start()

    def discover(self):
        name = self.server_choice.get()
        def discover():
            output = {}
            for category in ("tools", "resources", "prompts"):
                try:
                    output[category] = self.mcp.list(name, category)[category]
                except Exception as exc:
                    output[category] = []
                    output[category + "_error"] = redact(str(exc))
            return output
        self.background_mcp(discover, self.show_capabilities)

    def show_capabilities(self, document):
        self.capabilities.delete(*self.capabilities.get_children())
        self.capability_data.clear()
        for category, label in (("tools", "工具"), ("resources", "资源"), ("prompts", "提示")):
            for item in document.get(category, []):
                key = self.capabilities.insert("", "end", text=item.get("name", item.get("uri", "")), values=(label,))
                self.capability_data[key] = (category, item, self.server_choice.get())
        self._text(self.mcp_output, json.dumps(document, ensure_ascii=False, indent=2))

    def select_capability(self, event=None):
        selection = self.capabilities.selection()
        if selection:
            self._text(self.mcp_output, json.dumps(self.capability_data[selection[0]][1], ensure_ascii=False, indent=2))

    def call_capability(self):
        selection = self.capabilities.selection()
        if not selection:
            return
        category, item, server = self.capability_data[selection[0]]
        try:
            arguments = json.loads(self.arguments.get() or "{}")
            if not isinstance(arguments, dict):
                raise ValueError("参数应为JSON对象")
        except ValueError as exc:
            messagebox.showerror("参数无效", str(exc), parent=self)
            return
        if not messagebox.askyesno("确认调用 MCP", f"服务：{server}\n项目：{self.root}\n条目：{item.get('name', item.get('uri'))}\n参数：{redact(str(arguments))[:2000]}\n\n确认按第三方服务实际权限调用？", parent=self):
            return
        if category == "tools":
            method, args = "mcp_call", {"server": server, "tool": item["name"], "arguments": arguments}
        elif category == "resources":
            method, args = "mcp_read_resource", {"server": server, "uri": item["uri"]}
        else:
            method, args = "mcp_get_prompt", {"server": server, "prompt": item["name"], "arguments": arguments}
        self.background_mcp(lambda: self.mcp.run(method, args, lambda *_: None), lambda result: self._text(self.mcp_output, json.dumps(result, ensure_ascii=False, indent=2)))

    def poll(self):
        if self.closed:
            return
        for _ in range(200):
            try:
                kind, args = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "mcp":
                callback, result, error = args
                self.mcp_busy = False
                if error:
                    self._text(self.mcp_output, error)
                else:
                    callback(result)
            else:
                task_id, kind, value = args
                if kind == "delta":
                    self.streams[task_id] = (self.streams.get(task_id, "") + str(value))[-60000:]
                elif kind != "state":
                    self.streams[task_id] = (self.streams.get(task_id, "") + f"\n[{kind}] {redact(str(value))}\n")[-60000:]
                if task_id == self.task_id and kind != "state":
                    self._text(self.task_detail, self.streams.get(task_id, ""))
                    self.task_detail.see("end")
        self.refresh_tasks()
        try:
            task_id, name, args, stop, event, response = self.approvals.get_nowait()
            if not stop.is_set():
                self.deiconify()
                response["allowed"] = messagebox.askyesno("并行 Agent · 操作审批", f"任务：{self.manager.tasks[task_id]['task'][:100]}\n工具：{name}\n\n{redact(json.dumps(args, ensure_ascii=False, indent=2))[:5000]}\n\n允许本次操作？", parent=self)
            event.set()
        except queue.Empty:
            pass
        if not self.closed:
            self.poll_job = self.after(100, self.poll)

    def can_close(self):
        return self.editor.can_close()

    def close(self):
        self.closed = True
        if self.poll_job:
            self.after_cancel(self.poll_job)
        self.manager.close()
        self.mcp_stop.set()
        self.mcp.close()
        self.browser.close()
        self.destroy()
