"""聊天侧栏的线性图标和可用键盘操作的圆角菜单。"""

from __future__ import annotations

import os
import tkinter as tk

import customtkinter as ctk
from PIL import Image, ImageDraw


def sidebar_icon(name, size=16):
    images = []
    for color in ("#35383d", "#e3e4e7"):
        image = Image.new("RGBA", (96, 96))
        draw = ImageDraw.Draw(image)
        def line(points):
            draw.line([(x * 4, y * 4) for x, y in points], fill=color, width=6, joint="curve")
        def box(bounds, radius=2):
            draw.rounded_rectangle(tuple(v * 4 for v in bounds), radius=radius * 4, outline=color, width=6)
        if name == "pencil":
            line([(4, 16), (15, 5), (19, 9), (8, 20), (3, 21), (4, 16)])
            line([(13, 7), (17, 11)])
        elif name == "pin":
            line([(8, 3), (19, 3), (17, 10), (20, 14), (5, 14), (9, 10), (8, 3)])
            line([(12, 14), (12, 21)])
        elif name == "eye":
            line([(2, 12), (5, 7), (9, 4), (15, 4), (19, 7), (22, 12), (19, 17), (15, 20), (9, 20), (5, 17), (2, 12)])
            draw.ellipse((34, 34, 62, 62), outline=color, width=6)
        elif name == "folder":
            line([(21, 10), (21, 8), (10, 8), (8, 5), (3, 5), (3, 19), (12, 19)])
            line([(14, 15), (22, 15), (18, 11)])
            line([(22, 15), (18, 19)])
        elif name == "copy":
            box((8, 3, 21, 16))
            box((3, 8, 16, 21))
        elif name == "window":
            box((3, 4, 21, 20), 3)
            line([(3, 9), (21, 9)])
            box((11, 12, 23, 23))
        elif name in {"archive", "restore"}:
            box((3, 4, 21, 9))
            line([(5, 9), (5, 21), (19, 21), (19, 9)])
            line([(10, 13), (14, 13)])
        elif name == "check":
            line([(4, 12), (9, 17), (20, 6)])
        elif name == "trash":
            line([(3, 6), (21, 6)])
            line([(7, 6), (7, 3), (17, 3), (17, 6)])
            line([(5, 6), (6, 21), (18, 21), (19, 6)])
            line([(10, 10), (10, 17)])
            line([(14, 10), (14, 17)])
        elif name == "plus":
            line([(12, 4), (12, 20)])
            line([(4, 12), (20, 12)])
        elif name == "chevron":
            line([(9, 6), (15, 12), (9, 18)])
        elif name == "more":
            for x in (5, 12, 19):
                draw.ellipse((x * 4 - 3, 45, x * 4 + 3, 51), fill=color)
        images.append(image)
    return ctk.CTkImage(light_image=images[0], dark_image=images[1], size=(size, size))


class SidebarMenu(ctk.CTkToplevel):
    def __init__(self, master, items, parent_menu=None):
        super().__init__(master)
        self.withdraw()
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.items = items
        self.parent_menu = parent_menu
        self.root_menu = parent_menu.root_menu if parent_menu else self
        self.submenu = None
        self.selected = -1
        self.rows = {}
        self.previous_focus = master.focus_get()
        self.key_color = "#010203"
        self.configure(fg_color=self.key_color)
        if os.name == "nt":
            self.attributes("-transparentcolor", self.key_color)
        shadow = ctk.CTkFrame(self, corner_radius=16, fg_color=("#e4e6ea", "#111114"))
        shadow.pack(fill="both", expand=True)
        self.body = ctk.CTkFrame(shadow, corner_radius=14, border_width=1, border_color=("#e6e7ea", "#45464b"), fg_color=("#ffffff", "#28292d"))
        self.body.pack(fill="both", expand=True, padx=1, pady=(1, 3))
        for index, item in enumerate(items):
            if item is None:
                ctk.CTkFrame(self.body, height=1, fg_color=("#edeef0", "#414247")).pack(fill="x", padx=12, pady=5)
                continue
            row = ctk.CTkFrame(self.body, height=30, corner_radius=6, fg_color="transparent")
            row.pack(fill="x", padx=5, pady=(5 if index == 0 else 0, 5 if index == len(items) - 1 else 0))
            row.pack_propagate(False)
            enabled = item.get("enabled", True)
            color = ("#303238", "#efeff1") if enabled else ("#999ba1", "#777980")
            icon = ctk.CTkLabel(row, text="", image=sidebar_icon(item.get("icon", "more")), width=18, height=20)
            icon.pack(side="left", padx=(9, 7))
            label = ctk.CTkLabel(row, text=item["label"], anchor="w", font=ctk.CTkFont(size=13), text_color=color)
            label.pack(side="left", fill="y")
            suffix = "›" if item.get("children") else item.get("shortcut", "")
            hint = ctk.CTkLabel(row, text=suffix, anchor="e", width=18, font=ctk.CTkFont(size=12), text_color=("#92949a", "#a1a3aa"))
            hint.pack(side="right", padx=(10, 9))
            widgets = (row, icon, label, hint)
            self.rows[index] = widgets
            for widget in widgets:
                widget.bind("<Enter>", lambda event, i=index: self.select(i))
                widget.bind("<ButtonRelease-1>", lambda event, i=index: self.activate(i))
            if item.get("shortcut"):
                keys = item["shortcut"].replace("Ctrl", "Control").split("+")
                if "Shift" not in keys:
                    keys[-1] = keys[-1].lower()
                self.bind("<" + "-".join(keys) + ">", lambda event, i=index: self.activate(i))
        self.bind("<Down>", lambda _: self.step(1))
        self.bind("<Up>", lambda _: self.step(-1))
        self.bind("<Return>", lambda _: self.activate(self.selected))
        self.bind("<space>", lambda _: self.activate(self.selected))
        self.bind("<Right>", lambda _: self.open_submenu(self.selected, focus=True))
        self.bind("<Left>", lambda _: self.back())
        self.bind("<Escape>", lambda _: self.back())
        self.bind("<Button-1>", self.outside_click, add="+")
        self.bind("<FocusOut>", lambda _: self.after_idle(self.check_focus), add="+")

    def show(self, x, y):
        self.update_idletasks()
        width = max(244, self.winfo_reqwidth())
        height = self.winfo_reqheight()
        self.geometry(f"{self._reverse_window_scaling(width)}x{self._reverse_window_scaling(height)}+{int(max(0, min(x, self.winfo_screenwidth() - width)))}+{int(max(0, min(y, self.winfo_screenheight() - height)))}")
        self.deiconify()
        self.lift()
        if self.parent_menu is None:
            self.grab_set()
            self.focus_set()

    def select(self, index):
        if index not in self.rows or not self.items[index].get("enabled", True):
            return
        self.selected = index
        for i, widgets in self.rows.items():
            color = ("#f0f1f3", "#3c3e44") if i == index else ("#ffffff", "#28292d")
            for widget in widgets:
                widget.configure(fg_color=color)
        if self.submenu and self.submenu.parent_index != index:
            self.submenu.destroy()
            self.submenu = None
        if self.items[index].get("children"):
            self.open_submenu(index)

    def step(self, direction):
        choices = [i for i in self.rows if self.items[i].get("enabled", True)]
        if choices:
            current = choices.index(self.selected) if self.selected in choices else (-1 if direction > 0 else 0)
            self.select(choices[(current + direction) % len(choices)])
        return "break"

    def activate(self, index):
        if index not in self.rows or not self.items[index].get("enabled", True):
            return "break"
        item = self.items[index]
        if item.get("children"):
            self.open_submenu(index, focus=True)
        else:
            command = item.get("command")
            master = self.root_menu.master
            self.root_menu.close()
            if command:
                master.after_idle(command)
        return "break"

    def open_submenu(self, index, focus=False):
        if index not in self.rows or not self.items[index].get("children"):
            return "break"
        if not self.submenu:
            self.submenu = SidebarMenu(self, self.items[index]["children"], parent_menu=self)
            self.submenu.parent_index = index
            self.update_idletasks()
            self.submenu.update_idletasks()
            x = self.winfo_rootx() + self.winfo_width() - 3
            if x + self.submenu.winfo_reqwidth() > self.winfo_screenwidth():
                x = self.winfo_rootx() - self.submenu.winfo_reqwidth() + 3
            self.submenu.show(x, self.rows[index][0].winfo_rooty() - 5)
        if focus:
            self.submenu.focus_set()
            self.submenu.step(1)
        return "break"

    def outside_click(self, event):
        menu = self.root_menu
        while menu:
            if menu.winfo_rootx() <= event.x_root < menu.winfo_rootx() + menu.winfo_width() and menu.winfo_rooty() <= event.y_root < menu.winfo_rooty() + menu.winfo_height():
                return
            menu = menu.submenu
        self.root_menu.close()

    def check_focus(self):
        if not self.winfo_exists():
            return
        focus = self.focus_displayof()
        if focus is None or (focus != self.root_menu and not str(focus).startswith(str(self.root_menu) + ".")):
            self.root_menu.close()

    def back(self):
        if self.parent_menu:
            parent = self.parent_menu
            parent.submenu = None
            self.destroy()
            parent.focus_set()
        else:
            self.close()
        return "break"

    def close(self):
        if not self.winfo_exists():
            return
        focus = self.previous_focus
        self.grab_release()
        self.destroy()
        if focus and focus.winfo_exists():
            focus.focus_set()
