"""检查仓库里所有文本文件的编码，确保提交上去不会出现乱码。

乱码的两种真实来源，这个脚本都盯：

1. **文件本身不是 UTF-8**（GBK / cp1252 存的），GitHub 一律按 UTF-8 渲染，
   中文就变成「锛?」「æ±‚å¯¼」这类。Windows 上双击记事本另存、或 PowerShell
   `Set-Content` 不带 `-Encoding` 都会踩到。
2. **启动类脚本里混进非 ASCII**：`.bat` / `.cmd` / `.vbs` / `.ps1` 在中文
   Windows 下按 ANSI 码页解析，一个中文字节就能让 cmd 把整行切错位
   （`'e' 不是内部或外部命令`）。这些文件必须**纯 ASCII + CRLF**。

用法：
    python tools/check_encoding.py            # 检查当前目录
    python tools/check_encoding.py --root .   # 指定根目录
    python tools/check_encoding.py --fix      # 把能确定的 GBK 文件转回 UTF-8
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

TEXT_EXT = {
    ".py", ".js", ".cjs", ".mjs", ".css", ".html", ".htm", ".md", ".markdown",
    ".json", ".txt", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".bat", ".cmd",
    ".ps1", ".vbs", ".sh", ".sql",
}
BINARY_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".ico", ".ttf", ".woff",
    ".woff2", ".pdf", ".zip", ".gz", ".mp4", ".pyc", ".db", ".sqlite", ".exe",
}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "data", "logs", "dist", "build"}

# 必须纯 ASCII 的启动脚本：cmd/wscript 按 ANSI 码页解析，中文会让整行错位
ASCII_ONLY_EXT = {".bat", ".cmd", ".vbs", ".ps1"}

# 典型的「UTF-8 被当 GBK 读」痕迹，出现在正文里基本可以确认是乱码
MOJIBAKE_MARKERS = ("锛", "鈥", "銆", "鐨", "璇存", "闇€", "鍒犻", "æ±‚", "å¯¼", "ç”")
# 文档里为了讲解乱码而写的示例行要放过（同一个人不会在自己身上报错）
MOJIBAKE_CONTEXT = ("乱码", "示例", "例如", "mojibake", "cp1252", "GBK")
# 这个文件自己就带着上面那串特征字，不参与检查
SELF_NAME = "check_encoding.py"


def iter_files(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in BINARY_EXT:
            continue
        if path.suffix.lower() not in TEXT_EXT:
            continue
        yield path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--fix", action="store_true", help="尝试把 GBK 文件转成 UTF-8")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    problems: list[str] = []
    fixed: list[str] = []
    checked = 0

    for path in iter_files(root):
        rel = path.relative_to(root).as_posix()
        raw = path.read_bytes()
        checked += 1

        # UTF-8 BOM 不是错，但会污染首行（Python 3 能容忍，JS 不能）
        has_bom = raw.startswith(b"\xef\xbb\xbf")
        body = raw[3:] if has_bom else raw

        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            if args.fix and path.suffix.lower() != ".bat":
                try:
                    text = body.decode("gbk")
                except UnicodeDecodeError:
                    problems.append(f"{rel}: 既不是 UTF-8 也不是 GBK（{exc.reason} @ {exc.start}）")
                    continue
                path.write_bytes(text.encode("utf-8"))
                fixed.append(rel)
                print(f"  转换为 UTF-8：{rel}")
                continue
            problems.append(f"{rel}: 不是 UTF-8（{exc.reason} @ 字节 {exc.start}）")
            continue

        if has_bom and path.suffix.lower() in {".js", ".cjs", ".mjs", ".css", ".html"}:
            problems.append(f"{rel}: 带 UTF-8 BOM，浏览器会被当成内容的一部分")

        if path.suffix.lower() in ASCII_ONLY_EXT:
            bad = [(i, b) for i, b in enumerate(body) if b >= 0x80]
            if bad:
                problems.append(
                    f"{rel}: 启动脚本里有非 ASCII 字节（首个在偏移 {bad[0][0]}），"
                    "中文 Windows 下会让 cmd 整行错位"
                )
            if b"\n" in body.replace(b"\r\n", b""):
                problems.append(f"{rel}: 只写了 LF 换行，启动脚本必须 CRLF")

        hits: list[str] = []
        if path.name != SELF_NAME:
            for line in text.splitlines():
                if any(ctx in line for ctx in MOJIBAKE_CONTEXT):
                    continue
                found = [m for m in MOJIBAKE_MARKERS if m in line]
                if len(found) >= 2:
                    hits.extend(found)
                    break
        if hits:
            problems.append(f"{rel}: 疑似乱码字样 {','.join(dict.fromkeys(hits))[:40]}")

    print(f"\n检查 {checked} 个文本文件，问题 {len(problems)} 个，已转换 {len(fixed)} 个")
    for p in problems:
        print("  ✗ " + p)
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
