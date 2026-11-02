"""浏览器端公式渲染测试。

导出 PDF 走的就是浏览器排版，所以必须验证"真实浏览器里 KaTeX 能不能正确渲染"，
而不只是 Node 里 renderToString 不报错。这里启动一个静态服务，用无头 Edge 打开
测试页，读取 window.__result 做断言。
"""
from __future__ import annotations

import functools
import http.server
import json
import socketserver
import subprocess
import threading
from pathlib import Path

import pytest

from app.config import BASE_DIR
from tests.fixtures_math import ALL

PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>PENDING</title>
<link rel="stylesheet" href="/web/vendor/katex/katex.min.css">
<style>body{font-family:"Microsoft YaHei",sans-serif;margin:20px;} .box{margin:6px 0;}</style>
</head><body>
<div id="render"></div>
<script src="/web/vendor/katex/katex.min.js"></script>
<script src="/web/vendor/dompurify/dist/purify.min.js"></script>
<script type="module">
import { renderRich, renderFigure } from '/web/js/math.js';
const data = __FIXTURES__;
const root = document.getElementById('render');
const bad = [];
let errors = 0, katex = 0, zeroSized = 0;
data.items.forEach((text, i) => {
  const div = document.createElement('div');
  div.className = 'box';
  root.appendChild(div);
  const res = renderRich(div, text);
  errors += res.errors.length;
  if (res.errors.length) bad.push({ i, text, errors: res.errors });
  div.querySelectorAll('.katex').forEach((el) => {
    katex += 1;
    const r = el.getBoundingClientRect();
    if (r.height < 4 || r.width < 2) zeroSized += 1;
  });
});
const figBox = document.createElement('div');
root.appendChild(figBox);
const figOk = renderFigure(figBox, data.figure);
// 题图走 <img>（服务端 PNG / data:image/svg+xml），前端零 SVG 处理：
// RDKit 的键线全在 style 里，以前 DOMPurify 禁 style 导致整张只剩字母
const chemBox = document.createElement('div');
root.appendChild(chemBox);
const chemOk = renderFigure(chemBox, data.figureChem);
const chemImg = chemBox.querySelector('img');
const chemSrc = chemImg ? chemImg.src : '';
const probe = root.querySelector('.katex');
const fontFamily = probe ? getComputedStyle(probe).fontFamily : '';
window.__result = {
  items: data.items.length,
  errors, katex, zeroSized, bad,
  figureOk: figOk,
  figureIsImg: Boolean(figBox.querySelector('img')),
  chemOk,
  chemImgOk: Boolean(chemImg),
  chemSrcHasSvg: chemSrc.includes('image/svg+xml'),
  // data:image/svg+xml 是百分号编码，stroke-width 的字母会原样保留
  chemStrokeKept: /stroke-width/.test(decodeURIComponent(chemSrc.split(',').pop() || '')),
  fontFamily,
  fontLoaded: document.fonts.check('16px KaTeX_Main'),
};
document.title = 'DONE ' + errors;
</script>
</body></html>
"""

FIGURE = (
    '<svg viewBox="0 0 400 300"><polygon points="200,40 60,250 340,250" fill="none" stroke="#111" stroke-width="2"/>'
    '<text x="200" y="30" font-size="18" text-anchor="middle">A</text></svg>'
)

# RDKit 出的化学键：stroke 全写在 style 属性里（不是 stroke="" 属性）
FIGURE_CHEM = (
    "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 300 120\">"
    "<path d=\"M 30,30 L 90,30\" "
    "style=\"fill:none;fill-rule:evenodd;stroke:#000000;stroke-width:2.0px;stroke-linecap:butt\"/>"
    "<path d=\"M 30,30 m -6,0 a 6,6 0 1,0 12,0 a 6,6 0 1,0 -12,0\" "
    "style=\"fill:#000000;fill-rule:evenodd\"/>"
    "</svg>"
)


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def static_server():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(BASE_DIR))
    with socketserver.TCPServer(("127.0.0.1", 0), handler) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
        httpd.shutdown()


@pytest.mark.integration
def test_browser_renders_all_formulas(static_server, tmp_path):
    page = BASE_DIR / "data" / "cache" / "browser_math_test.html"
    page.parent.mkdir(parents=True, exist_ok=True)
    payload = {"items": ALL, "figure": FIGURE, "figureChem": FIGURE_CHEM}
    html = PAGE.replace("__FIXTURES__", json.dumps(payload, ensure_ascii=False))
    page.write_text(html, encoding="utf-8")

    out_png = tmp_path / "math.png"
    cmd = [
        "node",
        str(BASE_DIR / "tools" / "page_shot.cjs"),
        f"{static_server}/data/cache/browser_math_test.html",
        str(out_png),
        "--wait=3000",
        "--eval=JSON.stringify(window.__result)",
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=180, cwd=str(BASE_DIR))
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "ignore")[:500]
    result = json.loads(proc.stdout.decode("utf-8"))
    real_errors = [e for e in result["errors"] if "favicon.ico" not in e]
    assert not real_errors, real_errors[:3]

    data = json.loads(result["eval"])
    assert data["items"] == len(ALL)
    assert data["katex"] >= len(ALL), f"渲染数量不足：{data['katex']}"
    assert data["errors"] == 0, data["bad"][:2]
    assert data["zeroSized"] == 0, "存在尺寸为 0 的公式（说明 CSS/字体没生效）"
    assert data["figureOk"] and data["figureIsImg"]
    # 题图是 <img>：SVG 包成 data:image/svg+xml（<img> 内 SVG 不执行脚本，安全）
    assert data["chemOk"] and data["chemImgOk"], data
    assert data["chemSrcHasSvg"] and data["chemStrokeKept"], data
    assert "KaTeX_Main" in data["fontFamily"]
    assert out_png.exists() and out_png.stat().st_size > 5000
