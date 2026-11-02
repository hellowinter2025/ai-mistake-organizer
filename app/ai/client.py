"""OpenAI 兼容的视觉模型客户端。

特性：
- 多配置、多模型；base_url 自动补 ``/chat/completions``；
- 优先 ``response_format={"type":"json_object"}``，遇到不支持自动降级并记忆；
- 单次重试（5xx / 网络错误），超时与并发均受配置约束；
- 全量落盘日志：``logs/ai/YYYY-MM-DD.jsonl`` + 每次原始响应单独存文件，便于复盘。
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from starlette.concurrency import run_in_threadpool

from .. import db
from ..config import LOG_DIR, settings
from . import parser as out_parser
from .prompts import build_messages
from . import prompts as prompt_lib

AI_LOG_DIR = LOG_DIR / "ai"
AI_LOG_DIR.mkdir(parents=True, exist_ok=True)


class AIError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass
class SolveResult:
    data: dict[str, Any]
    raw: str
    strategy: str
    degraded: bool
    warnings: list[str]
    model: str
    config_name: str
    log_id: int
    latency_ms: int
    mode: str
    usage: dict[str, Any] = field(default_factory=dict)


def _endpoint(base_url: str) -> str:
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise AIError("API 地址为空")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def _serialise_body(body: dict[str, Any]) -> dict[str, Any]:
    """日志用：把 base64 图片截断，避免日志爆炸。"""
    clone = json.loads(json.dumps(body, ensure_ascii=False))
    try:
        for msg in clone.get("messages", []):
            content = msg.get("content")
            if isinstance(content, list):
                for part in content:
                    if part.get("type") == "image_url":
                        url = part["image_url"]["url"]
                        part["image_url"]["url"] = url[:64] + f"...({len(url)} chars)"
    except (KeyError, TypeError):
        pass
    return clone


def _write_log(record: dict[str, Any], raw: str | None) -> Path | None:
    day = datetime.now().strftime("%Y-%m-%d")
    raw_path: Path | None = None
    stamp = datetime.now().strftime("%H%M%S_%f")
    if raw is not None:
        raw_path = AI_LOG_DIR / f"{day}_{stamp}.txt"
        raw_path.write_text(raw, encoding="utf-8")
        record["raw_path"] = str(raw_path.relative_to(LOG_DIR.parent))
    with (AI_LOG_DIR / f"{day}.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return raw_path


async def _post(client: httpx.AsyncClient, url: str, headers: dict[str, str], body: dict[str, Any]) -> httpx.Response:
    return await client.post(url, headers=headers, json=body)


def _is_json_mode_rejection(resp: httpx.Response) -> bool:
    if resp.status_code not in (400, 404, 415, 422, 500):
        return False
    text = (resp.text or "").lower()
    return "response_format" in text or "json_object" in text or "json mode" in text or "unsupported" in text


async def solve_question(
    image_bytes: bytes,
    *,
    extra: str = "",
    config_id: int | None = None,
    model: str | None = None,
    image_id: str | None = None,
    question_uid: str | None = None,
    temperature: float | None = None,
    subject: str | None = None,
) -> SolveResult:
    active = settings.active_config()
    cfg = settings.config_by_id(config_id) or (active[0] if active else None)
    if cfg is None:
        raise AIError("尚未配置 AI 服务，请先在设置中导入或填写 API 配置")
    model_name = model or (active[1] if active else None) or (cfg.get("models") or [""])[0]
    if not model_name:
        raise AIError("未指定模型")

    api_key = (cfg.get("api_key") or "").strip()
    if not api_key:
        raise AIError(f"配置「{cfg.get('name')}」缺少 API Key")

    url = _endpoint(cfg.get("base_url", ""))
    image_b64 = base64.b64encode(image_bytes).decode()
    # 学科决定提示词（是否走 LaTeX），没传就用设置里的默认学科
    subject = prompt_lib.normalise_subject(subject or settings.get("general", "subject", default="数学"))

    prefer_json = bool(settings.get("ai", "prefer_json_mode", default=True))
    known = settings.json_capability(int(cfg["id"]), model_name)
    json_mode = prefer_json and (known is not False)
    temperature = settings.get("ai", "temperature", default=0.2) if temperature is None else temperature
    timeout = float(settings.get("ai", "timeout_seconds", default=180))

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    mode_used = "json" if json_mode else "text"
    started = time.perf_counter()
    last_error: Exception | None = None
    raw_text = ""
    usage: dict[str, Any] = {}

    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=20.0)) as client:
        for attempt in range(2):
            body: dict[str, Any] = {
                "model": model_name,
                "messages": build_messages(
                    image_b64, json_mode=json_mode, extra=extra, subject=subject
                ),
                "temperature": temperature,
                "stream": False,
            }
            if json_mode:
                body["response_format"] = {"type": "json_object"}
            try:
                resp = await _post(client, url, headers, body)
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt == 0:
                    await asyncio.sleep(1.2)
                    continue
                break

            if resp.status_code >= 400 and json_mode and _is_json_mode_rejection(resp):
                settings.set_json_capability(int(cfg["id"]), model_name, False)
                json_mode = False
                mode_used = "text(downgraded)"
                continue

            if resp.status_code in (429, 500, 502, 503, 504) and attempt == 0:
                last_error = AIError(f"HTTP {resp.status_code}", status=resp.status_code, body=resp.text[:500])
                await asyncio.sleep(2.0)
                continue

            if resp.status_code >= 400:
                raise AIError(
                    f"AI 接口返回 {resp.status_code}：{_extract_error(resp)}",
                    status=resp.status_code,
                    body=resp.text[:2000],
                )

            try:
                payload = resp.json()
            except json.JSONDecodeError as exc:
                raise AIError(f"AI 返回非 JSON 响应：{resp.text[:300]}") from exc

            raw_text = _extract_content(payload)
            usage = payload.get("usage") or {}
            break
        else:
            raw_text = ""

    latency_ms = int((time.perf_counter() - started) * 1000)

    if not raw_text:
        detail = str(last_error) if last_error else "未知错误"
        log_id = db.log_ai(
            config_id=int(cfg["id"]),
            model=model_name,
            mode=mode_used,
            ok=False,
            error=detail,
            latency_ms=latency_ms,
            image_id=image_id,
        )
        _write_log(
            {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "config": cfg.get("name"),
                "model": model_name,
                "mode": mode_used,
                "ok": False,
                "error": detail,
                "body": _serialise_body(body),
            },
            None,
        )
        raise AIError(f"调用 AI 失败：{detail}")

    try:
        parsed = out_parser.parse(raw_text)
    except out_parser.ParseError as exc:
        # 解析不出来时**必须报错**，而不是把原始 JSON 当题目返回。
        # 原始响应照样落盘，前端能拿到 log_id 去设置里查。
        raw_path = _write_log(
            {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "config": cfg.get("name"),
                "model": model_name,
                "mode": mode_used,
                "latency_ms": latency_ms,
                "usage": usage,
                "strategy": "parse:error",
                "error": str(exc),
                "detail": exc.detail,
                "request": _serialise_body(body),
            },
            raw_text,
        )
        log_id = db.log_ai(
            config_id=int(cfg["id"]),
            model=model_name,
            mode=mode_used,
            ok=False,
            parse_strategy="parse:error",
            error=str(exc),
            latency_ms=latency_ms,
            raw_path=str(raw_path.relative_to(LOG_DIR.parent)) if raw_path else None,
            image_id=image_id,
            question_uid=question_uid,
        )
        raise AIError(
            f"{exc}（{exc.detail}）" if exc.detail else str(exc),
            status=422,
            body=raw_text[:2000],
        ) from exc

    if json_mode and parsed.strategy.startswith("json"):
        settings.set_json_capability(int(cfg["id"]), model_name, True)
    elif json_mode and parsed.strategy.startswith("text"):
        # 声称支持 JSON 却吐了非 JSON，记一次降级，后续仍可用但不强依赖
        parsed.warnings.append("模型声明支持 JSON，但输出未通过解析，已按文本模式处理")
    # 图形编译走 node 子进程，0.5~1.6s，必须挪出事件循环
    if parsed.data.get("figure_tikz"):
        await run_in_threadpool(compile_figure, parsed)

    log_id = db.log_ai(
        config_id=int(cfg["id"]),
        model=model_name,
        mode=mode_used,
        ok=True,
        parse_strategy=parsed.strategy,
        latency_ms=latency_ms,
        tokens_in=(usage.get("prompt_tokens") if isinstance(usage, dict) else None),
        tokens_out=(usage.get("completion_tokens") if isinstance(usage, dict) else None),
        raw_path=None,
        image_id=image_id,
        question_uid=question_uid,
    )
    raw_path = _write_log(
        {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "config": cfg.get("name"),
            "model": model_name,
            "mode": mode_used,
            "latency_ms": latency_ms,
            "usage": usage,
            "strategy": parsed.strategy,
            "degraded": parsed.degraded,
            "warnings": parsed.warnings,
            "request": _serialise_body(body),
        },
        raw_text,
    )
    if raw_path:
        db.execute("UPDATE ai_logs SET raw_path=? WHERE id=?", (str(raw_path.relative_to(LOG_DIR.parent)), log_id))

    return SolveResult(
        data=parsed.data,
        raw=raw_text,
        strategy=parsed.strategy,
        degraded=parsed.degraded,
        usage=usage if isinstance(usage, dict) else {},
        warnings=parsed.warnings,
        model=model_name,
        config_name=str(cfg.get("name") or ""),
        log_id=log_id,
        latency_ms=latency_ms,
        mode=mode_used,
    )


def _compile_flow(data: dict[str, Any], parsed: "out_parser.ParsedResult") -> bool:
    """工艺流程图：结构化描述 → SVG。

    排版（框宽、间距、折行）全由 ``app.render.flow`` 算，模型只给"有哪些步骤、
    箭头上写什么"，所以这一步基本不会失败；真失败了就退回原题截图。
    """
    raw = data.get("figure_flow")
    if not raw:
        return False
    from ..render import flow as flow_mod
    from ..render.svg import sanitize_svg

    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
        svg = sanitize_svg(flow_mod.render(payload))
        if not svg:
            raise flow_mod.FlowError("渲染结果未通过安全校验")
    except Exception as exc:  # noqa: BLE001 - 渲染失败不能挡住整题
        data["figure_flow"] = ""
        parsed.warnings.append(f"工艺流程图未能渲染（{exc}），已改为附原题截图")
        return False
    data["figure_svg"] = svg
    return True


def _compile_smiles(data: dict[str, Any], parsed: "out_parser.ParsedResult") -> bool:
    """化学结构式：SMILES → SVG。成功返回 True。

    SMILES 有严格语法，能程序化判定对错（``MolFromSmiles`` 返回 None 即失败），
    所以这里失败就老老实实退回原题截图，不像 TikZ 那样存在"画歪了也能编译"的
    半吊子状态。
    """
    raw = str(data.get("figure_smiles") or "")
    if not raw:
        return False
    from ..render import chem
    from ..render.svg import sanitize_svg

    try:
        payload: Any = json.loads(raw)
    except json.JSONDecodeError:
        payload = raw  # 历史数据/手工填写可能是裸 SMILES 文本
    try:
        mapping = chem.normalise(payload)
    except Exception:  # noqa: BLE001 - 结构异常一律按“没有可用结构式”处理
        mapping = {}
    if not mapping:
        data["figure_smiles"] = ""
        parsed.warnings.append("AI 给的结构式不是合法 SMILES，已改为附原题截图")
        return False
    # 回写清洗后的映射，编辑器里看到的和画出来的保持一致
    data["figure_smiles"] = json.dumps(mapping, ensure_ascii=False)
    try:
        svg = sanitize_svg(chem.render_svg(mapping))
        if not svg:
            raise chem.ChemError("渲染结果未通过安全校验")
    except Exception as exc:  # noqa: BLE001 - 渲染失败一律降级
        parsed.warnings.append(f"化学结构式未能渲染（{exc}），已改为附原题截图")
        return False
    data["figure_svg"] = svg
    data["needs_figure"] = False
    return True


def compile_figure(parsed: "out_parser.ParsedResult") -> None:
    """把 model 给的图形编译成 SVG，写回 figure_svg。

    优先化学结构式（SMILES），其次 TikZ。失败不是错误——只降级成「附原题截图」，
    并在 warnings 里说明原因，用户在编辑器里还能看到原始代码并自行改。
    """
    data = parsed.data or {}
    if data.get("figure_flow"):
        if _compile_flow(data, parsed):
            return
        data["figure_svg"] = ""
        data["needs_figure"] = True
        return
    if data.get("figure_smiles"):
        if _compile_smiles(data, parsed):
            return
        # 结构式渲染失败：明确退回原图，别让 needs_figure 停在 false
        data["figure_svg"] = ""
        data["needs_figure"] = True
        return
    code = str(data.get("figure_tikz") or "")
    if not code:
        return
    try:
        from ..render import tikz as tikz_mod
        from ..render.svg import sanitize_svg

        result = tikz_mod.render(code)
        svg = sanitize_svg(result.svg)
        if not svg:
            raise tikz_mod.TikzError("编译结果未通过安全校验")
    except Exception as exc:  # noqa: BLE001 - 渲染失败一律降级，不能挡住解析结果
        data["figure_svg"] = ""
        data["needs_figure"] = True
        parsed.warnings.append(f"题图未能编译（{exc}），已改为附原题截图")
        return
    data["figure_svg"] = svg
    data["needs_figure"] = False


def _extract_content(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    msg = (choices[0] or {}).get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") in ("text", "output_text"):
                parts.append(str(item.get("text", "")))
        return "".join(parts).strip()
    return ""


def _extract_error(resp: httpx.Response) -> str:
    try:
        payload = resp.json()
    except json.JSONDecodeError:
        return (resp.text or "")[:300] or "无详情"
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err)[:300]
        if isinstance(err, str):
            return err[:300]
        return str(payload)[:300]
    return str(payload)[:300]


async def list_models(config_id: int) -> list[str]:
    cfg = settings.config_by_id(config_id)
    if not cfg:
        raise AIError("配置不存在")
    base = (cfg.get("base_url") or "").strip().rstrip("/")
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    url = f"{base}/models"
    headers = {"Authorization": f"Bearer {cfg.get('api_key', '')}"}
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(url, headers=headers)
    if resp.status_code >= 400:
        raise AIError(f"获取模型列表失败：{_extract_error(resp)}", status=resp.status_code)
    payload = resp.json()
    items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise AIError("接口未返回模型列表")
    out = []
    for item in items:
        if isinstance(item, dict) and item.get("id"):
            out.append(str(item["id"]))
    return sorted(out)


async def test_config(config_id: int, model: str | None = None) -> dict[str, Any]:
    """轻量连通性测试：发一条纯文本请求，不消耗图片。"""
    cfg = settings.config_by_id(config_id)
    if not cfg:
        raise AIError("配置不存在")
    model_name = model or ((cfg.get("models") or [None])[0])
    if not model_name:
        raise AIError("请先选择模型")
    url = _endpoint(cfg.get("base_url", ""))
    headers = {"Authorization": f"Bearer {cfg.get('api_key', '')}", "Content-Type": "application/json"}
    body = {
        "model": model_name,
        "messages": [{"role": "user", "content": "回复两个字：可用"}],
        "temperature": 0,
        "stream": False,
    }
    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, headers=headers, json=body)
    latency = int((time.perf_counter() - started) * 1000)
    if resp.status_code >= 400:
        raise AIError(f"测试失败（{resp.status_code}）：{_extract_error(resp)}", status=resp.status_code)
    payload = resp.json()
    return {
        "ok": True,
        "latency_ms": latency,
        "model": model_name,
        "reply": _extract_content(payload)[:100],
    }
