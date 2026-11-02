"""运行期控制：把「关闭服务」从网页转交给真正的 uvicorn 实例。

为什么不在路由里直接 ``os._exit``：uvicorn 正常退出会走 lifespan、关闭连接、
把日志刷完；直接杀进程容易留下半截日志和占用端口。这里只注册回调，
由 ``start.py`` 把 ``server.should_exit = True`` 挂进来，路由负责触发。
"""
from __future__ import annotations

import threading
from typing import Callable

_lock = threading.Lock()
_hooks: list[Callable[[], None]] = []


def on_shutdown(fn: Callable[[], None]) -> None:
    with _lock:
        _hooks.append(fn)


def scheduled() -> bool:
    """是否有进程可以响应关闭请求（测试/裸跑 FastAPI 时没有）。"""
    with _lock:
        return bool(_hooks)


def request_shutdown() -> bool:
    """触发所有关闭回调；返回是否真的有人接手。"""
    with _lock:
        hooks = list(_hooks)
    for fn in hooks:
        try:
            fn()
        except Exception:  # noqa: BLE001 - 关闭阶段不该因为某个回调炸掉
            pass
    return bool(hooks)


def reset_hooks() -> None:
    """仅测试使用：清空回调，避免影响其他用例。"""
    with _lock:
        _hooks.clear()
