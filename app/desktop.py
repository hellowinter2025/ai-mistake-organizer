"""Windows 桌面集成：导出文件直接落盘，并负责打开资源管理器。

为什么不走浏览器下载：Chromium 的保存动作会过一次安全扫描（企业策略、杀软、
"不常下载的文件"提示都可能拦下来），要么文件被静默改后缀，要么弹一堆框，
最后还得自己去下载列表里翻。这里由本地服务直接写盘，路径完全可预期。

只有在"文件夹确实没开着"的时候才弹资源管理器——判断依据是枚举系统里
已有的 CabinetWClass 窗口标题（Win11 的标题形如
``C:\\Users\\<用户名>\\Downloads - 文件资源管理器``），比 COM 遍历 Shell.Application
便宜得多，也不用额外依赖。
"""
from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

from .config import DATA_DIR

# 刚打开过的目录短暂记住，防连点两次导出弹出两个窗口
_recently_opened: dict[str, float] = {}
_SUPPRESS_SECONDS = 4.0


def downloads_dir() -> Path:
    """导出落点：环境变量 MM_DOWNLOAD_DIR > 用户 Downloads > 程序内 exports。"""
    env = str(os.environ.get("MM_DOWNLOAD_DIR") or "").strip()
    if env:
        path = Path(env).expanduser()
        try:
            path.mkdir(parents=True, exist_ok=True)
            return path
        except OSError:
            pass
    home_downloads = Path.home() / "Downloads"
    if home_downloads.is_dir():
        return home_downloads
    fallback = DATA_DIR / "exports"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def unique_path(folder: Path, name: str) -> Path:
    """同名文件已存在时自动加序号，绝不覆盖。

    ``试卷.pdf`` → ``试卷 (2).pdf`` → ``试卷 (3).pdf``。重复导出同一份卷子
    很多时候是想留两个版本（改过排版、改过答案），直接覆盖会把上一份悄悄弄丢。
    """
    dest = folder / name
    if not dest.exists():
        return dest
    stem, suffix = os.path.splitext(name)
    i = 2
    while True:
        candidate = folder / f"{stem} ({i}){suffix}"
        if not candidate.exists():
            return candidate
        i += 1


def publish(src: Path, name: str) -> Path:
    """把生成好的文件复制到下载目录，返回最终路径。

    **不覆盖同名文件**：已有同名就自动加 ``(2)``、``(3)`` …，返回的是真正落盘的
    那个路径（前端要拿它报给用户）。文件被 PDF 阅读器/Word 占用时给出可读的提示，
    而不是抛 WinError 32。
    """
    target_dir = downloads_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = unique_path(target_dir, name)
    try:
        shutil.copyfile(src, dest)
    except PermissionError as exc:  # 目标被占用或目录只读
        raise OSError(f"无法写入 {dest}，文件可能正被其他程序打开，请先关闭后再导出") from exc
    except OSError as exc:
        raise OSError(f"写入 {dest} 失败：{exc}") from exc
    return dest


def _explorer_titles() -> list[str]:
    """当前所有可见资源管理器窗口的标题。非 Windows 或调用失败返回空表。"""
    if sys.platform != "win32":
        return []
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
    except OSError:
        return []

    titles: list[str] = []
    proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]

    def walk(hwnd, _param):  # noqa: ANN001 - ctypes 回调签名固定
        if not user32.IsWindowVisible(hwnd):
            return True
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        if cls.value == "CabinetWClass":
            buf = ctypes.create_unicode_buffer(600)
            user32.GetWindowTextW(hwnd, buf, 600)
            if buf.value:
                titles.append(buf.value)
        return True

    try:
        user32.EnumWindows(proc_type(walk), 0)
    except OSError:
        return []
    return titles


def _norm_path(text: str) -> str:
    """统一分隔符与大小写再比：路径可能来自 Path（反斜杠）或手写的正斜杠。"""
    return str(text).replace("/", "\\").rstrip("\\").lower()


def _title_shows_folder(title: str, folder: Path) -> bool:
    """标题是不是就指向这个目录。

    Explorer 的标题是「<路径> - 文件资源管理器」或「<路径> - File Explorer」，
    所以按前缀比、并且要求后面紧跟分隔符，避免 Downloads 命中 Downloads2。
    """
    key = _norm_path(folder)
    low = _norm_path(title.strip())
    if low == key:
        return True
    if not low.startswith(key):
        return False
    return low[len(key) : len(key) + 1] in (" ", "-", "–", "—", "\t")


def folder_is_open(folder: Path) -> bool:
    return any(_title_shows_folder(t, folder) for t in _explorer_titles())


def reveal_folder(folder: Path) -> str:
    """打开目录。返回 ``opened`` / ``already`` / ``failed`` / ``skipped``。"""
    folder = folder.resolve() if folder.exists() else folder
    if folder_is_open(folder):
        return "already"
    key = str(folder).lower()
    now = time.monotonic()
    if now - _recently_opened.get(key, 0.0) < _SUPPRESS_SECONDS:
        # 窗口还在启动、枚举看不到，别重复弹
        return "already"
    if sys.platform != "win32":
        return "skipped"
    try:
        subprocess.Popen(  # noqa: S603 - 固定 explorer.exe，不做 shell 解析
            ["explorer.exe", str(folder)],
            close_fds=True,
        )
    except OSError:
        return "failed"
    _recently_opened[key] = now
    return "opened"


def open_file(path: Path) -> bool:
    """用系统默认程序打开文件（导出后想直接看效果时用）。"""
    if sys.platform != "win32":
        return False
    try:
        os.startfile(str(path))  # noqa: S606 - 打开自己刚生成的文件
        return True
    except OSError:
        return False
