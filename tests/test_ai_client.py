"""AI 客户端层：解析失败必须报错，绝不能把原始 JSON 当结果返回。

真实事故（2026-10-29）：模型输出被网关插了空格、中文引号被误转义，
JSON 解析失败后旧代码走 ``text:fallback``，把整段原始 JSON 塞进题干，
用户看到的是满屏 ``\\frac`` 和 ``\\n``。现在改为抛 422 并保留原始响应。
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from app.ai import client as ai_client
from app import db

CFG = {
    "id": 901,
    "name": "单元测试",
    "base_url": "http://127.0.0.1:1/v1",
    "api_key": "test-key",
    "models": ["fake-model"],
}


def _fake_response(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        },
        request=httpx.Request("POST", "http://127.0.0.1:1/v1/chat/completions"),
    )


def _solve(monkeypatch, content: str):
    db.init_db()
    settings = ai_client.settings
    monkeypatch.setattr(settings, "active_config", lambda: (CFG, "fake-model"))
    monkeypatch.setattr(settings, "config_by_id", lambda cid: CFG if cid in (None, 901) else None)
    # 强制文本模式，避免 json 模式降级多跑一轮
    monkeypatch.setattr(settings, "json_capability", lambda cid, model: False)
    monkeypatch.setattr(settings, "set_json_capability", lambda cid, model, ok: None)

    async def fake_post(client, url, headers, body):
        return _fake_response(content)

    monkeypatch.setattr(ai_client, "_post", fake_post)
    return asyncio.run(ai_client.solve_question(b"\x89PNG\r\n\x1a\n", config_id=901, model="fake-model"))


def test_parse_failure_raises_422(monkeypatch):
    """JSON 结构坏了 → AIError(422)，而不是返回一坨原始文本。"""
    with pytest.raises(ai_client.AIError) as box:
        _solve(monkeypatch, '{"question_text" "已知 $f(x)$", "answer": "1"}')
    err = box.value
    assert err.status == 422
    assert "JSON" in str(err)
    assert "question_text" in err.body  # 原始响应随错误返回，便于排查


def test_missing_stem_raises_422(monkeypatch):
    """JSON 通了但没题干 → 同样不能建题。"""
    with pytest.raises(ai_client.AIError) as box:
        _solve(monkeypatch, '{"answer": "1", "difficulty": 3}')
    assert box.value.status == 422
    assert "题目正文" in str(box.value)


def test_repairable_output_still_succeeds(monkeypatch):
    """能修好的（``\\ in``、``1⏎.0``、中文引号）仍然要成功，别一律报错。"""
    content = (
        '{"question_text": "已知 $x \\ in (0,1)$，由“三线合一”得 $k \\\\ge 1$。",\n'
        ' "answer": "1", "confidence": 1\n.0}'
    )
    res = _solve(monkeypatch, content)
    assert res.strategy.startswith("json"), res.strategy
    assert r"\in" in res.data["question_text"]
    assert "三线合一" in res.data["question_text"]
    assert res.data["confidence"] == 1.0
    assert res.degraded is False


def test_text_section_output_is_not_treated_as_error(monkeypatch):
    """模型用 [[题目]] 分节输出是合法格式，不能误判成 JSON 损坏。"""
    content = (
        "[[题目]]\n已知 $x>0$，求 $x+\\frac{1}{x}$ 的最小值。\n\n"
        "[[答案]]\n$2$\n\n[[解析]]\n由基本不等式得 $x+\\frac{1}{x}\\ge 2$。"
    )
    res = _solve(monkeypatch, content)
    assert res.strategy == "text:sections"
    assert "最小值" in res.data["question_text"]
    assert res.data["answer"] == "$2$"


def test_request_body_has_no_max_tokens(monkeypatch):
    """请求体里不许再出现 max_tokens。

    写死 4096 会把长解析、长 TikZ 拦腰截断，落库就是半截 JSON ——
    现在完全不传，由服务端按模型默认上限走。
    """
    captured: dict = {}
    content = '{"question_text":"求 $x$。","answer":"$x=1$"}'

    db.init_db()
    settings = ai_client.settings
    monkeypatch.setattr(settings, "active_config", lambda: (CFG, "fake-model"))
    monkeypatch.setattr(settings, "config_by_id", lambda cid: CFG if cid in (None, 901) else None)
    monkeypatch.setattr(settings, "json_capability", lambda cid, model: False)
    monkeypatch.setattr(settings, "set_json_capability", lambda cid, model, ok: None)

    async def fake_post(client, url, headers, body):
        captured.update(body)
        return _fake_response(content)

    monkeypatch.setattr(ai_client, "_post", fake_post)
    asyncio.run(ai_client.solve_question(b"\x89PNG\r\n\x1a\n", config_id=901, model="fake-model"))

    assert "max_tokens" not in captured
    assert captured["model"] == "fake-model"
