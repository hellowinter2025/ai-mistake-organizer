"""全局配置与路径管理。

设计要点：
- 所有运行时数据落在 ``data/``，配置落在 ``data/config.json``（首次启动自动生成）；
  该文件含 API Key，权限设为仅当前用户可读，且永不进入 git。
- 服务默认只绑定 127.0.0.1，不对外监听。
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = BASE_DIR / "web"
# 允许用环境变量把运行时数据放到别处（测试隔离 / 便携使用）
DATA_DIR = Path(os.environ.get("MM_DATA_DIR") or (BASE_DIR / "data")).resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
CROP_DIR = DATA_DIR / "crops"
EXPORT_DIR = DATA_DIR / "exports"
CACHE_DIR = DATA_DIR / "cache"
LOG_DIR = Path(os.environ.get("MM_LOG_DIR") or (BASE_DIR / "logs")).resolve()
CONFIG_PATH = DATA_DIR / "config.json"
DB_PATH = DATA_DIR / "app.db"

for _d in (DATA_DIR, UPLOAD_DIR, CROP_DIR, EXPORT_DIR, CACHE_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

APP_NAME = "AI 错题整理"
APP_VERSION = "1.0.0"

# 上传限制：单张图片 20MB，解码后像素上限（防解压炸弹）
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 80_000_000
MAX_IMAGES_PER_UPLOAD = 10
# 长边缩放上限，兼顾识别精度与 AI 传输体积
MAX_WORK_EDGE = 2400

DEFAULT_SETTINGS: dict[str, Any] = {
    "server": {
        "host": "127.0.0.1",
        "port": 8765,
    },
    "ai": {
        # 当前使用的配置（来自 ai_api_config 表导入）
        "active_config_id": None,
        "active_model": None,
        # 单题解析最大并发
        "concurrency": 2,
        "temperature": 0.2,
        "timeout_seconds": 180,
        # 是否优先尝试 response_format=json_object
        "prefer_json_mode": True,
    },
    "segment": {
        # 行投影切块参数（可在界面调节）
        "gap_ratio": 1.6,
        "min_block_height": 26,
        "red_threshold": 0.012,
        "detect_columns": True,
        "ignore_top_ratio": 0.0,
    },
    # 学科与界面通用设置（支持多学科，不再默认锁死数学）
    "general": {
        "subject": "数学",
        "subjects": ["数学", "物理", "化学", "生物", "语文", "英语", "历史", "地理", "政治", "科学"],
    },
    "export": {
        "paper_title": "",
        "student_name": "",
        "show_answer": True,
        "body_font": "宋体",
        "title_font": "方正小标宋简体",
        "font_size_pt": 10.5,
    },
    "configs": [
        # {"id": 1, "name": "OpenAI", "base_url": "...", "api_key": "...",
        #  "models": ["gpt-4o"], "supports_json": null}
    ],
}


def _secure_write(path: Path, data: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)


class Settings:
    """线程安全的配置容器，写入即落盘。"""

    def __init__(self, path: Path = CONFIG_PATH) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._data: dict[str, Any] = json.loads(json.dumps(DEFAULT_SETTINGS))
        self.load()

    # ---------- 基础读写 ----------
    def load(self) -> None:
        with self._lock:
            if self.path.exists():
                try:
                    raw = json.loads(self.path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:  # 配置损坏不应阻塞启动
                    backup = self.path.with_suffix(".broken.json")
                    try:
                        self.path.replace(backup)
                    except OSError:
                        pass
                    print(f"[config] 配置文件损坏，已备份到 {backup}: {exc}", file=sys.stderr)
                    raw = {}
                _drop_dead_keys(raw)
                self._data = _deep_merge(json.loads(json.dumps(DEFAULT_SETTINGS)), raw)
            else:
                self.save()

    def update(self, patch: dict[str, Any]) -> None:
        with self._lock:
            _drop_dead_keys(patch)
            self._data = _deep_merge(self._data, patch)
            self.save()

    def save(self) -> None:
        with self._lock:
            _secure_write(self.path, json.dumps(self._data, ensure_ascii=False, indent=2))

    # ---------- 访问 ----------
    def get(self, *keys: str, default: Any = None) -> Any:
        with self._lock:
            cur: Any = self._data
            for k in keys:
                if not isinstance(cur, dict) or k not in cur:
                    return default
                cur = cur[k]
            return cur

    def set(self, value: Any, *keys: str, save: bool = True) -> None:
        if not keys:
            raise ValueError("keys required")
        with self._lock:
            cur = self._data
            for k in keys[:-1]:
                nxt = cur.get(k)
                if not isinstance(nxt, dict):
                    nxt = {}
                    cur[k] = nxt
                cur = nxt
            cur[keys[-1]] = value
            if save:
                self.save()

    # ---------- AI 配置 ----------
    @property
    def configs(self) -> list[dict[str, Any]]:
        return self.get("configs", default=[]) or []

    def config_by_id(self, config_id: int | None) -> dict[str, Any] | None:
        if config_id is None:
            return None
        for cfg in self.configs:
            if int(cfg.get("id", -1)) == int(config_id):
                return cfg
        return None

    def active_config(self) -> tuple[dict[str, Any], str] | None:
        """返回 (配置字典, 模型名)；未配置时返回 None。"""
        cfg = self.config_by_id(self.get("ai", "active_config_id"))
        model = self.get("ai", "active_model")
        if cfg is None and self.configs:
            cfg = self.configs[0]
            models = cfg.get("models") or []
            model = model or (models[0] if models else None)
        if cfg is None:
            return None
        models = cfg.get("models") or []
        if not model:
            model = models[0] if models else (cfg.get("default_model") or "")
        if not model:
            return None
        return cfg, str(model)

    def set_json_capability(self, config_id: int, model: str, supported: bool) -> None:
        """记录 (配置, 模型) 是否支持 JSON 输出，避免每次都试错。"""
        with self._lock:
            caps = self._data.setdefault("json_capability", {})
            caps[f"{config_id}::{model}"] = {
                "supported": supported,
                "checked_at": _now_iso(),
            }
            self.save()

    def json_capability(self, config_id: int, model: str) -> bool | None:
        cap = (self._data.get("json_capability") or {}).get(f"{config_id}::{model}")
        if not cap:
            return None
        return bool(cap.get("supported"))

    def public_view(self) -> dict[str, Any]:
        """给前端的脱敏视图（不含 api_key）。"""
        out = json.loads(json.dumps({k: v for k, v in self._data.items() if k != "configs"}))
        out["configs"] = [config_public(c) for c in self.configs]
        return out

    def save_config(self, payload: dict[str, Any], config_id: int | None = None) -> dict[str, Any]:
        """新增或修改一条 AI 服务配置，返回脱敏后的这一条。

        约定（前端不需要知道旧 key 也能改其它字段）：
        - ``api_key`` 字段**缺省**或为空串 → 保留原 key；
        - ``api_key`` 显式传 ``None`` → 清空 key；
        - ``models`` 传空数组 → 清空已缓存模型列表。
        """
        name = str(payload.get("name") or "").strip()
        base_url = str(payload.get("base_url") or "").strip().rstrip("/")
        if not name:
            raise ValueError("服务名称不能为空")
        if not base_url:
            raise ValueError("接口地址不能为空")
        if not base_url.lower().startswith(("http://", "https://")):
            raise ValueError("接口地址必须以 http:// 或 https:// 开头")

        raw_models = payload.get("models")
        if raw_models is None:
            models_in: list[str] | None = None
        elif isinstance(raw_models, str):
            models_in = [m.strip() for m in re.split(r"[\s,;，、]+", raw_models) if m.strip()]
        else:
            models_in = [str(m).strip() for m in raw_models if str(m).strip()]
        if models_in is not None:
            models_in = list(dict.fromkeys(models_in))[:200]

        with self._lock:
            configs = [dict(c) for c in self.configs]
            existing = None
            if config_id is not None:
                for cfg in configs:
                    if int(cfg.get("id", -1)) == int(config_id):
                        existing = cfg
                        break
                if existing is None:
                    raise KeyError("配置不存在")
            else:
                new_id = (max([int(c.get("id", 0)) for c in configs], default=0) or 0) + 1
                existing = {"id": new_id, "api_key": ""}
                configs.append(existing)

            existing["name"] = name
            existing["base_url"] = base_url
            if "api_key" in payload:
                key = payload["api_key"]
                if key is None:
                    existing["api_key"] = ""
                else:
                    key = str(key).strip()
                    if key:
                        existing["api_key"] = key
            if models_in is not None:
                existing["models"] = models_in

            self._data["configs"] = configs
            ai = self._data.setdefault("ai", {})
            # 第一条配置自动成为当前配置，省得用户再点一次
            if ai.get("active_config_id") in (None, "") or not self.config_by_id(ai.get("active_config_id")):
                ai["active_config_id"] = existing.get("id")
                if models_in:
                    ai["active_model"] = models_in[0]
            self.save()
            return config_public(existing)

    def delete_config(self, config_id: int) -> bool:
        with self._lock:
            configs = [c for c in self.configs if int(c.get("id", -1)) != int(config_id)]
            if len(configs) == len(self.configs):
                return False
            self._data["configs"] = configs
            ai = self._data.setdefault("ai", {})
            if int(ai.get("active_config_id") or -1) == int(config_id):
                ai["active_config_id"] = configs[0].get("id") if configs else None
                ai["active_model"] = (configs[0].get("models") or [None])[0] if configs else None
            self.save()
            return True


def config_public(cfg: dict[str, Any]) -> dict[str, Any]:
    """单条配置的脱敏视图。"""
    return {
        "id": cfg.get("id"),
        "name": cfg.get("name"),
        "base_url": cfg.get("base_url"),
        "models": cfg.get("models") or [],
        "has_key": bool(cfg.get("api_key")),
        "key_hint": _mask_key(cfg.get("api_key") or ""),
    }


def _mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 10:
        return "*" * len(key)
    return f"{key[:6]}…{key[-4:]}"


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# 已经废弃的设置项：老配置文件里可能还留着，读到就丢掉，免得看着像还在生效。
DEAD_KEYS: dict[str, tuple[str, ...]] = {"ai": ("max_tokens",)}


def _drop_dead_keys(raw: dict[str, Any]) -> None:
    """就地删掉废弃设置。

    ``ai.max_tokens`` 就是典型：以前请求里写死 4096，长解析/长 TikZ 会被
    拦腰截断，落库后就是半截 JSON —— 现在完全不传，由服务端按模型默认走。
    """
    for section, keys in DEAD_KEYS.items():
        block = raw.get(section)
        if isinstance(block, dict):
            for key in keys:
                block.pop(key, None)


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


settings = Settings()

# 会话令牌：前端从 /api/bootstrap 获取，之后所有写操作需带 X-App-Token。
# 用途是配合 Origin 校验，抵御本机其他站点发起的 CSRF。
#
# 关键点：令牌必须**跨进程重启保持稳定**。早期版本每次启动都重新生成，
# 导致「页面开着、服务重启」后所有写请求（含上传）全部 403，
# 用户看到的就是一条莫名其妙的 Failed to load resource。
TOKEN_PATH = DATA_DIR / ".session_token"
TOKEN_MIN_LEN = 32


def _load_or_create_token() -> str:
    try:
        token = TOKEN_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        token = ""
    if len(token) >= TOKEN_MIN_LEN:
        return token
    token = secrets.token_urlsafe(32)
    try:
        _secure_write(TOKEN_PATH, token)  # 0600，与 config.json 同级
    except OSError:
        pass
    return token


# 测试可用 MM_APP_TOKEN 注入固定令牌
APP_TOKEN = os.environ.get("MM_APP_TOKEN") or _load_or_create_token()
STARTED_AT = _now_iso()

# 单次请求体上限：前端按 24MB 分批，留足 multipart 开销
MAX_REQUEST_BYTES = 64 * 1024 * 1024
