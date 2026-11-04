"""确认式记忆管理与自动候选审核；不扫描历史聊天。"""

import sqlite3
import tkinter as tk
from tkinter import messagebox

import customtkinter as ctk

from long_term_memory import KINDS
from persistence import read_json


class MemoryPanel(ctk.CTkToplevel):
    def __init__(self, app, store, project, source=None):
        super().__init__(app)
        self.app, self.store, self.project = app, store, project
        self.record = None
        self.candidate = self.candidate_target = None
        self.reference_ids = {value for value in (source or {}).get("memory_ids", []) if isinstance(value, str)}
        self.source = {} if self.reference_ids else (source or {})
        self.title("长安 · 记忆")
        self.geometry("920x720")
        self.minsize(800, 620)
        self.transient(app)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        heading = ctk.CTkFrame(self, fg_color="transparent")
        heading.grid(row=0, column=0, padx=28, pady=(24, 8), sticky="ew")
        ctk.CTkLabel(heading, text="记忆", font=ctk.CTkFont(size=24, weight="bold")).pack(side="left")
        ctk.CTkButton(heading, text="＋ 新建记忆", width=112, command=self.new_record).pack(side="right")
        self.candidate_count = ctk.CTkLabel(heading, text="", text_color=("#777777", "#bbbbbb"))
        self.candidate_count.pack(side="left", padx=18)
        self.search_var = tk.StringVar()
        toolbar = ctk.CTkFrame(self, fg_color="transparent")
        toolbar.grid(row=1, column=0, padx=28, pady=8, sticky="ew")
        search = ctk.CTkEntry(toolbar, placeholder_text="搜索记忆…", height=38, corner_radius=12)
        search.pack(side="left", fill="x", expand=True)
        self.view_var = tk.StringVar(value="本次引用" if self.reference_ids else "已确认")
        ctk.CTkSegmentedButton(toolbar, values=["已确认", "待确认", "本次引用"], variable=self.view_var, command=self.change_view).pack(side="right", padx=(14, 0))
        search.bind("<KeyRelease>", lambda _: self.search_var.set(search.get()))
        self.search_var.trace_add("write", lambda *_: self.refresh())
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=2, column=0, padx=22, pady=(6, 12), sticky="nsew")
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=1)
        self.records = ctk.CTkScrollableFrame(body, width=220, corner_radius=14)
        self.records.grid(row=0, column=0, padx=(0, 14), sticky="nsew")
        editor = ctk.CTkFrame(body, fg_color="transparent")
        editor.grid(row=0, column=1, sticky="nsew")
        editor.grid_columnconfigure(0, weight=1)
        editor.grid_rowconfigure(5, weight=1)
        ctk.CTkLabel(editor, text="范围", anchor="w").grid(row=0, column=0, sticky="w")
        self.scope_var = tk.StringVar(value="当前项目")
        self.scope = ctk.CTkSegmentedButton(editor, values=["当前项目", "通用"], variable=self.scope_var)
        self.scope.grid(row=1, column=0, pady=(4, 12), sticky="ew")
        row = ctk.CTkFrame(editor, fg_color="transparent")
        row.grid(row=2, column=0, sticky="ew")
        self.kind_var = tk.StringVar(value="事实")
        ctk.CTkOptionMenu(row, variable=self.kind_var, values=list(KINDS.values()), width=110).pack(side="left", padx=(0, 10))
        self.title_var = tk.StringVar()
        ctk.CTkEntry(row, textvariable=self.title_var, placeholder_text="简短标题，例如：回答风格", height=34).pack(side="left", fill="x", expand=True)
        self.scope_caption = ctk.CTkLabel(editor, text="项目：" + project.get("name", ""), anchor="w", text_color=("#7c7c7c", "#afafaf"), font=ctk.CTkFont(size=11))
        self.scope_caption.grid(row=3, column=0, pady=(10, 2), sticky="w")
        ctk.CTkLabel(editor, text="确认后生效的内容", anchor="w").grid(row=4, column=0, sticky="w")
        self.content = ctk.CTkTextbox(editor, wrap="word", corner_radius=14, border_width=1, font=ctk.CTkFont(size=14))
        self.content.grid(row=5, column=0, pady=(6, 10), sticky="nsew")
        self.provenance = ctk.CTkLabel(editor, text="手动确认", anchor="w", justify="left", wraplength=520, font=ctk.CTkFont(size=11), text_color=("#7f7f7f", "#b4b4b4"))
        self.provenance.grid(row=6, column=0, sticky="ew")
        actions = ctk.CTkFrame(editor, fg_color="transparent")
        actions.grid(row=7, column=0, pady=(14, 0), sticky="ew")
        self.delete_btn = ctk.CTkButton(actions, text="删除", width=65, fg_color=("#e7e7e7", "#3a3a3a"), text_color=("#865a47", "#e6b6a1"), command=self.delete_record)
        self.delete_btn.pack(side="left")
        self.forget_btn = ctk.CTkButton(actions, text="删除来源及其记忆", width=130, fg_color="transparent", text_color=("#a15d4d", "#e6b6a1"), command=self.forget_source_chat)
        self.forget_btn.pack(side="left", padx=(8, 0))
        ctk.CTkButton(actions, text="查看来源", width=85, fg_color="transparent", text_color=("#767676", "#c4c4c4"), command=self.show_source).pack(side="left", padx=8)
        self.save_btn = ctk.CTkButton(actions, text="确认保存", width=110, command=self.save_record)
        self.save_btn.pack(side="right")
        ctk.CTkLabel(self, text=f"本机记忆库：{store.path}\n删除只停止使用这条记忆；删除来源会清除该聊天或并行任务产生的记忆与候选。", wraplength=820, justify="left", text_color=("#7d7d7d", "#adadad"), font=ctk.CTkFont(size=11)).grid(row=3, column=0, padx=28, pady=(0, 18), sticky="ew")
        self.new_record(source=self.source)
        self.refresh()
        self.after(80, self.lift)

    def refresh(self):
        for widget in self.records.winfo_children():
            widget.destroy()
        query = self.search_var.get().strip().casefold()
        pending = self.store.candidates("local", self.project["id"])
        self.candidate_count.configure(text=f"{len(pending)} 条待确认" if pending else "")
        view = self.view_var.get()
        is_pending = view == "待确认"
        records = pending if is_pending else self.store.list("local", self.project["id"])
        if view == "本次引用":
            records = [record for record in records if record["id"] in self.reference_ids]
        records = [r for r in records if query in (r["title"] + r["content"]).casefold()]
        if not records:
            empty = "没有待确认候选" if is_pending else "本次引用的记忆已删除或不再可用" if view == "本次引用" else "还没有相关记忆\n\n写下希望长安记住的信息"
            ctk.CTkLabel(self.records, text=empty, justify="left", text_color=("#848484", "#ababab")).pack(padx=10, pady=24)
        for record in records:
            scope = "通用" if not record["project_id"] else "项目"
            ctk.CTkButton(self.records, text=record["title"][:17] + "\n" + ("待确认" if is_pending else scope) + " · " + KINDS[record["kind"]], height=62, corner_radius=12, anchor="w", fg_color=("#e9e9e9", "#383838") if (self.record or self.candidate or {}).get("id") == record["id"] else "transparent", text_color=("#4b4b4b", "#d0d0d0"), hover_color=("#ececec", "#464646"), command=lambda r=record, p=is_pending: self.select_candidate(r) if p else self.select_record(r)).pack(fill="x", pady=3)

    def change_view(self, _=None):
        self.new_record()
        self.refresh()

    def new_record(self, source=None):
        self.record = None
        self.candidate = self.candidate_target = None
        self.source = source or {}
        self.title_var.set("")
        self.scope_var.set("当前项目")
        self.scope.configure(state="normal")
        self.kind_var.set("事实")
        self.content.delete("1.0", "end")
        self.content.insert("1.0", self.source.get("content", "")[:3000])
        self.provenance.configure(text="来源：当前用户消息，保存前请确认内容与范围" if self.source else "来源：手动确认；不自动导入历史聊天")
        self.delete_btn.configure(state="disabled", text="删除")
        self.forget_btn.configure(state="disabled")
        self.save_btn.configure(text="确认保存")

    def select_record(self, record):
        self.candidate = self.candidate_target = None
        self.record = record
        self.source = {}
        self.title_var.set(record["title"])
        self.kind_var.set(KINDS[record["kind"]])
        self.scope_var.set("通用" if not record["project_id"] else "当前项目")
        self.scope.configure(state="disabled")
        self.content.delete("1.0", "end")
        self.content.insert("1.0", record["content"])
        provenance = "并行 Agent 任务" if record.get("source_chat", "").startswith("parallel:") else "用户消息" if record["source_type"] == "user" else "手动确认"
        self.provenance.configure(text=f"来源：{provenance} · 修订 {record['revision']}\n更新于 {record['updated'][:19].replace('T', ' ')}")
        self.delete_btn.configure(state="normal", text="删除")
        parallel = record.get("source_chat", "").startswith("parallel:")
        self.forget_btn.configure(text="删除来源及其记忆" if parallel else "彻底忘记来源聊天",
                                  state="normal" if record.get("source_chat") else "disabled")
        self.save_btn.configure(text="确认保存")
        self.refresh()

    def select_candidate(self, candidate):
        self.new_record()
        self.candidate = candidate
        self.source = {"chat_id": candidate["source_chat"], "message_id": candidate["source_message"]}
        self.title_var.set(candidate["title"])
        self.kind_var.set(KINDS[candidate["kind"]])
        self.scope.configure(state="disabled")
        self.content.insert("1.0", candidate["content"])
        self.candidate_target = next((r for r in self.store.list("local", self.project["id"], include_global=False)
                                      if r["title"] == candidate["title"] and r["kind"] == candidate["kind"]), None)
        previous = "\n已有内容：" + self.candidate_target["content"][:180] if self.candidate_target else ""
        self.provenance.configure(text="待确认，不参与模型回答。原文证据：" + candidate["evidence"][:200] + previous)
        self.delete_btn.configure(text="忽略候选", state="normal")
        parallel = candidate.get("source_chat", "").startswith("parallel:")
        self.forget_btn.configure(text="删除来源及其记忆" if parallel else "彻底忘记来源聊天", state="normal")
        self.save_btn.configure(text="确认更新" if self.candidate_target else "确认保存")
        self.refresh()

    def _can_edit(self):
        if self.app.running:
            messagebox.showinfo("任务正在运行", "请在任务结束后修改记忆，避免请求中途改变参考资料。", parent=self)
            return False
        return True

    def save_record(self):
        if not self._can_edit():
            return
        try:
            if self.candidate:
                if self.candidate_target and not messagebox.askyesno("确认更新记忆", "即将替换同名记忆。\n\n原内容：\n" + self.candidate_target["content"] + "\n\n新内容：\n" + self.content.get("1.0", "end-1c"), parent=self):
                    return
                record = self.store.confirm_candidate("local", self.project["id"], self.candidate["id"],
                        kind=next(k for k, v in KINDS.items() if v == self.kind_var.get()), title=self.title_var.get(),
                        content=self.content.get("1.0", "end-1c"),
                        expected_revision=self.candidate_target["revision"] if self.candidate_target else None,
                        expected_memory_id=self.candidate_target["id"] if self.candidate_target else None)
                self.view_var.set("已确认")
            else:
                record = self.store.save(user_id="local", project_id=self.project["id"] if self.scope_var.get() == "当前项目" else "",
                                     kind=next(k for k, v in KINDS.items() if v == self.kind_var.get()),
                                     title=self.title_var.get(), content=self.content.get("1.0", "end-1c"),
                                     source_type="user" if self.source else "manual",
                                     source_chat=self.source.get("chat_id", ""), source_message=self.source.get("message_id", ""),
                                     memory_id=self.record["id"] if self.record else None)
        except (ValueError, sqlite3.Error) as exc:
            messagebox.showwarning("记忆未保存", str(exc), parent=self)
            return
        self.select_record(record)
        self.app.status.configure(text="记忆已保存")

    def delete_record(self):
        if self.candidate:
            if self._can_edit():
                try:
                    self.store.reject_candidate("local", self.project["id"], self.candidate["id"])
                except sqlite3.Error as exc:
                    messagebox.showwarning("操作未完成", str(exc), parent=self)
                    return
                self.new_record()
                self.refresh()
            return
        if not self.record or not self._can_edit():
            return
        if not messagebox.askyesno("停止使用这条记忆", "只从长期记忆检索中移除这条记录。\n原聊天、已生成的回答和备份会保留。\n如需连来源一起删除，请使用“彻底忘记来源聊天”。", parent=self):
            return
        try:
            self.store.delete("local", self.record["project_id"], self.record["id"])
        except sqlite3.Error as exc:
            messagebox.showwarning("删除未完成", str(exc), parent=self)
            return
        self.new_record()
        self.refresh()
        self.app.status.configure(text="记忆已删除")

    def forget_source_chat(self):
        record = self.record or self.candidate
        chat_id = (record or {}).get("source_chat", "")
        if not chat_id:
            return
        if not self._can_edit():
            return
        parallel = chat_id.startswith("parallel:")
        title = "删除并行任务来源记忆" if parallel else "彻底忘记来源聊天"
        detail = "将删除该并行任务产生的记忆、候选和去重记录；任务记录及文件保留。此操作不可恢复，确定继续？" if parallel else "将删除这个聊天的消息、附件、运行记录、检查点、产物、备份，以及由它产生的长期记忆和待确认候选。\n\n外部项目实际文件不会删除。此操作不可恢复，确定继续？"
        if not messagebox.askyesno(title, detail, parent=self):
            return
        try:
            if parallel:
                self.store.forget_chat("local", chat_id)
            else:
                self.app.forget_chat(chat_id)
        except (ValueError, OSError, sqlite3.Error) as exc:
            messagebox.showwarning("未完成", str(exc), parent=self)
            return
        self.new_record()
        self.refresh()
        self.app.status.configure(text="已删除来源记忆")

    def show_source(self):
        record = self.record or self.source
        chat_id = record.get("source_chat", record.get("chat_id"))
        message_id = record.get("source_message", record.get("message_id"))
        if isinstance(chat_id, str) and chat_id.startswith("parallel:"):
            task_id = chat_id.split(":", 1)[1]
            task = read_json(self.store.path.parent.parent / "parallel" / task_id / "task.json", {})
            messagebox.showinfo("记忆来源", f"来源：并行 Agent 任务\n状态：{task.get('status', '记录不存在')}\n\n{task.get('task', '任务记录已移除。')}", parent=self)
            return
        for project in self.app.projects:
            for chat in project.get("chats", []):
                if chat["id"] == chat_id:
                    message = next((m for m in chat["messages"] if m.get("id") == message_id and m.get("role") == "user"), None)
                    if message:
                        messagebox.showinfo("记忆来源", f"项目：{project['name']}\n聊天：{chat['title']}\n时间：{message.get('time', '')}\n\n{message['content']}", parent=self)
                        return
        messagebox.showinfo("记忆来源", "手动确认的条目没有原始聊天消息；原消息已移除时也无法定位。", parent=self)
