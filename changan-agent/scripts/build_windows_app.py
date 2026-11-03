"""构建独立 Windows 长安桌面版，并进行隔离目录的实际窗口启动检查。"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path
from ctypes import wintypes

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
APP_NAME = "ChangAn"


def _make_icon(target: Path):
    images = []
    for size in (16, 24, 32, 48, 64, 128, 256):
        image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        pad = max(1, size // 20)
        draw.rounded_rectangle((pad, pad, size - pad - 1, size - pad - 1), radius=size // 4,
                               fill="#252628")
        width = max(2, size // 10)
        center = size // 2
        left, right = size // 4, size * 3 // 4
        draw.rounded_rectangle((left, size // 3, right, size // 3 + width), radius=width // 2,
                               fill="#f5f5f2")
        draw.rounded_rectangle((left, center - width // 2, right - size // 8, center + width // 2),
                               radius=width // 2, fill="#62c7a5")
        draw.rounded_rectangle((left, size * 2 // 3 - width, right, size * 2 // 3), radius=width // 2,
                               fill="#f5f5f2")
        images.append(image)
    images[-1].save(target, format="ICO", append_images=images[:-1], sizes=[(im.width, im.height) for im in images])


def _window_for_process(pid: int):
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    found = []

    @callback_type
    def visit(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            title = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, title, len(title))
            if title.value:
                found.append((hwnd, title.value))
                return False
        return True

    user32.EnumWindows(visit, 0)
    return found


def _smoke_test(executable: Path, output_dir: Path):
    if os.name != "nt":
        raise RuntimeError("真实窗口验收只能在 Windows 上执行")
    from PIL import ImageGrab

    data_dir = output_dir / "smoke-data"
    if data_dir.exists():
        raise FileExistsError(f"冒烟检查目录已存在，拒绝覆盖：{data_dir}")
    data_dir.mkdir()
    environment = os.environ.copy()
    environment.update(CHANGAN_DATA_DIR=str(data_dir), CHANGAN_OFFLINE="1", PYTHONUTF8="1")
    process = subprocess.Popen([str(executable)], cwd=executable.parent, env=environment,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    screenshot = output_dir / "桌面版实际窗口.png"
    try:
        deadline = time.monotonic() + 50
        window = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"桌面程序在创建窗口前退出，返回码 {process.returncode}")
            window = next((item for item in _window_for_process(process.pid)
                           if "长安" in item[1]), None)
            if window:
                break
            time.sleep(0.25)
        if not window:
            raise TimeoutError("50 秒内没有检测到长安主窗口")
        time.sleep(2)
        rectangle = wintypes.RECT()
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        if not user32.GetWindowRect(window[0], ctypes.byref(rectangle)):
            raise ctypes.WinError(ctypes.get_last_error())
        ImageGrab.grab(bbox=(rectangle.left, rectangle.top, rectangle.right, rectangle.bottom)).save(screenshot)
        with Image.open(screenshot) as image:
            width, height = image.size
        if width < 900 or height < 600:
            raise RuntimeError(f"实际桌面分辨率不足，截图仅 {width}×{height}")
        user32.PostMessageW(window[0], 0x0010, 0, 0)
        process.wait(timeout=12)
        if process.returncode:
            raise RuntimeError(f"窗口正常关闭后退出码不为0：{process.returncode}")
        return {"passed": True, "window_title": window[1], "screenshot": screenshot.name,
                "resolution": [width, height], "isolated_data": str(data_dir)}
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if data_dir.exists():
            shutil.rmtree(data_dir)


def _build_destination(base: Path):
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    candidate = base / f"{stamp}-windows-x64"
    suffix = 1
    while candidate.exists():
        candidate = base / f"{stamp}-{suffix}-windows-x64"
        suffix += 1
    return candidate


def main():
    if os.name != "nt" or platform.machine().lower() not in ("amd64", "x86_64"):
        raise RuntimeError("本脚本需要在 64 位 Windows 上构建")
    if Path(sys.executable).resolve().is_relative_to(ROOT):
        raise RuntimeError("请使用项目外的 Python 构建环境，避免把项目虚拟环境打进程序")
    required = ("customtkinter", "tkinterdnd2", "PIL", "playwright", "pypdf", "docx", "openpyxl", "PyInstaller")
    missing = []
    for module in required:
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    if missing:
        raise RuntimeError("构建环境缺少桌面依赖：" + ", ".join(missing))
    verification = ROOT / "outputs" / "verification" / datetime.now().strftime("%Y-%m-%d") / "checks.json"
    checks = json.loads(verification.read_text(encoding="utf-8"))
    if not checks.get("passed") or checks.get("real_api_requests") != 0:
        raise RuntimeError("最近完整回归未通过或发现真实 API 请求，停止打包")
    install_check = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                    str(ROOT / "tests" / "test_desktop_installer.ps1")],
                                   cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    if install_check.returncode:
        raise RuntimeError("安装器临时目录复制检查失败：" + install_check.stdout + install_check.stderr)

    destination = _build_destination(ROOT / "outputs" / "release")
    app_dir, build_dir = destination / "长安桌面版", destination / "build"
    app_dir.mkdir(parents=True)
    build_dir.mkdir()
    icon = destination / "changan.ico"
    _make_icon(icon)
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--onedir",
        "--name", APP_NAME, "--contents-directory", "_internal", "--icon", str(icon),
        "--noupx", "--distpath", str(app_dir), "--workpath", str(build_dir), "--specpath", str(destination),
        "--add-data", f"{ROOT / 'README.md'};.",
        "--add-data", f"{ROOT / 'docs' / '文件夹说明.md'};docs",
        "--add-data", f"{icon};assets",
        "--collect-all", "customtkinter", "--collect-all", "tkinterdnd2",
        "--collect-all", "playwright", "--collect-all", "pypdf", "--collect-all", "docx",
        "--collect-all", "openpyxl", "--hidden-import", "PIL._tkinter_finder",
        str(ROOT / "desktop_app.py"),
    ]
    for name in ("tcl86t.dll", "tk86t.dll", "sqlite3.dll", "libmpdec-4.dll", "ffi.dll"):
        source = next((folder / name for folder in (Path(sys.prefix) / "Library" / "bin",
                                                     Path(sys.base_prefix) / "Library" / "bin",
                                                     Path(sys.base_prefix) / "DLLs", Path(sys.base_prefix))
                       if (folder / name).is_file()), None)
        if source:
            command.extend(("--add-binary", f"{source};."))
    log = destination / "build.log"
    with log.open("w", encoding="utf-8") as handle:
        result = subprocess.run(command, cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT, check=False)
    if result.returncode:
        tail = log.read_text(encoding="utf-8", errors="replace")[-5000:]
        raise RuntimeError(f"打包失败，完整日志保存在 {log}\n{tail}")

    generated = app_dir / APP_NAME
    executable = generated / f"{APP_NAME}.exe"
    if not executable.is_file():
        raise FileNotFoundError(f"PyInstaller 未生成主程序：{executable}")
    for item in generated.iterdir():
        item.replace(app_dir / item.name)
    generated.rmdir()
    executable = app_dir / f"{APP_NAME}.exe"
    support = """# 长安 Windows 桌面便携版

双击 `ChangAn.exe` 即可启动，无需另行安装 Python。请保持 `ChangAn.exe` 与 `_internal` 文件夹在一起。

新安装默认将设置、聊天、记忆和日志保存在 `%LOCALAPPDATA%\\ChangAn\\data`。若程序放在已有长安项目目录中且能识别原项目，则继续使用该项目原有的 `data`，不会搬移或覆盖资料。也可以在启动前设置 `CHANGAN_DATA_DIR` 指向另一数据目录。

如需桌面图标，先把整个文件夹放在固定位置，再右键 `ChangAn.exe` →“显示更多选项”→“发送到”→“桌面快捷方式”。移动程序文件夹后需重新创建快捷方式。

本包是 64 位 Windows 便携应用，不含代码签名；首次下载可能出现 Windows SmartScreen 提示。本包未打入独立 Chromium 浏览器，浏览器自动化优先使用已有的 Playwright Chromium，否则在 Windows 上调用系统自带的 Microsoft Edge；聊天、文件工具与办公文档读取不依赖浏览器。

浏览器请求经本地代理连接到 DNS 校验后的公网 IP；每次最多执行 20 个浏览器操作，单连接流量上限 100 MB。命令输出最多保留 stdout 20 KB、stderr 10 KB；Windows 命令树最多 64 个进程，结束时会清理。Full access 不是操作系统沙箱，也没有 CPU/内存配额。

PDF、Word、Excel 文件最大 100 MB；Office 压缩包另有限制；PDF 每次最多提取 20 页，Excel 单次最多写入 10000 个单元格。

源码启动方式仍保留在项目根目录的 `启动桌面版.bat`，不会被本便携版替换。卸载便携版只需退出程序后删除应用文件夹；默认用户数据在上述独立目录中，不会一并删除。
"""
    (app_dir / "桌面版使用说明.md").write_text(support, encoding="utf-8")
    dist_dir = app_dir
    distribution_readme = """# 长安 Windows 桌面版

1. 将 `长安桌面版-Windows-x64.zip` 完整解压到固定位置。
2. 可直接双击 `长安桌面版\\ChangAn.exe` 使用，或运行 `Install-ChangAn.bat` 安装桌面和开始菜单快捷方式。
3. 安装时可选择导入旧版 `Agent\\data`。请先退出旧程序；旧程序运行时或数据有冲突，安装会停止。复制经校验，原目录保留。也可以跳过导入，开始一份独立的新数据。
4. 卸载请先退出应用，在开始菜单“长安”文件夹中打开“卸载长安”。卸载会再次确认，但不会删除用户数据 `%LOCALAPPDATA%\\ChangAn\\data`。

本版本是未签名的本机验收版，尚未进行干净电脑安装、升级与卸载实机验收。
"""
    (dist_dir / "README-桌面版.md").write_text(distribution_readme, encoding="utf-8")
    for source, output_name in ((ROOT / "scripts" / "install_desktop.ps1", "Install-ChangAn.ps1"),
                                (ROOT / "scripts" / "uninstall_desktop.ps1", "Uninstall-ChangAn.ps1")):
        (dist_dir / output_name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8-sig")
    shutil.copy2(ROOT / "scripts" / "install_desktop.bat", dist_dir / "Install-ChangAn.bat")
    generated = destination / "ChangAn.spec"
    if generated.is_file():
        generated.unlink()
    for path in destination.glob("ChangAn*.spec"):
        path.unlink()
    try:
        smoke = _smoke_test(executable, destination)
    except Exception:
        (destination / "build-status.json").write_text(
            json.dumps({"passed": False, "artifact": str(app_dir), "log": str(log)}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        raise

    manifest = {"application": "长安", "version": "2026.11.03", "format": "Windows x64 portable onedir",
                "python_runtime_included": True, "source_data_packaged": False, "real_api_requests": 0,
                "smoke_test": {key: value for key, value in smoke.items() if key != "isolated_data"} | {"isolated_data": True},
                "source_tests": checks, "source_files": [], "files": []}
    (destination / "source-checks.json").write_text(json.dumps(checks, ensure_ascii=False, indent=2), encoding="utf-8")
    for name in ("app_runtime.py", "desktop_app.py", "orchestrator.py", "specialized_tools.py", "validators.py",
                 "tests/test_app_runtime.py", "tests/test_documents.py", "tests/test_engine.py", "tests/test_reliability.py", "tests/test_desktop_installer.ps1",
                 "scripts/build_windows_app.py", "scripts/install_desktop.bat", "scripts/install_desktop.ps1",
                 "scripts/uninstall_desktop.ps1", "docs/Windows桌面版使用与验收.md"):
        path = ROOT / name
        manifest["source_files"].append({"path": name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    for path in sorted(app_dir.rglob("*")):
        if path.is_file():
            manifest["files"].append({"path": path.relative_to(app_dir).as_posix(), "size": path.stat().st_size,
                                      "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    (destination / "package-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    archive = destination / "长安桌面版-Windows-x64.zip"
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as result_zip:
        for path in sorted(app_dir.rglob("*")):
            if path.is_file():
                result_zip.write(path, Path("长安桌面版") / path.relative_to(app_dir))
    manifest["archive"] = {"path": archive.name, "size": archive.stat().st_size,
                            "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}
    (destination / "package-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": True, "application": str(app_dir), "executable": str(executable),
                      "portable_zip": str(archive), "zip_bytes": archive.stat().st_size,
                      "manifest": str(destination / "package-manifest.json"), "smoke_test": smoke},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
