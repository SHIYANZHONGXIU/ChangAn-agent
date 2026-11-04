"""独立进程 Chromium 浏览器；Windows 下嵌入 Tk，网络复用固定公网代理。"""

from __future__ import annotations

import ctypes
import ipaddress
import multiprocessing
import os
import re
import sys
import time
from urllib.parse import urlsplit

import customtkinter as ctk


def browser_url(value):
    value = str(value).strip()
    if value == "about:blank":
        return value
    if "://" not in value and re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value) and not re.match(r"^[A-Za-z][A-Za-z0-9.-]*:\d+(?:[/?#]|$)", value):
        raise ValueError("只接受不含账号密码的 HTTP/HTTPS 公网地址")
    if "://" not in value:
        value = "https://" + value
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("只接受不含账号密码的 HTTP/HTTPS 公网地址")
    hostname = parsed.hostname.lower().rstrip(".")
    if hostname == "localhost" or hostname.endswith((".localhost", ".local", ".internal")):
        raise ValueError("浏览器禁止访问本机和内网服务")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("浏览器禁止访问本机和内网地址")
    return value


def browser_process(pipe, domains, initial_html=None):
    """Qt 和 Tk 不共享事件循环；Qt/Chromium 退出不会破坏聊天窗口。"""
    try:
        from specialized_tools import PinnedBrowserProxy
        with PinnedBrowserProxy(set(domains)) as proxy:
            os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = " ".join((
                f"--proxy-server={proxy.url}", "--proxy-bypass-list=<-loopback>",
                "--host-resolver-rules=MAP * ~NOTFOUND", "--disable-quic",
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            ))
            _qt_browser(pipe, proxy.url, initial_html)
    except BaseException as exc:
        try:
            pipe.send({"type": "error", "message": f"浏览器启动失败：{type(exc).__name__}: {exc}"})
        except (OSError, EOFError):
            pass
    finally:
        pipe.close()


def _qt_browser(pipe, proxy_url, initial_html):
    from pathlib import Path
    from PySide6.QtCore import QTimer, QUrl, Qt
    from PySide6.QtGui import QAction, QKeySequence, QIcon
    from PySide6.QtNetwork import QNetworkProxy
    from PySide6.QtWidgets import QApplication, QMainWindow, QToolBar, QLineEdit, QTabWidget, QFileDialog, QMessageBox, QInputDialog, QMenu
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings, QWebEngineUrlRequestInterceptor
    from PySide6.QtWebEngineWidgets import QWebEngineView

    app = QApplication([])
    app.setApplicationName("长安可视浏览器")
    icon = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "assets" / "changan.ico"
    if icon.is_file():
        app.setWindowIcon(QIcon(str(icon)))
    proxy = urlsplit(proxy_url)
    QNetworkProxy.setApplicationProxy(QNetworkProxy(QNetworkProxy.ProxyType.HttpProxy, proxy.hostname, proxy.port))

    class Filter(QWebEngineUrlRequestInterceptor):
        def interceptRequest(self, info):
            scheme = info.requestUrl().scheme()
            if scheme not in {"http", "https", "about", "data", "blob"}:
                info.block(True)

    profile = QWebEngineProfile(app)
    interceptor = Filter(profile)
    profile.setUrlRequestInterceptor(interceptor)
    profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies)
    settings = profile.settings()
    settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, False)
    settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
    settings.setAttribute(QWebEngineSettings.WebAttribute.FullScreenSupportEnabled, False)
    settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanAccessClipboard, False)
    settings.setAttribute(QWebEngineSettings.WebAttribute.AllowRunningInsecureContent, False)
    settings.setAttribute(QWebEngineSettings.WebAttribute.ScreenCaptureEnabled, False)

    class Page(QWebEnginePage):
        def acceptNavigationRequest(self, url, kind, main):
            if url.scheme() in {"data", "about"}:
                return True
            try:
                browser_url(url.toString())
                return True
            except ValueError:
                return False

        def createWindow(self, kind):
            return window.new_tab().page()

    class Window(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("长安 · 可视浏览器")
            self.resize(1120, 700)
            self.tabs = QTabWidget(self)
            self.tabs.setTabsClosable(True)
            self.tabs.setMovable(True)
            self.tabs.tabCloseRequested.connect(self.close_tab)
            self.tabs.currentChanged.connect(self.changed)
            self.setCentralWidget(self.tabs)
            bar = QToolBar("导航", self)
            bar.setMovable(False)
            self.addToolBar(bar)
            for label, callback, shortcut in (("后退", lambda: self.view().back(), "Alt+Left"), ("前进", lambda: self.view().forward(), "Alt+Right"), ("刷新", lambda: self.view().reload(), "Ctrl+R"), ("停止", lambda: self.view().stop(), "Escape"), ("新标签", self.new_tab, "Ctrl+T")):
                action = QAction(label, self)
                action.triggered.connect(callback)
                action.setShortcut(QKeySequence(shortcut))
                bar.addAction(action)
            self.address = QLineEdit(self)
            self.address.setPlaceholderText("输入公网网址 · 回车打开")
            self.address.returnPressed.connect(self.navigate)
            bar.addWidget(self.address)
            more = QAction("更多", self)
            more.triggered.connect(self.more)
            bar.addAction(more)
            self.statusBar().showMessage("临时浏览会话 · 不保存登录状态 · 本机和内网访问被拦截")
            self.downloads = set()
            self.devtools = []
            self.new_tab()

        def view(self):
            return self.tabs.currentWidget()

        def new_tab(self, checked=False):
            view = QWebEngineView(self.tabs)
            page = Page(profile, view)
            view.setPage(page)
            index = self.tabs.addTab(view, "新标签")
            self.tabs.setCurrentIndex(index)
            view.titleChanged.connect(lambda title, v=view: self.tabs.setTabText(self.tabs.indexOf(v), (title or "新标签")[:24]) if self.tabs.indexOf(v) >= 0 else None)
            view.urlChanged.connect(lambda url, v=view: self.address.setText(url.toString()) if v is self.view() else None)
            view.loadProgress.connect(lambda progress: self.statusBar().showMessage(f"正在加载 {progress}%"))
            view.loadFinished.connect(lambda ok: self.loaded(view, ok))
            page.featurePermissionRequested.connect(lambda origin, feature, p=page: p.setFeaturePermission(origin, feature, QWebEnginePage.PermissionPolicy.PermissionDeniedByUser))
            page.certificateError.connect(lambda error: error.rejectCertificate())
            return view

        def close_tab(self, index):
            if self.tabs.count() == 1:
                self.new_tab()
            view = self.tabs.widget(index)
            self.tabs.removeTab(index)
            view.page().deleteLater()
            view.deleteLater()

        def changed(self, index):
            if index >= 0 and hasattr(self, "address"):
                self.address.setText(self.view().url().toString())

        def navigate(self):
            try:
                self.view().setUrl(QUrl(browser_url(self.address.text())))
            except ValueError as exc:
                QMessageBox.warning(self, "地址被拦截", str(exc))

        def loaded(self, view, ok):
            self.statusBar().showMessage("已加载 · 摄像头/麦克风/定位默认禁用 · 下载需选择位置" if ok else "页面加载失败：请检查公网地址、网络或域名授权")
            send({"type": "loaded", "ok": ok, "url": view.url().toString()})

        def more(self):
            menu = QMenu(self)
            for label, callback in (("查找页面文字", self.find), ("放大", lambda: self.zoom(.1)), ("缩小", lambda: self.zoom(-.1)), ("恢复100%", lambda: self.view().setZoomFactor(1)), ("保存页面截图", self.screenshot), ("复制页面文本", lambda: self.view().page().toPlainText(lambda text: app.clipboard().setText(text))), ("开发者工具", self.inspect)):
                menu.addAction(label, callback)
            menu.exec(self.mapToGlobal(self.rect().topRight()) + __import__("PySide6.QtCore", fromlist=["QPoint"]).QPoint(-180, 42))

        def find(self):
            query, ok = QInputDialog.getText(self, "查找", "输入要查找的文字")
            if ok:
                self.view().findText(query)

        def zoom(self, delta):
            self.view().setZoomFactor(max(.3, min(3, self.view().zoomFactor() + delta)))

        def screenshot(self):
            path, _ = QFileDialog.getSaveFileName(self, "保存浏览器截图", "page.png", "PNG (*.png)")
            if path and not self.view().grab().save(path, "PNG"):
                QMessageBox.warning(self, "保存失败", "无法保存截图，请检查目标文件夹")

        def inspect(self):
            view = QWebEngineView()
            view.setWindowTitle("长安 · 开发者工具")
            view.resize(1000, 700)
            self.view().page().setDevToolsPage(view.page())
            self.devtools.append(view)
            view.show()

        def download(self, item):
            path, _ = QFileDialog.getSaveFileName(self, "保存下载文件（请确认来源可信）", item.downloadFileName())
            if not path:
                item.cancel()
                return
            target = Path(path)
            item.setDownloadDirectory(str(target.parent))
            item.setDownloadFileName(target.name)
            self.downloads.add(item)
            item.receivedBytesChanged.connect(lambda: self.statusBar().showMessage(f"下载 {target.name}：{item.receivedBytes()} 字节"))
            item.isFinishedChanged.connect(lambda: self.statusBar().showMessage(f"下载结束：{target.name}，状态 {item.state().name}"))
            item.accept()

        def closeEvent(self, event):
            for download in self.downloads:
                if not download.isFinished():
                    download.cancel()
            super().closeEvent(event)

    def send(value):
        try:
            pipe.send(value)
        except (EOFError, OSError):
            app.quit()

    window = Window()
    profile.downloadRequested.connect(window.download)
    window.show()
    send({"type": "ready", "handle": int(window.winId())})
    if initial_html is not None:
        window.view().setHtml(initial_html)

    def receive():
        try:
            while pipe.poll():
                message = pipe.recv()
                kind = message.get("command")
                if kind == "close":
                    window.close()
                    app.quit()
                elif kind == "navigate":
                    window.address.setText(browser_url(message["url"]))
                    window.navigate()
                elif kind == "html":
                    window.view().setHtml(str(message["html"])[:2_000_000])
                elif kind == "evaluate":
                    window.view().page().runJavaScript(str(message["script"]), lambda result, i=message.get("id"): send({"type": "evaluation", "id": i, "result": result}))
                elif kind == "capture":
                    send({"type": "captured", "ok": window.view().grab().save(str(message["path"]), "PNG")})
        except (EOFError, BrokenPipeError, OSError):
            window.close()
            app.quit()
        except Exception as exc:
            send({"type": "error", "message": str(exc)})
    timer = QTimer()
    timer.timeout.connect(receive)
    timer.start(40)
    app.exec()
    for i in range(window.tabs.count()):
        window.tabs.widget(i).page().deleteLater()
    window.deleteLater()
    app.processEvents()
    profile.deleteLater()
    app.processEvents()


class BrowserHost(ctk.CTkFrame):
    def __init__(self, master, domains=(), on_event=None):
        super().__init__(master, fg_color=("#f5f5f5", "#202124"), corner_radius=10)
        self.domains, self.on_event = domains, on_event or (lambda value: None)
        self.process = self.pipe = None
        self.handle = None
        self.job = None
        self.closed = False
        self.status = ctk.CTkLabel(self, text="点击启动真实浏览器 · 支持标签、网页交互、下载与截图\n临时会话不会保存登录；禁用内网访问。", wraplength=700)
        self.status.pack(pady=(40, 16))
        self.start_button = ctk.CTkButton(self, text="启动浏览器", command=self.start)
        self.start_button.pack()
        self.bind("<Configure>", lambda _: self.resize())
        self.bind("<Map>", lambda _: self.resize())

    def start(self):
        if self.process and self.process.is_alive():
            return
        self.closed = False
        context = multiprocessing.get_context("spawn")
        self.pipe, child = context.Pipe()
        self.process = context.Process(target=browser_process, args=(child, list(self.domains)), daemon=True)
        self.process.start()
        child.close()
        self.start_button.configure(state="disabled")
        self.status.configure(text="正在启动 Chromium 浏览器…")
        self.job = self.after(80, self.poll)

    def send(self, command, **value):
        if self.pipe:
            self.pipe.send({"command": command, **value})

    def poll(self):
        self.job = None
        if self.closed:
            return
        try:
            while self.pipe and self.pipe.poll():
                message = self.pipe.recv()
                if message.get("type") == "ready":
                    self.handle = message["handle"]
                    if os.name == "nt":
                        user = ctypes.windll.user32
                        user.SetParent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
                        user.SetParent.restype = ctypes.c_void_p
                        get = user.GetWindowLongPtrW
                        get.argtypes, get.restype = [ctypes.c_void_p, ctypes.c_int], ctypes.c_ssize_t
                        set_value = user.SetWindowLongPtrW
                        set_value.argtypes, set_value.restype = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t], ctypes.c_ssize_t
                        style = get(self.handle, -16)
                        set_value(self.handle, -16, (style & ~0x80CF0000) | 0x40000000)
                        ctypes.set_last_error(0)
                        user.SetParent(self.handle, self.winfo_id())
                        self.status.pack_forget()
                        self.start_button.pack_forget()
                        self.resize()
                    else:
                        self.status.configure(text="浏览器已在独立窗口打开")
                if message.get("type") == "error":
                    self.status.configure(text=message["message"])
                    self.start_button.configure(state="normal")
                self.on_event(message)
            if self.process and not self.process.is_alive():
                self.status.pack(pady=20)
                self.status.configure(text="浏览器已退出，可重新启动")
                self.start_button.pack()
                self.start_button.configure(state="normal")
                self.handle = None
                return
        except (EOFError, OSError):
            self.status.configure(text="浏览器连接已结束，可关闭工作台后重新打开")
            return
        self.job = self.after(80, self.poll)

    def resize(self):
        if self.handle and os.name == "nt":
            ctypes.windll.user32.MoveWindow(ctypes.c_void_p(self.handle), 0, 0, max(1, self.winfo_width()), max(1, self.winfo_height()), True)

    def close(self):
        self.closed = True
        if self.job:
            self.after_cancel(self.job)
            self.job = None
        if self.process:
            try:
                self.send("close")
            except (OSError, EOFError):
                pass
            self.process.join(timeout=3)
            if self.process.is_alive():
                from orchestrator import ToolExecutor
                ToolExecutor._terminate_process_tree(self, self.process)
                self.process.join(timeout=2)
            if not self.process.is_alive():
                self.process.close()
            self.process = None
        if self.pipe:
            self.pipe.close()
            self.pipe = None
