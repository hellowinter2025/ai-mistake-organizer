"""API 层测试：上传、解析、题库、组卷、安全策略。"""
from __future__ import annotations

import io

import pytest
from PIL import Image

from app.ai.client import SolveResult


def _png_bytes(page_image) -> bytes:
    buf = io.BytesIO()
    Image.open(page_image["path"]).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture()
def uploaded(client, page_image):
    files = {"files": ("page.png", _png_bytes(page_image), "image/png")}
    res = client.post("/api/images", files=files)
    assert res.status_code == 200, res.text
    return res.json()["images"][0]


def test_bootstrap(client):
    res = client.get("/api/bootstrap")
    assert res.status_code == 200
    data = res.json()
    assert data["token"]
    assert "settings" in data


def test_upload_rejects_non_image(client):
    res = client.post("/api/images", files={"files": ("a.txt", b"hello", "text/plain")})
    assert res.status_code == 400


def test_upload_rejects_truncated_image(client):
    res = client.post("/api/images", files={"files": ("a.png", b"\x89PNG\r\n\x1a\nbroken", "image/png")})
    assert res.status_code == 400


def test_upload_skips_auto_segmentation(uploaded, page_image):
    """自动切题已下线：上传后不再产生候选框，题目由用户自己框。"""
    assert uploaded["id"]
    assert uploaded["width"] == page_image["width"]
    assert uploaded["candidates"] == []


def _manual_box() -> dict:
    """手动框选模式下没有自动候选，测试自己给一个框（对应第 1 题区域）。"""
    return {"x": 60, "y": 110, "w": 1100, "h": 170}


def test_reanalyse(uploaded, client):
    res = client.post(f"/api/images/{uploaded['id']}/analyze", json={"options": {"gap_ratio": 1.2}})
    assert res.status_code == 200
    assert res.json()["candidates"]


def test_missing_token_is_rejected(client, page_image):
    res = client.post(
        "/api/images",
        files={"files": ("page.png", _png_bytes(page_image), "image/png")},
        headers={"X-App-Token": "wrong"},
    )
    assert res.status_code == 403


def test_cross_origin_rejected(client, page_image):
    res = client.post(
        "/api/images",
        files={"files": ("page.png", _png_bytes(page_image), "image/png")},
        headers={"Origin": "http://evil.example"},
    )
    assert res.status_code == 403


def test_bad_host_rejected(client):
    res = client.get("/api/health", headers={"Host": "evil.example"})
    assert res.status_code == 421


def test_solve_requires_bbox(client):
    res = client.post("/api/ai/solve", json={"image_id": "x"})
    assert res.status_code == 400


def test_solve_with_mocked_model(client, uploaded, monkeypatch):
    async def fake_solve(image_bytes, **kwargs):
        assert image_bytes[:2] == b"\xff\xd8"  # 传给模型的是 JPEG
        return SolveResult(
            data={
                "question_text": "已知 $x^2-4x+3$，求最小值。",
                "answer": "$-1$",
                "error_analysis": "没有配方。",
                "solution_fix": "$$(x-2)^2-1$$",
                "solution_alt": "",
                "knowledge_points": ["二次函数"],
                "difficulty": 2,
                "figure_svg": "",
                "needs_figure": False,
            },
            raw="{}",
            strategy="json:strict",
            degraded=False,
            warnings=[],
            model="fake-model",
            config_name="fake",
            log_id=1,
            latency_ms=12,
            mode="json",
        )

    monkeypatch.setattr("app.ai.client.solve_question", fake_solve)
    b = _manual_box()
    res = client.post(
        "/api/ai/solve",
        json={"image_id": uploaded["id"], "bbox": {"x": b["x"], "y": b["y"], "w": b["w"], "h": b["h"]}},
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["fields"]["answer"] == "$-1$"
    assert data["crop"]["url"].startswith("/api/crops/")
    assert data["meta"]["strategy"] == "json:strict"


def test_question_crud(client):
    created = client.post(
        "/api/questions",
        json={
            "stem": "设有理数 $a$，求 $a+\\frac{1}{a}$ 的最小值。",
            "answer": "$2$",
            "error_reason": "没有说明取等条件。",
            "solution_fix": "由基本不等式得 $a+\\frac{1}{a}\\ge 2$。",
            "knowledge_points": ["基本不等式"],
            "difficulty": 3,
            "question_type": "解答题",
        },
    )
    assert created.status_code == 200, created.text
    q = created.json()["question"]
    assert q["uid"]

    listed = client.get("/api/questions?keyword=基本不等式")
    assert listed.json()["total"] >= 1

    patched = client.patch(f"/api/questions/{q['uid']}", json={"starred": True, "difficulty": 4})
    assert patched.status_code == 200
    assert patched.json()["question"]["starred"] is True
    assert patched.json()["question"]["difficulty"] == 4

    starred = client.get("/api/questions?starred=true")
    assert starred.json()["total"] >= 1

    assert client.delete(f"/api/questions/{q['uid']}").status_code == 200
    assert client.get(f"/api/questions/{q['uid']}").status_code == 404


def test_question_rejects_empty_stem(client):
    assert client.post("/api/questions", json={"stem": "  "}).status_code == 400


def test_question_sanitises_svg(client):
    bad = '<svg viewBox="0 0 10 10"><script>alert(1)</script><circle cx="5" cy="5" r="3"/></svg>'
    res = client.post("/api/questions", json={"stem": "如图", "figure_svg": bad})
    q = res.json()["question"]
    assert "script" not in (q["figure_svg"] or "").lower()
    assert q["figure_svg"]


def test_paper_flow(client):
    ids = []
    for i in range(3):
        res = client.post("/api/questions", json={"stem": f"题目{i + 1}，$x^{i + 1}$", "answer": str(i)})
        ids.append(res.json()["question"]["uid"])
    paper = client.post(
        "/api/papers",
        json={
            "title": "接口测试卷",
            "student_name": "李四",
            "exam_date": "2026-02-03",
            "question_uids": ids,
            "options": {"show_answer": True},
        },
    )
    assert paper.status_code == 200, paper.text
    uid = paper.json()["paper"]["uid"]

    listing = client.get("/api/papers").json()["items"]
    assert any(p["uid"] == uid for p in listing)

    preview = client.get(f"/api/papers/{uid}/preview")
    assert preview.status_code == 200
    assert "接口测试卷" in preview.text

    docx = client.get(f"/api/papers/{uid}/export?fmt=docx")
    assert docx.status_code == 200
    assert docx.content[:2] == b"PK"

    assert client.delete(f"/api/papers/{uid}").status_code == 200


def test_paper_requires_questions(client):
    res = client.post("/api/papers", json={"title": "空卷", "question_uids": []})
    assert res.status_code == 400


def test_settings_update(client):
    res = client.patch("/api/settings", json={"ai": {"concurrency": 99}, "segment": {"gap_ratio": 1.8}})
    assert res.status_code == 200
    assert res.json()["settings"]["ai"]["concurrency"] == 6
    assert res.json()["settings"]["segment"]["gap_ratio"] == 1.8


def test_settings_rejects_unknown_config(client):
    res = client.patch("/api/settings", json={"ai": {"active_config_id": 99999}})
    assert res.status_code == 400


def test_state_and_health(client):
    assert client.get("/api/health").json()["ok"] is True
    state = client.get("/api/state").json()
    assert "stats" in state and "questions" in state["stats"]


def test_static_assets_revalidate_every_load(client):
    """本地应用：改了 js/css 刷新就该生效，不能被浏览器启发式缓存住。"""
    assert client.get("/").headers.get("cache-control") == "no-cache"
    assert client.get("/js/app.js").headers.get("cache-control") == "no-cache"
    assert client.get("/css/app.css").headers.get("cache-control") == "no-cache"
    # 接口数据一律不缓存
    assert client.get("/api/health").headers.get("cache-control") == "no-store"


def test_csp_allows_blob_worker(client):
    """浏览器扩展会在页面里建 blob worker，缺这条会刷一片 CSP 报错。"""
    csp = client.get("/").headers.get("content-security-policy", "")
    assert "worker-src 'self' blob:" in csp
    assert "script-src 'self'" in csp


def test_solve_reports_parse_failure_as_422(client, uploaded, monkeypatch):
    """模型答了但 JSON 坏到修不好 → 422，且提示里要说明原始响应已存档。

    绝不能像旧版那样返回 200 + 一坨原始 JSON 当题干。
    """
    from app.ai.client import AIError

    async def fake_solve(image_bytes, **kwargs):
        raise AIError("AI 返回的是 JSON，但结构损坏到无法解析（Expecting ',' delimiter）",
                      status=422, body='{"question_text": broken')

    monkeypatch.setattr("app.ai.client.solve_question", fake_solve)
    b = _manual_box()
    res = client.post(
        "/api/ai/solve",
        json={"image_id": uploaded["id"], "bbox": {"x": b["x"], "y": b["y"], "w": b["w"], "h": b["h"]}},
    )
    assert res.status_code == 422, res.text
    detail = res.json()["detail"]
    assert "JSON" in detail
    assert "原始响应已存档" in detail


def test_solve_keeps_502_for_upstream_failure(client, uploaded, monkeypatch):
    """上游 5xx / 网络故障仍然是 502，别和"内容不可用"混为一谈。"""
    from app.ai.client import AIError

    async def fake_solve(image_bytes, **kwargs):
        raise AIError("AI 接口返回 503：busy", status=503)

    monkeypatch.setattr("app.ai.client.solve_question", fake_solve)
    b = _manual_box()
    res = client.post(
        "/api/ai/solve",
        json={"image_id": uploaded["id"], "bbox": {"x": b["x"], "y": b["y"], "w": b["w"], "h": b["h"]}},
    )
    assert res.status_code == 502, res.text


# ---------------------------------------------------------------- 试卷来源 / 默认卷名

def test_paper_default_title_is_date_plus_cuoti(client):
    """不填标题时默认叫「M月D日错题」，不再是「数学错题专项练习」。"""
    from datetime import date

    res = client.post("/api/questions", json={"stem": "默认标题测试题"})
    uid = res.json()["question"]["uid"]
    paper = client.post("/api/papers", json={"title": "", "question_uids": [uid]})
    assert paper.status_code == 200, paper.text
    today = date.today()
    assert paper.json()["paper"]["title"] == f"{today.month}月{today.day}日错题"
    client.delete(f"/api/papers/{paper.json()['paper']['uid']}")


def test_paper_source_roundtrip(client):
    res = client.post("/api/questions", json={"stem": "来源测试题"})
    uid = res.json()["question"]["uid"]
    paper = client.post("/api/papers", json={
        "title": "来源测试卷", "source": "XX中学高三第一次月考", "question_uids": [uid],
    })
    assert paper.status_code == 200, paper.text
    data = paper.json()["paper"]
    assert data["source"] == "XX中学高三第一次月考"
    preview = client.get(f"/api/papers/{data['uid']}/preview").text
    assert "XX中学高三第一次月考" in preview
    client.delete(f"/api/papers/{data['uid']}")


# ---------------------------------------------------------------- 题图裁剪

def _make_question_with_crop(client, monkeypatch, uploaded):
    """造一道带题目截图的题（借用 mock 的 solve 拿 crop_token）。"""
    async def fake_solve(image_bytes, **kwargs):
        return SolveResult(
            data={"question_text": "如图，求面积。", "answer": "12",
                  "knowledge_points": [], "figure_svg": "", "needs_figure": False},
            raw="{}", strategy="json:strict", degraded=False, warnings=[],
            model="fake", config_name="fake", log_id=1, latency_ms=5, mode="json",
        )

    monkeypatch.setattr("app.ai.client.solve_question", fake_solve)
    b = _manual_box()
    solved = client.post(
        "/api/ai/solve",
        json={"image_id": uploaded["id"], "bbox": {"x": b["x"], "y": b["y"], "w": b["w"], "h": b["h"]}},
    )
    token = solved.json()["crop"]["token"]
    created = client.post(
        "/api/questions",
        json={"stem": "如图，求面积。", "crop_token": token, "image_id": uploaded["id"]},
    )
    return created.json()["question"]


def test_figure_crop_keeps_only_selected_region(client, uploaded, monkeypatch):
    """框选后只保留图形区域；导出层据此不再贴整块题目截图。"""
    from PIL import Image

    q = _make_question_with_crop(client, monkeypatch, uploaded)
    assert q["crop_url"], "建题时应当带上题目截图"
    assert q["figure_crop_url"] is None

    src = client.get(q["crop_url"])
    full = Image.open(io.BytesIO(src.content))
    res = client.patch(
        f"/api/questions/{q['uid']}",
        json={"figure_box": {"x": 0.0, "y": 0.5, "w": 1.0, "h": 0.5}},
    )
    assert res.status_code == 200, res.text
    data = res.json()["question"]
    assert data["figure_crop_url"], "裁剪后应当生成 figure_crop"
    assert data["figure_box"] == [0.0, 0.5, 1.0, 0.5]

    cropped = Image.open(io.BytesIO(client.get(data["figure_crop_url"]).content))
    assert abs(cropped.width - full.width) <= 2, (cropped.size, full.size)
    assert abs(cropped.height - full.height / 2) <= 2, (cropped.size, full.size)

    # 清除后回到整块截图
    cleared = client.patch(f"/api/questions/{q['uid']}", json={"figure_box": None})
    assert cleared.json()["question"]["figure_crop_url"] is None
    client.delete(f"/api/questions/{q['uid']}")


def test_re_enhance_rebuilds_from_original(client, uploaded, monkeypatch):
    """「重新增强」从原始截图重算：不叠加处理，返回新地址。"""
    from PIL import Image

    q = _make_question_with_crop(client, monkeypatch, uploaded)
    first = client.patch(
        f"/api/questions/{q['uid']}",
        json={"figure_box": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}},
    ).json()["question"]
    assert first["figure_crop_url"]

    res = client.post(f"/api/questions/{q['uid']}/re-enhance")
    assert res.status_code == 200, res.text
    again = res.json()["question"]
    assert again["figure_crop_url"], "重新增强后应当仍有题图"
    img = Image.open(io.BytesIO(client.get(again["figure_crop_url"]).content))
    assert img.size == Image.open(io.BytesIO(client.get(first["figure_crop_url"]).content)).size
    client.delete(f"/api/questions/{q['uid']}")


def test_re_enhance_requires_box(client, uploaded, monkeypatch):
    """没框选过就没有可重算的区域，给明确的 400 而不是 500。"""
    q = _make_question_with_crop(client, monkeypatch, uploaded)
    res = client.post(f"/api/questions/{q['uid']}/re-enhance")
    assert res.status_code == 400
    client.delete(f"/api/questions/{q['uid']}")


def test_figure_crop_ignores_question_without_image(client):
    """没有截图的手工题，传 figure_box 也不能报错。"""
    res = client.post("/api/questions", json={"stem": "手工录入的题"})
    uid = res.json()["question"]["uid"]
    patched = client.patch(f"/api/questions/{uid}", json={"figure_box": {"x": 0, "y": 0, "w": 1, "h": 1}})
    assert patched.status_code == 200
    assert patched.json()["question"]["figure_crop_url"] is None
    client.delete(f"/api/questions/{uid}")


# ---------------------------------------------------------------- 裁切粘贴版 / 学科

def _paper_count(client) -> int:
    return len(client.get("/api/papers").json()["items"])


def test_paper_put_updates_in_place(client):
    """已经有试卷记录时再导出，应该原地更新而不是又建一份。"""
    uid = client.post("/api/questions", json={"stem": "原地更新测试题"}).json()["question"]["uid"]
    before = _paper_count(client)
    paper = client.post("/api/papers", json={"title": "第一版", "question_uids": [uid]}).json()["paper"]
    assert _paper_count(client) == before + 1

    res = client.put(
        f"/api/papers/{paper['uid']}",
        json={
            "title": "第二版",
            "source": "改过的来源",
            "options": {"layout": "cutout"},
            "question_uids": [uid],
        },
    )
    assert res.status_code == 200, res.text
    data = res.json()["paper"]
    assert data["title"] == "第二版"
    assert data["source"] == "改过的来源"
    assert data["options"]["layout"] == "cutout"
    # 试卷库里仍然只有这一份
    assert _paper_count(client) == before + 1
    client.delete(f"/api/papers/{paper['uid']}")
    client.delete(f"/api/questions/{uid}")


def test_paper_layout_option_validated(client):
    uid = client.post("/api/questions", json={"stem": "排版选项测试"}).json()["question"]["uid"]
    paper = client.post(
        "/api/papers",
        json={"title": "排版", "question_uids": [uid], "options": {"layout": "乱填的"}},
    ).json()["paper"]
    assert paper["options"]["layout"] == "standard"
    client.delete(f"/api/papers/{paper['uid']}")
    client.delete(f"/api/questions/{uid}")


def test_paper_subject_defaults_to_settings_and_can_override(client):
    uid = client.post("/api/questions", json={"stem": "学科测试题"}).json()["question"]["uid"]
    default = client.post("/api/papers", json={"title": "默认学科", "question_uids": [uid]}).json()["paper"]
    assert default["subject"] == "数学"

    physics = client.post(
        "/api/papers",
        json={"title": "物理卷", "subject": "物理", "question_uids": [uid]},
    ).json()["paper"]
    assert physics["subject"] == "物理"
    assert "学科：物理" in client.get(f"/api/papers/{physics['uid']}/preview").text

    client.delete(f"/api/papers/{default['uid']}")
    client.delete(f"/api/papers/{physics['uid']}")
    client.delete(f"/api/questions/{uid}")


def test_question_subject_roundtrip(client):
    """题目可以带学科，题库能按学科筛选。"""
    made = []
    for subject, stem in (("数学", "数学题一"), ("语文", "语文题一")):
        res = client.post("/api/questions", json={"stem": stem, "subject": subject})
        assert res.json()["question"]["subject"] == subject
        made.append(res.json()["question"]["uid"])

    data = client.get("/api/questions", params={"subject": "语文", "limit": 300}).json()
    subjects = {q["subject"] for q in data["items"]}
    assert subjects <= {"语文"}
    assert any(q["uid"] == made[1] for q in data["items"])
    assert not any(q["uid"] == made[0] for q in data["items"])
    for uid in made:
        client.delete(f"/api/questions/{uid}")


def test_cutout_preview_only_has_stem_and_error(client):
    uid = client.post(
        "/api/questions",
        json={
            "stem": "裁切预览题干",
            "error_reason": "把符号写反了",
            "answer": "答案不该出现",
            "solution_fix": "解析不该出现",
        },
    ).json()["question"]["uid"]
    paper = client.post(
        "/api/papers",
        json={"title": "裁切卷", "question_uids": [uid], "options": {"layout": "cutout"}},
    ).json()["paper"]
    html = client.get(f"/api/papers/{paper['uid']}/preview").text
    assert "裁切预览题干" in html
    assert "把符号写反了" in html
    assert "解析不该出现" not in html
    assert "答案不该出现" not in html
    assert "class=\"cut-item\"" in html
    client.delete(f"/api/papers/{paper['uid']}")
    client.delete(f"/api/questions/{uid}")


# ---------------------------------------------------------------- 关闭服务

def test_shutdown_endpoint_fires_registered_hook(client):
    """网页上的 ⏻ 走这里：触发 uvicorn 的优雅退出，而不是硬杀进程。"""
    from app import runtime

    fired: list[int] = []
    runtime.reset_hooks()
    runtime.on_shutdown(lambda: fired.append(1))
    try:
        info = client.get("/api/system").json()
        assert info["can_shutdown"] is True

        res = client.post("/api/shutdown")
        assert res.status_code == 200, res.text
        assert res.json() == {"ok": True, "scheduled": True}
        assert fired == [1], "回调应当被调用一次"
    finally:
        runtime.reset_hooks()


def test_shutdown_reports_unmanaged_instance(client):
    """测试/裸跑 FastAPI 时没有可退出的 server，只报告、不真关。"""
    from app import runtime

    runtime.reset_hooks()
    try:
        assert client.get("/api/system").json()["can_shutdown"] is False
        res = client.post("/api/shutdown")
        assert res.status_code == 200
        assert res.json()["scheduled"] is False
    finally:
        runtime.reset_hooks()


def test_shutdown_requires_token(client):
    """关闭服务是写接口，必须带令牌——否则任意网页都能把本地服务关掉。"""
    res = client.post("/api/shutdown", headers={"X-App-Token": "wrong-token"})
    assert res.status_code == 403
    assert res.json()["code"] == "token"


# ---------------------------------------------------------------- 题图 TikZ

TIKZ_TRIANGLE = r"""\begin{tikzpicture}[scale=1]
  \draw[thick] (0,0) -- (6,0) -- (3,4) -- cycle;
  \draw[dashed] (3,0) -- (3,4);
  \draw (3,0) rectangle (3.4,0.4);
  \node[below left] at (0,0) {$B$};
  \node[above] at (3,4) {$A$};
\end{tikzpicture}"""


def _needs_node() -> bool:
    import shutil

    return shutil.which("node") is not None


@pytest.mark.skipif(not _needs_node(), reason="需要 node 运行 TikZ 引擎")
def test_tikz_render_endpoint_returns_svg(client):
    res = client.post("/api/tikz/render", json={"code": TIKZ_TRIANGLE})
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["svg"].lstrip().startswith("<svg")
    assert "viewBox" in data["svg"]


def test_tikz_render_endpoint_rejects_empty(client):
    res = client.post("/api/tikz/render", json={"code": "  "})
    assert res.status_code == 400


@pytest.mark.skipif(not _needs_node(), reason="需要 node 运行 TikZ 引擎")
def test_question_with_tikz_compiles_on_save(client):
    """保存时就把 TikZ 编译成 SVG，导出层不用知道 TikZ 的存在。"""
    res = client.post(
        "/api/questions",
        json={"stem": "如图，$AB=AC=5$，$BC=6$，求面积。", "answer": "$12$", "figure_tikz": TIKZ_TRIANGLE},
    )
    assert res.status_code == 200, res.text
    q = res.json()["question"]
    assert q["figure_tikz"].startswith("\\begin{tikzpicture}")
    assert "<svg" in (q["figure_svg"] or "")
    # 有编译好的图就不该再贴原题截图
    assert q["needs_figure"] is False
    client.delete(f"/api/questions/{q['uid']}")


@pytest.mark.skipif(not _needs_node(), reason="需要 node 运行 TikZ 引擎")
def test_truncated_tikz_is_repaired_on_save(client):
    """模型输出被截断（\\end{tikzpicture 少个括号）也要能出图，并把修好的代码存回去。"""
    broken = TIKZ_TRIANGLE[: -len("\\end{tikzpicture}")] + "\\end{tikzpicture"
    res = client.post("/api/questions", json={"stem": "截断的图", "figure_tikz": broken})
    assert res.status_code == 200, res.text
    q = res.json()["question"]
    assert "<svg" in (q["figure_svg"] or ""), "补全后应当能编译"
    assert q["figure_tikz"].endswith("\\end{tikzpicture}"), "修好的代码要回写，别留坏代码在库里"
    assert q["needs_figure"] is False
    client.delete(f"/api/questions/{q['uid']}")


@pytest.mark.skipif(not _needs_node(), reason="需要 node 运行 TikZ 引擎")
def test_broken_tikz_falls_back_to_source_image(client):
    """编译不出来不阻断保存，只是退回「附原题截图」，源码留着让用户改。"""
    res = client.post(
        "/api/questions",
        json={"stem": "坏图的题", "figure_tikz": r"\begin{tikzpicture}\draw (0,0)--;\end{tikzpicture}"},
    )
    assert res.status_code == 200, res.text
    q = res.json()["question"]
    assert q["needs_figure"] is True
    assert not q["figure_svg"]
    assert q["figure_tikz"], "源码要留着，否则用户没法改"
    client.delete(f"/api/questions/{q['uid']}")


@pytest.mark.skipif(not _needs_node(), reason="需要 node 运行 TikZ 引擎")
def test_patch_question_recompiles_figure(client):
    """编辑器里改完 TikZ 保存，图要跟着变，不用重新跑 AI。"""
    created = client.post(
        "/api/questions",
        json={"stem": "改图测试", "figure_tikz": TIKZ_TRIANGLE},
    ).json()["question"]
    first = created["figure_svg"]

    shorter = r"""\begin{tikzpicture}
  \draw (0,0) -- (2,0) -- (0,1) -- cycle;
\end{tikzpicture}"""
    res = client.patch(f"/api/questions/{created['uid']}", json={"figure_tikz": shorter})
    assert res.status_code == 200, res.text
    q = res.json()["question"]
    assert q["figure_svg"] and q["figure_svg"] != first
    client.delete(f"/api/questions/{created['uid']}")


def test_question_flow_survives_save(client):
    """合成路线的 figure_flow 必须完整入库（前端曾把它漏掉，图整张丢失）。"""
    flow = {
        "steps": [
            {"name": "X", "smiles": "O=Cc1ccc(CO)cc1"},
            {"name": "Y", "smiles": "O=C(O)/C=C/c1ccc(CO)cc1", "label": "CH3COOH / 浓H2SO4, 加热"},
            {"name": "Z", "smiles": "COC(=O)/C=C/c1ccc(CO)cc1", "label": "CH3OH / 浓H2SO4, 加热"},
        ]
    }
    res = client.post("/api/questions", json={"stem": "合成路线题", "figure_flow": flow})
    assert res.status_code == 200, res.text
    q = res.json()["question"]
    saved_flow = q["figure_flow"]
    if isinstance(saved_flow, str):
        import json as _json

        saved_flow = _json.loads(saved_flow)
    names = [s["name"] for s in saved_flow["steps"]]
    assert names == ["X", "Y", "Z"]
    assert saved_flow["steps"][1]["smiles"].startswith("O=C(O)")
    # 保存时应编译出图
    assert "<svg" in (q["figure_svg"] or "")
    assert q["needs_figure"] is False
    client.delete(f"/api/questions/{q['uid']}")


def test_raw_ai_prefers_full_log_over_preview(client, monkeypatch, tmp_path):
    """raw_preview 只有 1500 字（队列存 localStorage 的限制），入库时要按 log_id 取全文。"""
    from app import db
    from app.config import LOG_DIR

    logs = tmp_path / "logs" / "ai"
    logs.mkdir(parents=True)
    full = '{"question_text":"完整响应","figure_flow":{"steps":[{"name":"A"},{"name":"B"}]}}'
    (logs / "raw.txt").write_text(full, encoding="utf-8")
    monkeypatch.setattr("app.config.LOG_DIR", tmp_path / "logs")

    log_id = db.execute(
        "INSERT INTO ai_logs (created_at, ok, raw_path) VALUES (?, 1, ?)",
        ("2026-11-01 00:00:00", "logs/ai/raw.txt"),
    )

    res = client.post(
        "/api/questions",
        json={
            "stem": "raw 测试",
            "ai_meta": {"model": "test", "log_id": log_id},
            "raw_ai": full[:1500],  # 前端带来的截断预览
        },
    )
    assert res.status_code == 200, res.text
    q = res.json()["question"]
    row = db.query_one("SELECT raw_ai FROM questions WHERE uid = ?", (q["uid"],))
    assert row["raw_ai"] == full
    client.delete(f"/api/questions/{q['uid']}")


def test_draft_from_fields_keeps_figure_flow():
    """recognize.js 的字段映射不能再漏 figure_flow（漏了图在入库时整张丢）。"""
    import json as _json
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "web" / "js" / "recognize.js").read_text(
        encoding="utf-8"
    )
    m = re.search(r"function draftFromFields\(fields\)\s*\{(.*?)\n\}", src, re.S)
    assert m, "draftFromFields 不见了"
    body = m.group(1)
    for key in ("figure_svg", "figure_tikz", "figure_smiles", "figure_flow", "needs_figure"):
        assert key in body, f"draftFromFields 漏了 {key}"


def test_queue_roundtrip_server_side(client):
    """队列存服务端 SQLite（替代 localStorage：5MB 配额、多标签页覆盖、换机丢失）。"""
    items = [
        {"id": "q1", "imageId": "a" * 12, "boxId": "b1", "bbox": {"x": 1, "y": 2, "w": 3, "h": 4},
         "status": "done", "fields": {"question_text": "1+1=?"}, "meta": {"raw_preview": "x" * 2000}},
        {"id": "q2", "imageId": "a" * 12, "boxId": "b2", "bbox": {"x": 5, "y": 6, "w": 7, "h": 8},
         "status": "wait"},
        "脏数据应被丢掉",
    ]
    res = client.put("/api/queue", json={"items": items})
    assert res.status_code == 200, res.text
    assert res.json()["total"] == 2

    res = client.get("/api/queue")
    got = res.json()
    assert got["total"] == 2
    by_id = {it["id"]: it for it in got["items"]}
    # 2000 字的 raw_preview 不会被截断（localStorage 时代会）
    assert by_id["q1"]["fields"]["question_text"] == "1+1=?"
    assert len(by_id["q1"]["meta"]["raw_preview"]) == 2000
    assert by_id["q2"]["status"] == "wait"

    # 全量替换语义：再写一次只剩新内容
    res = client.put("/api/queue", json={"items": [{"id": "q3", "status": "saved"}]})
    assert res.json()["total"] == 1
    assert client.get("/api/queue").json()["items"][0]["id"] == "q3"


def test_figure_png_endpoint_renders(client):
    """题图 PNG 按需渲染：前端 <img> 直接指向它，不再自己渲染 SVG。"""
    res = client.post(
        "/api/questions",
        json={"stem": "画乙醇", "figure_smiles": {"乙醇": "CCO"}},
    )
    assert res.status_code == 200, res.text
    q = res.json()["question"]
    assert q["figure_png_url"] == f"/api/questions/{q['uid']}/figure.png"

    png = client.get(q["figure_png_url"])
    assert png.status_code == 200
    assert png.headers["content-type"].startswith("image/png")
    assert png.content[:8] == b"\x89PNG\r\n\x1a\n"
    client.delete(f"/api/questions/{q['uid']}")


def test_framing_a_figure_switches_to_source_image(client, uploaded, monkeypatch):
    """框选图形 = 我要用这张图：保存时应自动勾上 needs_figure，
    否则导出会继续用 AI 重绘的图盖掉它。"""
    q = _make_question_with_crop(client, monkeypatch, uploaded)
    assert q["needs_figure"] is False

    res = client.patch(
        f"/api/questions/{q['uid']}",
        json={"figure_box": {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.4}},
    )
    data = res.json()["question"]
    assert data["figure_crop_url"]
    assert data["needs_figure"] is True, "框选后应该自动改用原题截图"

    # 框没变的情况下，用户主动取消勾选要能保留
    uncheck = client.patch(
        f"/api/questions/{q['uid']}",
        json={"figure_box": data["figure_box"], "needs_figure": False},
    )
    assert uncheck.json()["question"]["needs_figure"] is False
    client.delete(f"/api/questions/{q['uid']}")


def test_saving_strips_json_tail_from_text_fields(client):
    """编辑器里残留着带 JSON 尾巴的旧内容时，保存也要顺手清干净。"""
    dirty = (
        '设 $\\mathbf{u}=\\overrightarrow{AP}$，故应为 $\\frac{1}{\\sqrt{3}}$。",'
        '"knowledge_points":["二面角的平面角"],"difficulty":4,"needs_figure":true'
    )
    res = client.post("/api/questions", json={"stem": "求二面角的正弦值。", "answer": dirty})
    q = res.json()["question"]
    assert "knowledge_points" not in q["answer"]
    assert q["answer"].endswith("$\\frac{1}{\\sqrt{3}}$。")
    client.delete(f"/api/questions/{q['uid']}")


def test_each_question_gets_short_unique_code(client):
    """每道题都有 6 位随机编号：试卷上印它，反馈时一报就能定位。"""
    import re

    codes = []
    for i in range(3):
        res = client.post("/api/questions", json={"stem": f"第 {i} 题"})
        q = res.json()["question"]
        assert re.fullmatch(r"[2-9A-HJKMNP-Z]{6}", q["code"]), q["code"]
        codes.append(q["code"])
        client.delete(f"/api/questions/{q['uid']}")
    assert len(set(codes)) == 3


def test_search_finds_question_by_code(client):
    """搜索框里粘编号也要能找到题。"""
    created = client.post("/api/questions", json={"stem": "编号检索用题"}).json()["question"]
    hit = client.get("/api/questions", params={"keyword": created["code"]}).json()
    assert any(item["uid"] == created["uid"] for item in hit["items"])
    client.delete(f"/api/questions/{created['uid']}")


def test_needs_figure_survives_save_with_tikz(client):
    """勾了「改用原题截图」再保存，勾选不能被重新编译出来的 AI 图覆盖。

    真实反馈（装置图那题）：用户在编辑器里勾了「改用原题截图」，保存后库里
    仍是 0，导出继续用 AI 那张画歪的装置图。原因是保存时 ``_resolve_figure``
    一编译成功就把 needs_figure 无条件改回 False。
    """
    tikz = "\\begin{tikzpicture}\\draw (0,0)--(1,1);\\end{tikzpicture}"
    created = client.post(
        "/api/questions",
        json={"stem": "装置图测试", "figure_tikz": tikz, "needs_figure": True},
    ).json()["question"]
    assert created["needs_figure"] is True, "新建时勾选就要生效"
    assert created["figure_svg"], "图还是应该编译好，方便随时切回来"

    saved = client.patch(
        f"/api/questions/{created['uid']}",
        json={"figure_tikz": tikz, "needs_figure": True},
    ).json()["question"]
    assert saved["needs_figure"] is True, "保存后勾选不能被覆盖"

    # 主动取消勾选（想用 AI 图）时也不能被强行改回
    unchecked = client.patch(
        f"/api/questions/{created['uid']}",
        json={"figure_tikz": tikz, "needs_figure": False},
    ).json()["question"]
    assert unchecked["needs_figure"] is False
    client.delete(f"/api/questions/{created['uid']}")
