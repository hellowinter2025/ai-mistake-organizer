"""从站点数据库导入 AI 配置（ai_api_config 表）。

用法：
    .venv/Scripts/python.exe tools/import_ai_config.py
    .venv/Scripts/python.exe tools/import_ai_config.py --mysql main:pass@127.0.0.1/main
    .venv/Scripts/python.exe tools/import_ai_config.py --from-json data/_ai_config_raw.json

三种输入方式二选一：直接给 MySQL DSN、给一个 PHP 数据库配置文件、或给一份导出好的 JSON。
导入后 API Key 只写入本地 data/config.json（0600 权限），不会出现在前端或日志里。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402

def parse_db_config_php(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    out: dict[str, str] = {}
    for key in ("host", "dbname", "username", "password"):
        m = re.search(rf"'{key}'\s*=>\s*'([^']*)'", text)
        if m:
            out[key] = m.group(1)
    return out


def load_from_mysql(dsn: str) -> list[dict]:
    import pymysql

    user, rest = dsn.split(":", 1)
    password, rest = rest.split("@", 1)
    host, database = rest.split("/", 1)
    conn = pymysql.connect(host=host, user=user, password=password, database=database, charset="utf8mb4")
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT id,name,base_url,api_key,models,is_default,sort_order "
                "FROM ai_api_config ORDER BY sort_order, id"
            )
            return list(cur.fetchall())
    finally:
        conn.close()


def normalise(rows: list[dict]) -> list[dict]:
    out = []
    for row in rows:
        models_raw = row.get("models")
        models: list[str] = []
        if models_raw:
            try:
                parsed = json.loads(models_raw) if isinstance(models_raw, str) else models_raw
                if isinstance(parsed, list):
                    models = [str(m) for m in parsed if str(m).strip()]
            except json.JSONDecodeError:
                models = [m.strip() for m in str(models_raw).split(",") if m.strip()]
        out.append(
            {
                "id": int(row["id"]),
                "name": str(row.get("name") or f"配置{row['id']}"),
                "base_url": str(row.get("base_url") or "").rstrip("/"),
                "api_key": str(row.get("api_key") or ""),
                "models": models,
                "supports_json": None,
                "is_default": bool(row.get("is_default")),
                "sort_order": int(row.get("sort_order") or 0),
            }
        )
    return out


def merge_known_models(configs: list[dict]) -> None:
    """对常见服务预填模型名，省去一次 /models 探测。"""
    known = {
        "api.deepseek.com": ["deepseek-chat", "deepseek-reasoner"],
        "api.openai.com": ["gpt-4o", "gpt-4o-mini", "gpt-4.1"],
        "dashscope.aliyuncs.com": ["qwen-vl-max", "qwen-plus", "qwen-max"],
        "open.bigmodel.cn": ["glm-4v-plus", "glm-4-plus", "glm-4-flash"],
        "api.moonshot.cn": ["moonshot-v1-32k-vision-preview", "moonshot-v1-32k"],
        "api.siliconflow.cn": ["Qwen/Qwen2.5-VL-72B-Instruct"],
        "generativelanguage.googleapis.com": ["gemini-2.0-flash"],
    }
    for cfg in configs:
        if cfg["models"]:
            continue
        for host, models in known.items():
            if host in cfg["base_url"]:
                cfg["models"] = models
                break


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mysql", help="user:password@host/database")
    ap.add_argument("--config-php", help="PHP 配置文件路径（含 host/dbname/username/password）")
    ap.add_argument("--from-json", help="从导出的 JSON 文件导入")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    rows: list[dict] = []
    if args.from_json:
        rows = json.loads(Path(args.from_json).read_text(encoding="utf-8"))
        print(f"[import] 从 JSON 读取 {len(rows)} 条配置")
    else:
        if args.mysql:
            dsn = args.mysql
        elif not args.config_php:
            print("[import] 请用 --mysql DSN、--config-php 文件或 --from-json 指定数据来源", file=sys.stderr)
            return 2
        else:
            php = Path(args.config_php)
            if not php.exists():
                print(f"[import] 找不到数据库配置 {php}，请用 --mysql 显式指定", file=sys.stderr)
                return 2
            db_cfg = parse_db_config_php(php)
            dsn = f"{db_cfg['username']}:{db_cfg['password']}@{db_cfg['host']}/{db_cfg['dbname']}"
            print(f"[import] 使用 {php.name} 中的连接信息：{db_cfg['username']}@{db_cfg['host']}/{db_cfg['dbname']}")
        try:
            rows = load_from_mysql(dsn)
        except Exception as exc:  # noqa: BLE001
            print(f"[import] 读取数据库失败：{exc}", file=sys.stderr)
            return 3
        print(f"[import] 从 ai_api_config 读取 {len(rows)} 条配置")

    configs = normalise(rows)
    merge_known_models(configs)
    if not configs:
        print("[import] 没有可用配置", file=sys.stderr)
        return 4

    default_cfg = next((c for c in configs if c["is_default"]), configs[0])
    active_model = (default_cfg["models"] or [None])[0]

    for c in configs:
        masked = (c["api_key"][:6] + "…" + c["api_key"][-4:]) if len(c["api_key"]) > 12 else "***"
        print(f"  #{c['id']:<3} {c['name']:<14} {c['base_url']:<45} key={masked:<18} models={c['models'] or '自动获取'}")

    if args.dry_run:
        print("[import] dry-run，未写入")
        return 0

    settings.set(configs, "configs")
    settings.set(int(default_cfg["id"]), "ai", "active_config_id")
    settings.set(active_model, "ai", "active_model")
    print(f"[import] 已写入 {len(configs)} 条配置；默认使用 #{default_cfg['id']} {default_cfg['name']} / {active_model or '待选择模型'}")
    print(f"[import] 配置文件：{settings.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
