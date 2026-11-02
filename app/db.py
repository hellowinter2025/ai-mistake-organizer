"""SQLite 数据层：题库、试卷、图片索引、AI 调用日志。

单文件 SQLite + WAL，适合本机单用户场景。所有写入通过同一把锁串行化，
读取使用线程本地连接，避免 ``database is locked``。
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import uuid
from datetime import datetime
from typing import Any, Iterable

from .config import DB_PATH

_lock = threading.RLock()
_local = threading.local()

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS images (
    id          TEXT PRIMARY KEY,
    path        TEXT NOT NULL,
    orig_name   TEXT,
    width       INTEGER NOT NULL DEFAULT 0,
    height      INTEGER NOT NULL DEFAULT 0,
    sha256      TEXT,
    page_json   TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_images_sha ON images(sha256);

CREATE TABLE IF NOT EXISTS questions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    uid              TEXT NOT NULL UNIQUE,
    code             TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    subject          TEXT NOT NULL DEFAULT '数学',
    question_type    TEXT,
    stem             TEXT NOT NULL DEFAULT '',
    answer           TEXT,
    error_reason     TEXT,
    solution_fix     TEXT,
    solution_alt     TEXT,
    knowledge_points TEXT NOT NULL DEFAULT '[]',
    difficulty       INTEGER,
    tags             TEXT NOT NULL DEFAULT '[]',
    image_id         TEXT,
    crop_path        TEXT,
    bbox             TEXT,
    source           TEXT NOT NULL DEFAULT 'ai',
    ai_model         TEXT,
    ai_log_id        INTEGER,
    raw_ai           TEXT,
    notes            TEXT,
    starred          INTEGER NOT NULL DEFAULT 0,
    figure_svg       TEXT,
    figure_tikz      TEXT,
    figure_smiles    TEXT,
    figure_flow      TEXT,
    needs_figure     INTEGER NOT NULL DEFAULT 0,
    figure_crop      TEXT,
    figure_box       TEXT,
    deleted          INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_questions_created ON questions(deleted, created_at DESC);

CREATE TABLE IF NOT EXISTS papers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    uid          TEXT NOT NULL UNIQUE,
    title        TEXT NOT NULL,
    subtitle     TEXT,
    source       TEXT,
    subject      TEXT,
    student_name TEXT,
    exam_date    TEXT,
    options      TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    deleted      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS paper_items (
    paper_id    INTEGER NOT NULL,
    question_id INTEGER NOT NULL,
    position    INTEGER NOT NULL,
    PRIMARY KEY (paper_id, question_id),
    FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS ai_logs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     TEXT NOT NULL,
    config_id      INTEGER,
    model          TEXT,
    mode           TEXT,
    ok             INTEGER NOT NULL DEFAULT 0,
    parse_strategy TEXT,
    latency_ms     INTEGER,
    tokens_in      INTEGER,
    tokens_out     INTEGER,
    error          TEXT,
    raw_path       TEXT,
    question_uid   TEXT,
    image_id       TEXT
);
CREATE INDEX IF NOT EXISTS idx_logs_created ON ai_logs(created_at DESC);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- 识别队列：改存 SQLite，不再放 localStorage（浏览器 5MB 配额、
-- 多标签页互相覆盖、raw 截断到 1500 字都是它带来的）
CREATE TABLE IF NOT EXISTS queue_items (
    id         TEXT PRIMARY KEY,
    pos        INTEGER NOT NULL DEFAULT 0,
    payload    TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def new_uid() -> str:
    return uuid.uuid4().hex


# 题目编号用的字符集：去掉了容易看错的 0/O、1/I、L，剩下 32 个。
# 6 位 ≈ 10.7 亿种组合，一眼能念、能手打、能搜。
CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CODE_LEN = 6


def new_code() -> str:
    """随机题目编号（如 ``K7M2QA``）。"""
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))


def unique_code() -> str:
    """随机编号 + 查重。撞上了就再摇一次，最多试 20 次。"""
    for _ in range(20):
        code = new_code()
        if not query_one("SELECT 1 FROM questions WHERE code=?", (code,)):
            return code
    return new_code()  # 概率低到可以忽略；唯一索引会兜底


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=15.0)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        _local.conn = c
    return c


def init_db() -> None:
    with _lock:
        conn().executescript(SCHEMA)
        _migrate()
        conn().commit()


def _migrate() -> None:
    """轻量迁移：给旧库补上后加的列。"""
    for table, extra in (
        (
            "questions",
            (
                ("code", "ALTER TABLE questions ADD COLUMN code TEXT"),
                ("figure_svg", "ALTER TABLE questions ADD COLUMN figure_svg TEXT"),
                ("figure_tikz", "ALTER TABLE questions ADD COLUMN figure_tikz TEXT"),
                ("needs_figure", "ALTER TABLE questions ADD COLUMN needs_figure INTEGER NOT NULL DEFAULT 0"),
                ("figure_crop", "ALTER TABLE questions ADD COLUMN figure_crop TEXT"),
                ("figure_box", "ALTER TABLE questions ADD COLUMN figure_box TEXT"),
                (
                    "figure_smiles",
                    "ALTER TABLE questions ADD COLUMN figure_smiles TEXT",
                ),
                (
                    "figure_flow",
                    "ALTER TABLE questions ADD COLUMN figure_flow TEXT",
                ),
            ),
        ),
        (
            "papers",
            (
                ("source", "ALTER TABLE papers ADD COLUMN source TEXT"),
                ("subject", "ALTER TABLE papers ADD COLUMN subject TEXT"),
            ),
        ),
    ):
        cols = {row["name"] for row in conn().execute(f"PRAGMA table_info({table})").fetchall()}
        for name, ddl in extra:
            if name not in cols:
                conn().execute(ddl)
    _backfill_codes()
    _revive_dead_figures()


def _backfill_codes() -> None:
    """给没有编号的老题补一个随机编号，并建唯一索引。

    编号会印在试卷上，用户拿着它来问「这道题怎么回事」，所以要保证
    全局唯一、且短到能一眼念出来。
    """
    conn().execute("UPDATE questions SET code = NULL WHERE code = ''")
    rows = conn().execute(
        "SELECT id FROM questions WHERE code IS NULL"
    ).fetchall()
    for row in rows:
        conn().execute("UPDATE questions SET code = ? WHERE id = ?", (unique_code(), row["id"]))
    conn().execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_questions_code ON questions(code)"
    )
    conn().commit()


def _revive_dead_figures() -> None:
    """把历史"死图"重画一遍并写回。

    早期 ``sanitize_svg`` 会一刀切删掉 ``style`` 属性，RDKit 的键线于是变成
    ``<path d="M .. L .."/>`` —— stroke 默认 none、fill 填不了线段，整张结构式
    只剩 O、OH 几个字母。代码已修，但这种 SVG 一旦存进库会一直错下去，
    所以启动时扫一遍，用 figure_smiles / figure_flow 重画。
    """
    from .render.svg import is_dead_svg, revive_figure_svg

    rows = conn().execute(
        "SELECT id, figure_svg, figure_smiles, figure_flow FROM questions "
        "WHERE figure_svg IS NOT NULL AND figure_svg != '' "
        "  AND (figure_smiles IS NOT NULL OR figure_flow IS NOT NULL)"
    ).fetchall()
    for row in rows:
        svg = row["figure_svg"] or ""
        if not is_dead_svg(svg):
            continue
        fixed = revive_figure_svg(dict(row))
        if fixed and fixed != svg:
            conn().execute("UPDATE questions SET figure_svg = ? WHERE id = ?", (fixed, row["id"]))
    conn().commit()


def query(sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    cur = conn().execute(sql, tuple(params))
    try:
        return cur.fetchall()
    finally:
        cur.close()


def query_one(sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    with _lock:
        cur = conn().execute(sql, tuple(params))
        conn().commit()
        last = cur.lastrowid
        cur.close()
        return int(last or 0)


def execute_many(sql: str, seq: Iterable[Iterable[Any]]) -> None:
    with _lock:
        conn().executemany(sql, [tuple(p) for p in seq])
        conn().commit()


# ---------------- 题目 ----------------

QUESTION_FIELDS = (
    "subject",
    "question_type",
    "stem",
    "answer",
    "error_reason",
    "solution_fix",
    "solution_alt",
    "knowledge_points",
    "difficulty",
    "tags",
    "image_id",
    "crop_path",
    "figure_crop",
    "figure_box",
    "bbox",
    "source",
    "ai_model",
    "ai_log_id",
    "raw_ai",
    "notes",
    "starred",
    "figure_svg",
    "figure_tikz",
    "figure_smiles",
    "figure_flow",
    "needs_figure",
)


def question_to_dict(row: sqlite3.Row | dict) -> dict[str, Any]:
    d = dict(row)
    for key in ("knowledge_points", "tags"):
        raw = d.get(key)
        try:
            val = json.loads(raw) if raw else []
        except (TypeError, json.JSONDecodeError):
            val = []
        d[key] = val if isinstance(val, list) else []
    if "bbox" in d and d["bbox"]:
        try:
            d["bbox"] = json.loads(d["bbox"])
        except (TypeError, json.JSONDecodeError):
            pass
    # 化学结构式：库里存 JSON 文本，取出来直接给前端/导出用 dict
    if d.get("figure_smiles"):
        try:
            parsed_smiles = json.loads(d["figure_smiles"])
        except (TypeError, json.JSONDecodeError):
            parsed_smiles = None
        d["figure_smiles"] = parsed_smiles if isinstance(parsed_smiles, dict) else None
    else:
        d["figure_smiles"] = None
    # 工艺流程图同样是 JSON 文本
    if d.get("figure_flow"):
        try:
            parsed_flow = json.loads(d["figure_flow"])
        except (TypeError, json.JSONDecodeError):
            parsed_flow = None
        d["figure_flow"] = parsed_flow if isinstance(parsed_flow, dict) else None
    else:
        d["figure_flow"] = None
    # figure_crop 存的是路径；导出层用 *_path 命名，前端用 *_url
    d["figure_crop_path"] = d.get("figure_crop") or None
    if d.get("figure_box"):
        try:
            d["figure_box"] = json.loads(d["figure_box"])
        except (TypeError, json.JSONDecodeError):
            d["figure_box"] = None
    else:
        d["figure_box"] = None
    d["starred"] = bool(d.get("starred"))
    d["needs_figure"] = bool(d.get("needs_figure"))
    d.pop("deleted", None)
    d.pop("raw_ai", None) if d.get("raw_ai") is None else None
    # 历史"死图"（键线 path 的 style 被早期清洗剥掉）在这里顺手救活：
    # 不只导出，编辑器预览也要看到能用的图。健康图只是跑两个正则，零成本。
    svg = d.get("figure_svg")
    if svg:
        from .render.svg import is_dead_svg, revive_figure_svg

        if is_dead_svg(svg):
            d["figure_svg"] = revive_figure_svg(d)
    # 编号：老库补过、新库插入时就有；万一还是空的，给个占位避免前端 undefined
    if not d.get("code"):
        d["code"] = ""
    return d


def insert_question(data: dict[str, Any]) -> dict[str, Any]:
    ts = now()
    uid = new_uid()
    payload = {k: data.get(k) for k in QUESTION_FIELDS}
    payload["subject"] = payload["subject"] or "数学"
    payload["stem"] = payload["stem"] or ""
    payload["source"] = payload["source"] or "manual"
    for key in ("knowledge_points", "tags"):
        val = payload.get(key) or []
        payload[key] = json.dumps(val, ensure_ascii=False)
    if isinstance(payload.get("bbox"), (list, dict)):
        payload["bbox"] = json.dumps(payload["bbox"], ensure_ascii=False)
    # figure_smiles 在代码里是 {名称: SMILES}，落库要转成 JSON 文本
    if isinstance(payload.get("figure_smiles"), (list, dict)):
        payload["figure_smiles"] = json.dumps(payload["figure_smiles"], ensure_ascii=False)
    if isinstance(payload.get("figure_flow"), (list, dict)):
        payload["figure_flow"] = json.dumps(payload["figure_flow"], ensure_ascii=False)
    payload["starred"] = 1 if payload.get("starred") else 0
    payload["needs_figure"] = 1 if payload.get("needs_figure") else 0
    cols = ["uid", "code", "created_at", "updated_at", *QUESTION_FIELDS]
    vals = [uid, unique_code(), ts, ts, *[payload[k] for k in QUESTION_FIELDS]]
    qid = execute(
        f"INSERT INTO questions ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
        vals,
    )
    row = query_one("SELECT * FROM questions WHERE id=?", (qid,))
    assert row is not None
    return question_to_dict(row)


def update_question(qid: int, patch: dict[str, Any]) -> dict[str, Any] | None:
    fields: list[str] = []
    vals: list[Any] = []
    for key in QUESTION_FIELDS:
        if key not in patch:
            continue
        val = patch[key]
        if key in ("knowledge_points", "tags"):
            val = json.dumps(val or [], ensure_ascii=False)
        elif key == "bbox" and isinstance(val, (list, dict)):
            val = json.dumps(val, ensure_ascii=False)
        elif key == "figure_smiles" and isinstance(val, (list, dict)):
            val = json.dumps(val, ensure_ascii=False)
        elif key == "figure_flow" and isinstance(val, (list, dict)):
            val = json.dumps(val, ensure_ascii=False)
        elif key == "starred":
            val = 1 if val else 0
        elif key == "needs_figure":
            val = 1 if val else 0
        elif key == "subject" and not str(val or "").strip():
            val = "数学"  # 列是 NOT NULL，空值会把整次保存打回去
        fields.append(f"{key}=?")
        vals.append(val)
    if not fields:
        return get_question_by_id(qid)
    fields.append("updated_at=?")
    vals.append(now())
    vals.append(qid)
    execute(f"UPDATE questions SET {','.join(fields)} WHERE id=? AND deleted=0", vals)
    return get_question_by_id(qid)


def get_question_by_id(qid: int) -> dict[str, Any] | None:
    row = query_one("SELECT * FROM questions WHERE id=? AND deleted=0", (qid,))
    return question_to_dict(row) if row else None


def get_question_by_uid(uid: str) -> dict[str, Any] | None:
    row = query_one("SELECT * FROM questions WHERE uid=? AND deleted=0", (uid,))
    return question_to_dict(row) if row else None


def list_questions(
    keyword: str = "",
    subject: str = "",
    starred_only: bool = False,
    limit: int = 200,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    where = ["deleted=0"]
    params: list[Any] = []
    if keyword:
        like = f"%{keyword}%"
        where.append(
            "(stem LIKE ? OR answer LIKE ? OR error_reason LIKE ? OR solution_fix LIKE ? "
            "OR solution_alt LIKE ? OR tags LIKE ? OR knowledge_points LIKE ? OR code LIKE ?)"
        )
        params.extend([like] * 8)
    if subject:
        where.append("subject=?")
        params.append(subject)
    if starred_only:
        where.append("starred=1")
    cond = " AND ".join(where)
    total_row = query_one(f"SELECT COUNT(*) AS n FROM questions WHERE {cond}", params)
    total = int(total_row["n"]) if total_row else 0
    rows = query(
        f"SELECT * FROM questions WHERE {cond} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
        [*params, int(limit), int(offset)],
    )
    return [question_to_dict(r) for r in rows], total


# ---------------- 图片 ----------------

def insert_image(data: dict[str, Any]) -> None:
    execute(
        "INSERT INTO images (id,path,orig_name,width,height,sha256,page_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            data["id"],
            data["path"],
            data.get("orig_name"),
            int(data.get("width") or 0),
            int(data.get("height") or 0),
            data.get("sha256"),
            json.dumps(data.get("page"), ensure_ascii=False) if data.get("page") else None,
            now(),
        ),
    )


def get_image(image_id: str) -> dict[str, Any] | None:
    row = query_one("SELECT * FROM images WHERE id=?", (image_id,))
    if not row:
        return None
    d = dict(row)
    if d.get("page_json"):
        try:
            d["page"] = json.loads(d["page_json"])
        except json.JSONDecodeError:
            d["page"] = None
    return d


def image_by_sha(sha256: str) -> dict[str, Any] | None:
    """按内容哈希找已有图片，用于重复上传去重（同一张照片反复拖入很常见）。"""
    if not sha256:
        return None
    row = query_one(
        "SELECT * FROM images WHERE sha256=? ORDER BY created_at DESC LIMIT 1", (sha256,)
    )
    return dict(row) if row else None


# ---------------- 试卷 ----------------

def insert_paper(data: dict[str, Any], question_ids: list[int]) -> dict[str, Any]:
    ts = now()
    uid = new_uid()
    pid = execute(
        "INSERT INTO papers (uid,title,subtitle,source,subject,student_name,exam_date,options,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            uid,
            data.get("title") or "错题练习",
            data.get("subtitle"),
            data.get("source"),
            data.get("subject"),
            data.get("student_name"),
            data.get("exam_date"),
            json.dumps(data.get("options") or {}, ensure_ascii=False),
            ts,
            ts,
        ),
    )
    execute_many(
        "INSERT OR IGNORE INTO paper_items (paper_id,question_id,position) VALUES (?,?,?)",
        [(pid, int(qid), idx) for idx, qid in enumerate(question_ids)],
    )
    paper = get_paper(pid)
    assert paper is not None
    return paper


PAPER_FIELDS = ("title", "subtitle", "source", "subject", "student_name", "exam_date", "options")


def update_paper(
    pid: int,
    data: dict[str, Any],
    question_ids: list[int] | None = None,
) -> dict[str, Any] | None:
    """原地更新试卷；question_ids 不为 None 时整体替换题目与顺序。"""
    sets: list[str] = []
    params: list[Any] = []
    for key in PAPER_FIELDS:
        if key not in data:
            continue
        value = data[key]
        if key == "options":
            value = json.dumps(value or {}, ensure_ascii=False)
        sets.append(f"{key}=?")
        params.append(value)
    sets.append("updated_at=?")
    params.extend([now(), pid])
    execute(f"UPDATE papers SET {', '.join(sets)} WHERE id=? AND deleted=0", params)
    if question_ids is not None:
        execute("DELETE FROM paper_items WHERE paper_id=?", (pid,))
        execute_many(
            "INSERT OR IGNORE INTO paper_items (paper_id,question_id,position) VALUES (?,?,?)",
            [(pid, int(qid), idx) for idx, qid in enumerate(question_ids)],
        )
    return get_paper(pid)


def get_paper(pid: int) -> dict[str, Any] | None:
    row = query_one("SELECT * FROM papers WHERE id=? AND deleted=0", (pid,))
    if not row:
        return None
    d = dict(row)
    try:
        d["options"] = json.loads(d.get("options") or "{}")
    except json.JSONDecodeError:
        d["options"] = {}
    return d


def list_papers() -> list[dict[str, Any]]:
    rows = query("SELECT * FROM papers WHERE deleted=0 ORDER BY created_at DESC, id DESC")
    out = []
    for row in rows:
        d = dict(row)
        try:
            d["options"] = json.loads(d.get("options") or "{}")
        except json.JSONDecodeError:
            d["options"] = {}
        cnt = query_one("SELECT COUNT(*) AS n FROM paper_items WHERE paper_id=?", (d["id"],))
        d["question_count"] = int(cnt["n"]) if cnt else 0
        out.append(d)
    return out


def paper_questions(pid: int) -> list[dict[str, Any]]:
    rows = query(
        "SELECT q.*, i.position FROM paper_items i JOIN questions q ON q.id=i.question_id "
        "WHERE i.paper_id=? AND q.deleted=0 ORDER BY i.position",
        (pid,),
    )
    return [question_to_dict(r) for r in rows]


def delete_paper(pid: int) -> None:
    execute("UPDATE papers SET deleted=1, updated_at=? WHERE id=?", (now(), pid))


# ---------------- AI 日志 ----------------

def log_ai(**kw: Any) -> int:
    return execute(
        "INSERT INTO ai_logs (created_at,config_id,model,mode,ok,parse_strategy,latency_ms,"
        "tokens_in,tokens_out,error,raw_path,question_uid,image_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            now(),
            kw.get("config_id"),
            kw.get("model"),
            kw.get("mode"),
            1 if kw.get("ok") else 0,
            kw.get("parse_strategy"),
            kw.get("latency_ms"),
            kw.get("tokens_in"),
            kw.get("tokens_out"),
            (kw.get("error") or "")[:2000] or None,
            kw.get("raw_path"),
            kw.get("question_uid"),
            kw.get("image_id"),
        ),
    )


def list_logs(limit: int = 50) -> list[dict[str, Any]]:
    return [dict(r) for r in query("SELECT * FROM ai_logs ORDER BY id DESC LIMIT ?", (int(limit),))]


def stats() -> dict[str, int]:
    def count(sql: str) -> int:
        row = query_one(sql)
        return int(row[0]) if row else 0

    return {
        "questions": count("SELECT COUNT(*) FROM questions WHERE deleted=0"),
        "starred": count("SELECT COUNT(*) FROM questions WHERE deleted=0 AND starred=1"),
        "papers": count("SELECT COUNT(*) FROM papers WHERE deleted=0"),
        "images": count("SELECT COUNT(*) FROM images"),
    }


# ---------------- 识别队列（替代 localStorage） ----------------

def queue_list() -> list[dict[str, Any]]:
    out = []
    for row in query("SELECT id, payload, updated_at FROM queue_items ORDER BY pos ASC, rowid ASC"):
        try:
            payload = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            payload.setdefault("id", row["id"])
            payload["updated_at"] = row["updated_at"]
            out.append(payload)
    return out


def queue_replace(items: list[dict[str, Any]]) -> None:
    """整表替换成当前队列（前端本就是全量快照写回，服务端跟着全量存）。

    存 SQLite 而不是 localStorage：浏览器 5MB 配额会截断 AI 原始响应、
    多标签页会互相覆盖，而且换台机器就没了。
    """
    with _lock:
        conn().execute("DELETE FROM queue_items")
        for pos, item in enumerate(items):
            qid = str(item.get("id") or new_uid())[:40]
            conn().execute(
                "INSERT OR REPLACE INTO queue_items (id, pos, payload, updated_at) VALUES (?, ?, ?, ?)",
                (qid, pos, json.dumps(item, ensure_ascii=False), now()),
            )
        conn().commit()
