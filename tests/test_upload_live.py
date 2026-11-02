"""真实服务器级上传测试。

单元测试用 TestClient 拿不到的东西全在这里：进程重启后的令牌状态、
多线程并发上传、multipart 大文件分块落盘、以及浏览器里前端自动换令牌重试。
"""
from __future__ import annotations

import concurrent.futures
import io
import json
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from PIL import Image, ImageDraw

from app.config import BASE_DIR

pytestmark = pytest.mark.integration


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LiveServer:
    def __init__(self, data_dir: Path) -> None:
        self.data = data_dir
        self.port = _free_port()
        self.proc: subprocess.Popen | None = None

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self, timeout: float = 90.0) -> None:
        import os

        env = os.environ.copy()
        env["MM_DATA_DIR"] = str(self.data)
        env["MM_LOG_DIR"] = str(self.data / "logs")
        env["PYTHONPATH"] = str(BASE_DIR)
        env.pop("MM_APP_TOKEN", None)   # 走真实的令牌文件流程
        env.pop("PYTHONSTARTUP", None)
        self.proc = subprocess.Popen(
            [
                sys.executable, "-m", "uvicorn", "app.main:app",
                "--host", "127.0.0.1", "--port", str(self.port), "--log-level", "warning",
            ],
            cwd=str(BASE_DIR),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                r = httpx.get(f"{self.base}/api/health", timeout=2.0, trust_env=False)
                if r.status_code == 200:
                    return
            except Exception:  # noqa: BLE001 - 启动中
                pass
            time.sleep(0.25)
        raise RuntimeError("服务器未能在预期时间内启动")

    def stop(self) -> None:
        if self.proc is None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)
        self.proc = None

    def restart(self) -> None:
        self.stop()
        time.sleep(0.4)
        self.start()

    def token(self) -> str:
        return httpx.get(f"{self.base}/api/bootstrap", timeout=10, trust_env=False).json()["token"]

    def upload(self, files: list[tuple[str, bytes]], token: str | None = None, timeout: float = 180.0):
        headers = {"X-App-Token": token if token is not None else self.token()}
        return httpx.post(
            f"{self.base}/api/images",
            headers=headers,
            files=[("files", (name, raw, "image/png")) for name, raw in files],
            timeout=timeout,
            trust_env=False,
        )


def png_bytes(seed: int, width: int = 900, height: int = 1200) -> bytes:
    img = Image.new("RGB", (width, height), "white")
    d = ImageDraw.Draw(img)
    d.text((60, 90), f"QUESTION {seed}", fill="black")
    for i in range(6):
        d.line([(60, 150 + i * 90), (width - 80, 150 + i * 90)], fill=(20, 20, 20), width=4)
    d.line([(width - 160, 150), (width - 90, 230)], fill=(200, 40, 30), width=8)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    srv = LiveServer(tmp_path_factory.mktemp("live_data"))
    srv.start()
    yield srv
    srv.stop()


def test_token_survives_restart_and_old_page_keeps_working(server):
    """核心回归：服务重启后，旧页面手里的令牌必须仍然有效。

    之前每次启动都换新令牌，用户开着页面重启服务，上传就 403。
    """
    token_before = server.token()
    first = server.upload([("a.png", png_bytes(1))], token=token_before)
    assert first.status_code == 200, first.text

    server.restart()

    token_after = server.token()
    assert token_after == token_before, "令牌会随重启变化，旧页面必然 403"

    again = server.upload([("b.png", png_bytes(2))], token=token_before)
    assert again.status_code == 200, again.text
    assert again.json()["images"][0]["id"]


def test_stale_token_still_returns_structured_403(server):
    """真正换了令牌的场景（比如删了 data/.session_token），后端要给出可识别的 code。"""
    res = server.upload([("c.png", png_bytes(3))], token="a" * 43)
    assert res.status_code == 403
    body = res.json()
    assert body["code"] == "token"
    assert "令牌" in body["error"]


def test_concurrent_uploads(server):
    """多张照片同时上传（前端并发 + 浏览器多标签）不应互相踩踏。"""
    files = [(f"p{i}.png", png_bytes(100 + i)) for i in range(8)]
    token = server.token()

    def one(item):
        return server.upload([item], token=token, timeout=240)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(one, files))

    ids = []
    for r in results:
        assert r.status_code == 200, r.text
        ids.append(r.json()["images"][0]["id"])
    assert len(set(ids)) == len(ids), "并发上传出现了重复图片 ID"


def test_large_upload_through_real_multipart(server):
    """21MB 走真正的 multipart 分块解析，验证不会被中间件/解析器莫名 500。"""
    big = png_bytes(7, 2600, 3400)
    big = big + b"\x00" * (21 * 1024 * 1024 - len(big))
    res = server.upload([("big.png", big)], timeout=300)
    assert res.status_code == 400, res.text
    assert "MB" in res.json()["detail"]


def _page_eval(server, script: str, out_name: str, ignore_errors: tuple[str, ...] = ()) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("本机没有 node")
    out_png = server.data / out_name
    proc = subprocess.run(
        [node, str(BASE_DIR / "tools" / "page_shot.cjs"), f"{server.base}/", str(out_png),
         "--wait=2500", f"--eval={script}"],
        capture_output=True,
        timeout=240,
        cwd=str(BASE_DIR),
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "ignore")[:600]
    payload = json.loads(proc.stdout.decode("utf-8"))
    real_errors = [
        e for e in payload.get("errors", [])
        if "favicon" not in e and not any(k in e for k in ignore_errors)
    ]
    assert not real_errors, real_errors[:3]
    return json.loads(payload["eval"])


def test_browser_auto_refreshes_token_and_uploads(server):
    """端到端：页面里故意把令牌改错，前端应自动换新令牌并完成上传。"""
    script = """
(async () => {
  try {
    const api = await import('/js/api.js');
    const st = await import('/js/state.js');
    st.state.token = 'stale-token-from-before-restart';
    const canvas = document.createElement('canvas');
    canvas.width = 900; canvas.height = 1200;
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = '#ffffff'; ctx.fillRect(0, 0, 900, 1200);
    ctx.fillStyle = '#111111'; ctx.font = '28px sans-serif';
    ctx.fillText('1. 浏览器重试测试题', 60, 110);
    for (let i = 0; i < 5; i += 1) ctx.fillRect(60, 150 + i * 80, 720, 5);
    const blob = await new Promise((r) => canvas.toBlob(r, 'image/png'));
    const file = new File([blob], 'retry.png', { type: 'image/png' });
    const data = await api.uploadImages([file]);
    return JSON.stringify({
      ok: true,
      images: (data.images || []).length,
      id: (data.images || [])[0]?.id || null,
      tokenChanged: st.state.token !== 'stale-token-from-before-restart',
      reused: (data.images || [])[0]?.reused ?? null,
    });
  } catch (err) {
    return JSON.stringify({ ok: false, error: String(err && err.message || err) });
  }
})()
"""
    # 第一次请求必然 403（这正是要验证的触发条件），重试那次才是成功
    data = _page_eval(server, script, "browser_upload.png", ignore_errors=("403 (Forbidden)",))
    assert data["ok"] is True, data
    assert data["images"] == 1, data
    assert data["tokenChanged"] is True, "前端没有自动换新令牌"
    assert data["id"]

    # 上传本身也应被后端接受（说明重试命中的是真实接口）
    check = httpx.get(f"{server.base}/api/images/{data['id']}/thumb?w=300", timeout=30, trust_env=False)
    assert check.status_code == 200
    assert Image.open(io.BytesIO(check.content)).width == 300


def test_browser_second_upload_still_works(server):
    """回归：上传过程中替换 dropzone.innerHTML 会把 <input type=file> 一起销毁，
    导致第二次上传点了没反应。这里连续传两次，并检查输入框仍在文档里。"""
    script = """
(async () => {
  const st = await import('/js/state.js');
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const make = async (label) => {
    const canvas = document.createElement('canvas');
    canvas.width = 880; canvas.height = 1180;
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = '#ffffff'; ctx.fillRect(0, 0, 880, 1180);
    ctx.fillStyle = '#111111'; ctx.font = '26px sans-serif';
    ctx.fillText(label, 50, 100);
    for (let i = 0; i < 6; i += 1) ctx.fillRect(50, 140 + i * 80, 700, 5);
    const blob = await new Promise((r) => canvas.toBlob(r, 'image/png'));
    return new File([blob], label + '.png', { type: 'image/png' });
  };
  const push = async (file) => {
    const input = document.querySelector('#fileInput');
    const dt = new DataTransfer();
    dt.items.add(file);
    input.files = dt.files;
    input.dispatchEvent(new Event('change'));
  };

  await push(await make('FIRST'));
  await sleep(3000);
  const afterFirst = st.state.images.length;
  await push(await make('SECOND'));
  await sleep(3000);
  const input = document.querySelector('#fileInput');
  const idle = document.querySelector('#dzIdle');
  const busy = document.querySelector('#dzBusy');
  return JSON.stringify({
    afterFirst,
    afterSecond: st.state.images.length,
    inputConnected: input.isConnected,
    idleVisible: !idle.classList.contains('hidden'),
    busyHidden: busy.classList.contains('hidden'),
  });
})()
"""
    data = _page_eval(server, script, "browser_upload_twice.png")
    assert data["afterFirst"] == 1, data
    assert data["afterSecond"] == 2, f"第二次上传没有生效：{data}"
    assert data["inputConnected"] is True, "文件输入被打包重建，后续上传会失效"
    assert data["idleVisible"] is True and data["busyHidden"] is True, "上传结束后状态没复位"
