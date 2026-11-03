"""把本机安装的「错题录入」skill 同步进仓库，保证随仓库分发的那份不过期。

源：``~/.workbuddy/skills/错题录入/``（WorkBuddy 实际加载的地方）
目标：``<项目>/skills/错题录入/``（进 git，给仓库使用者）

改动 skill 后跑一次即可：

    .venv/Scripts/python.exe tools/sync_skill.py          # 同步
    .venv/Scripts/python.exe tools/sync_skill.py --check   # 只检查是否一致
"""
from __future__ import annotations

import filecmp
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = Path.home() / ".workbuddy" / "skills" / "错题录入"
DST = ROOT / "skills" / "错题录入"
# .root / .local 之类是「本机装在哪」的配置，不属于公开仓库内容
SKIP = {"__pycache__", ".pytest_cache"}


def _skip(path: Path) -> bool:
    return any(part in SKIP or part.startswith(".") for part in path.parts)


def files_under(base: Path) -> list[Path]:
    if not base.is_dir():
        return []
    return sorted(p for p in base.rglob("*") if p.is_file() and not _skip(p.relative_to(base)))


def main() -> int:
    if not SRC.is_dir():
        print(f"找不到已安装的 skill：{SRC}")
        return 2
    check_only = "--check" in sys.argv

    src_files = files_under(SRC)
    changed = 0
    for src in src_files:
        rel = src.relative_to(SRC)
        dst = DST / rel
        same = dst.is_file() and filecmp.cmp(src, dst, shallow=False)
        if same:
            continue
        changed += 1
        print(f"  {'~' if dst.exists() else '+'} {rel}")
        if not check_only:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    # 仓库里多出来的文件（skill 删过东西）也要清掉，否则会误导使用者
    stale = [dst for dst in files_under(DST) if not (SRC / dst.relative_to(DST)).is_file()]
    for dst in stale:
        changed += 1
        print(f"  - {dst.relative_to(DST)}")
        if not check_only:
            dst.unlink()

    if not changed:
        print(f"已是最新：{len(src_files)} 个文件与仓库一致")
        return 0
    if check_only:
        print(f"有 {changed} 处差异（去掉 --check 才会写入）")
        return 1
    print(f"已同步 {len(src_files)} 个文件 → {DST}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
