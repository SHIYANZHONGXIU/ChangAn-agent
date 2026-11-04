"""项目文本编辑：保留编码/换行，保存前检测外部改动并复用文件版本备份。"""

from __future__ import annotations

import difflib
import hashlib
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk

from orchestrator import ToolExecutor
from workspace_panel import is_private_name


def project_file(root, path):
    executor = ToolExecutor(Path(root), "full access")
    target = executor._safe_path(str(path))
    relative = target.relative_to(executor.project_dir)
    if any(is_private_name(part) or part in {".git", ".agent-backups", ".venv", ".venv-workbench"} for part in relative.parts):
        raise ValueError("编辑器不打开密钥、内部配置或版本目录")
    return target


class EditorBuffer:
    def __init__(self, root, path, new=False):
        self.root = Path(root).resolve()
        self.path = project_file(root, path)
        if not new and not self.path.is_file():
            raise ValueError("请选择项目内的文本文件")
        self.reload()

    def reload(self):
        data = self.path.read_bytes() if self.path.exists() else b""
        if len(data) > 4_000_000 or b"\x00" in data:
            raise ValueError("仅支持 4 MB 以内、不含空字节的 UTF-8 文本")
        text = data.decode("utf-8-sig")
        self.bom = data.startswith(b"\xef\xbb\xbf")
        self.newline = "\r\n" if "\r\n" in text else "\n"
        self.content = text.replace("\r\n", "\n")
        self.digest = hashlib.sha256(data).hexdigest() if self.path.exists() else None
        return self.content

    def save(self, content):
        project_file(self.root, self.path)
        current = ToolExecutor._hash(self.path)
        if current != self.digest:
            raise RuntimeError("文件已被其他程序修改，未覆盖。请查看差异或重新载入后再保存。")
        output = ("\ufeff" if self.bom else "") + content.replace("\n", self.newline)
        result = ToolExecutor(self.root, "full access").run("write_file", {"path": str(self.path), "content": output})
        self.digest = ToolExecutor._hash(self.path)
        self.content = content
        return result

    def diff(self, content):
        disk = self.path.read_text(encoding="utf-8-sig").replace("\r\n", "\n") if self.path.exists() else ""
        return "".join(difflib.unified_diff(disk.splitlines(True), content.splitlines(True), fromfile="磁盘版本", tofile="编辑版本")) or "没有差异。"


class CodeEditor(ctk.CTkFrame):
    def __init__(self, master, root):
        super().__init__(master, fg_color="transparent")
        self.root = Path(root).resolve()
        self.buffers = {}
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", pady=5)
        for title, callback in (("打开文件", self.choose), ("新建", self.new), ("保存", self.save), ("差异", self.show_diff), ("重载", self.reload), ("关闭标签", self.close_tab)):
            ctk.CTkButton(bar, text=title, width=78, height=30, command=callback).pack(side="left", padx=3)
        search = ctk.CTkFrame(self, fg_color="transparent")
        search.grid(row=1, column=0, sticky="ew", pady=5)
        self.query = ctk.CTkEntry(search, placeholder_text="查找文本")
        self.query.pack(side="left", fill="x", expand=True, padx=3)
        self.replacement = ctk.CTkEntry(search, placeholder_text="替换为")
        self.replacement.pack(side="left", fill="x", expand=True, padx=3)
        ctk.CTkButton(search, text="下一个", width=70, command=self.find).pack(side="left", padx=3)
        ctk.CTkButton(search, text="替换选中", width=80, command=self.replace).pack(side="left", padx=3)
        self.query.bind("<Return>", lambda _: self.find())
        self.notebook = ttk.Notebook(self)
        self.notebook.grid(row=2, column=0, sticky="nsew")
        self.status = ctk.CTkLabel(self, text="打开项目内文件开始编辑 · Ctrl+S 保存 · Ctrl+F 查找 · 关闭前提示未保存内容", anchor="w")
        self.status.grid(row=3, column=0, sticky="ew", padx=5)

    def current(self):
        return self.buffers.get(self.notebook.select())

    def choose(self):
        path = filedialog.askopenfilename(parent=self, initialdir=self.root, title="打开项目文本文件")
        if path:
            self.open_file(path)

    def new(self):
        path = filedialog.asksaveasfilename(parent=self, initialdir=self.root, title="新建项目文本文件", defaultextension=".txt")
        if path:
            self.open_file(path, new=True)

    def open_file(self, path, new=False):
        try:
            path = project_file(self.root, path)
            for item in self.buffers.values():
                if item["buffer"].path == path:
                    self.notebook.select(item["frame"])
                    return
            buffer = EditorBuffer(self.root, path, new)
        except (OSError, UnicodeError, RuntimeError, ValueError) as exc:
            messagebox.showerror("无法打开", str(exc), parent=self)
            return
        frame = tk.Frame(self.notebook, bg="#202124")
        frame.grid_rowconfigure(0, weight=1)
        frame.grid_columnconfigure(1, weight=1)
        gutter = tk.Canvas(frame, width=52, bg="#26272a", highlightthickness=0)
        gutter.grid(row=0, column=0, sticky="ns")
        text = tk.Text(frame, wrap="none", undo=True, maxundo=1000, font=("Cascadia Code", 12), tabs=("32p",), bg="#202124", fg="#e6e6e6", insertbackground="#ffffff", selectbackground="#3c536e", relief="flat", padx=8, pady=8)
        text.grid(row=0, column=1, sticky="nsew")
        vertical = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        vertical.grid(row=0, column=2, sticky="ns")
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=text.xview)
        horizontal.grid(row=1, column=1, sticky="ew")
        text.configure(xscrollcommand=horizontal.set, yscrollcommand=lambda *args: (vertical.set(*args), self._lines(text, gutter)))
        text.insert("1.0", buffer.content)
        text.edit_reset()
        text.edit_modified(False)
        item = {"buffer": buffer, "text": text, "frame": frame, "gutter": gutter, "job": None}
        self.buffers[str(frame)] = item
        self.notebook.add(frame, text=path.name)
        self.notebook.select(frame)
        text.bind("<<Modified>>", lambda _: self._modified(item))
        text.bind("<Configure>", lambda _: self._lines(text, gutter))
        text.bind("<Control-s>", lambda _: (self.save(), "break")[1])
        text.bind("<Control-f>", lambda _: (self.query.focus_set(), "break")[1])
        text.bind("<Tab>", lambda _: (text.insert("insert", "    "), "break")[1])
        text.bind("<KeyRelease>", lambda _: self._position(item), add="+")
        text.bind("<ButtonRelease-1>", lambda _: self._position(item), add="+")
        text.tag_configure("keyword", foreground="#c792ea")
        text.tag_configure("string", foreground="#c3e88d")
        text.tag_configure("comment", foreground="#87939c")
        text.tag_configure("number", foreground="#f78c6c")
        text.tag_configure("search", background="#685325")
        self._highlight(item)
        self._position(item)

    def _position(self, item):
        self.status.configure(text=f"{item['buffer'].path.relative_to(self.root)} · UTF-8 · {'CRLF' if item['buffer'].newline == chr(13)+chr(10) else 'LF'} · 行:列 {item['text'].index('insert')}")

    def _lines(self, text, canvas):
        if not text.winfo_exists():
            return
        canvas.delete("all")
        line = text.index("@0,0")
        while True:
            info = text.dlineinfo(line)
            if info is None:
                break
            canvas.create_text(45, info[1] + info[3] / 2, text=line.split(".")[0], anchor="e", fill="#8b8c90", font=("Cascadia Code", 11))
            line = text.index(line + "+1line")

    def _modified(self, item):
        if not item["text"].edit_modified():
            return
        item["text"].edit_modified(False)
        dirty = item["text"].get("1.0", "end-1c") != item["buffer"].content
        self.notebook.tab(item["frame"], text=item["buffer"].path.name + (" ●" if dirty else ""))
        if item["job"]:
            self.after_cancel(item["job"])
        item["job"] = self.after(350, lambda: self._highlight(item))

    def _highlight(self, item):
        item["job"] = None
        text = item["text"]
        for tag in ("keyword", "string", "comment", "number"):
            text.tag_remove(tag, "1.0", "end")
        try:
            from pygments import lex
            from pygments.lexers import get_lexer_for_filename
            lexer = get_lexer_for_filename(item["buffer"].path.name, stripnl=False, ensurenl=False)
            offset = 0
            # shortcut: 高亮仅处理前100000字符，大文件改用增量解析时再扩大。
            for token, value in lex(text.get("1.0", "end-1c")[:100000], lexer):
                kind = str(token)
                tag = next((name for name, group in (("keyword", "Keyword"), ("string", "String"), ("comment", "Comment"), ("number", "Number")) if group in kind), None)
                if tag:
                    text.tag_add(tag, f"1.0+{offset}c", f"1.0+{offset+len(value)}c")
                offset += len(value)
        except (ImportError, ValueError):
            pass

    def save(self):
        item = self.current()
        if item:
            try:
                item["buffer"].save(item["text"].get("1.0", "end-1c"))
                self.notebook.tab(item["frame"], text=item["buffer"].path.name)
                self.status.configure(text="已保存，修改前版本已进入项目备份记录。")
                return True
            except (OSError, RuntimeError, ValueError) as exc:
                messagebox.showerror("未保存", str(exc), parent=self)
        return False

    def show_diff(self):
        item = self.current()
        if item:
            try:
                content = item["buffer"].diff(item["text"].get("1.0", "end-1c"))
            except (OSError, UnicodeError) as exc:
                content = str(exc)
            window = ctk.CTkToplevel(self)
            window.title("未保存改动")
            window.geometry("850x620")
            box = ctk.CTkTextbox(window, wrap="none", font=("Cascadia Code", 12))
            box.pack(fill="both", expand=True, padx=14, pady=14)
            box.insert("1.0", content)
            box.configure(state="disabled")

    def reload(self):
        item = self.current()
        if item and (item["text"].get("1.0", "end-1c") == item["buffer"].content or messagebox.askyesno("重新载入", "放弃此标签未保存的内容并读取磁盘版本？", parent=self)):
            try:
                content = item["buffer"].reload()
                item["text"].delete("1.0", "end")
                item["text"].insert("1.0", content)
                item["text"].edit_reset()
                self._highlight(item)
            except (OSError, UnicodeError, ValueError) as exc:
                messagebox.showerror("无法载入", str(exc), parent=self)

    def close_tab(self):
        item = self.current()
        if item:
            if item["text"].get("1.0", "end-1c") != item["buffer"].content:
                answer = messagebox.askyesnocancel("未保存的文件", f"保存 {item['buffer'].path.name} 的修改？", parent=self)
                if answer is None or answer and not self.save():
                    return False
            if item["job"]:
                self.after_cancel(item["job"])
            self.notebook.forget(item["frame"])
            del self.buffers[str(item["frame"])]
            item["frame"].destroy()
        return True

    def can_close(self):
        for key in list(self.buffers):
            self.notebook.select(key)
            if not self.close_tab():
                return False
        return True

    def find(self):
        item, query = self.current(), self.query.get()
        if item and query:
            text = item["text"]
            text.tag_remove("search", "1.0", "end")
            position = text.search(query, "insert+1c", stopindex="end") or text.search(query, "1.0", stopindex="end")
            if position:
                end = f"{position}+{len(query)}c"
                text.tag_add("search", position, end)
                text.tag_remove("sel", "1.0", "end")
                text.tag_add("sel", position, end)
                text.mark_set("insert", position)
                text.see(position)

    def replace(self):
        item = self.current()
        if item:
            text = item["text"]
            if text.tag_ranges("sel") and text.get("sel.first", "sel.last") == self.query.get():
                text.replace("sel.first", "sel.last", self.replacement.get())
            self.find()
