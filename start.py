"""桌面启动器：拉起本地 Web 服务并打开浏览器。

两种用法：
1. 双击 ``启动.vbs`` —— **不弹命令行窗口**，输出写进 logs/launcher.log；
2. 双击 ``启动.bat`` 或 ``.venv\\Scripts\\python.exe start.py`` —— 带控制台，方便看报错。

``启动.vbs`` 会给子进程设 MM_HEADLESS=1，本文件据此把 stdout/stderr 重定向到
日志文件（没有控制台时 python.exe 的输出没地方去，报错会彻底消失）。

关服务不用关窗口：网页右上角的 ⏻ 按钮会走 ``/api/shutdown``，
由这里注册的回调把 uvicorn 优雅停掉。
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
LOG_DIR = ROOT / "logs"


def _redirect_output() -> None:
    """无窗口启动（MM_HEADLESS=1）时把输出落盘，否则报错完全看不见。"""
    if os.environ.get("MM_HEADLESS") != "1":
        return
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = LOG_DIR / "launcher.log"
        # 每次启动都 append，攒到 2MB 就滚一份，免得几年后打开是几十兆
        if path.exists() and path.stat().st_size > 2 * 1024 * 1024:
            path.replace(LOG_DIR / "launcher.old.log")
        fh = open(path, "a", buffering=1, encoding="utf-8")
    except OSError:
        return
    sys.stdout = fh
    sys.stderr = fh
    print(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} start ---")


def _fatal(message: str) -> None:
    """致命错误：无窗口时用系统弹窗，否则用户只会看到"什么都没发生"。"""
    print(message)
    if os.environ.get("MM_HEADLESS") != "1":
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, message, "AI 错题整理 · 无法启动", 0x10)
    except Exception:  # noqa: BLE001 - 弹窗失败也不能再抛
        pass


def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.6)
        return sock.connect_ex((host, port)) == 0


def _open_browser(url: str) -> None:
    """无窗口启动时也想让浏览器自己弹出来；调试/自动化可用环境变量关掉。"""
    if os.environ.get("MM_NO_BROWSER") == "1" or os.environ.get("NO_BROWSER") == "1":
        return
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001 - 没装默认浏览器不该影响服务启动
        pass


def _pause_if_interactive() -> None:
    """只有真控制台才等回车；无窗口启动时 stdin 为空，等下去就是永久挂起。"""
    try:
        if sys.stdin and sys.stdin.isatty():
            input("按回车退出…")
    except (OSError, ValueError):
        pass


def main() -> int:
    _redirect_output()
    try:
        import uvicorn
    except ImportError:
        _fatal("依赖未安装。请在项目目录运行：\n.venv\\Scripts\\python.exe -m pip install -r requirements.txt")
        _pause_if_interactive()
        return 1

    from app import runtime
    from app.config import APP_NAME, APP_VERSION, settings
    from app.main import app

    host = settings.get("server", "host", default="127.0.0.1")
    port = int(settings.get("server", "port", default=8765))
    url = f"http://{host}:{port}/"

    if port_in_use(host, port):
        # 已有实例在跑，直接把页面叫出来
        print(f"{APP_NAME} 已在运行：{url}")
        _open_browser(url)
        return 0

    print("=" * 58)
    print(f"  {APP_NAME} v{APP_VERSION}")
    print(f"  地址：{url}")
    print("  停止服务：点页面右上角的 ⏻，或关闭这个窗口")
    print("=" * 58)

    def open_later() -> None:
        time.sleep(1.3)
        _open_browser(url)

    threading.Thread(target=open_later, daemon=True).start()

    config = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    # 网页「关闭服务」→ should_exit → uvicorn 正常收尾（关连接、刷日志、释放端口）
    runtime.on_shutdown(lambda: setattr(server, "should_exit", True))
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
