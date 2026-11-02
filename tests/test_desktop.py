"""导出落盘与「打开文件夹」的判断逻辑。

这些用例一律用桩替换 subprocess.Popen——测试绝不能真的弹资源管理器窗口。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app import desktop


@pytest.fixture()
def no_explorer(monkeypatch):
    """记录 explorer 调用但不真的启动。"""
    calls: list[list[str]] = []

    class FakePopen:
        def __init__(self, args, **kwargs):  # noqa: ANN001 - 模拟 subprocess.Popen
            calls.append(list(args))

    monkeypatch.setattr(desktop.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(desktop, "_recently_opened", {})
    return calls


def test_downloads_dir_exists():
    target = desktop.downloads_dir()
    assert target.is_dir()


def test_publish_never_overwrites(tmp_path, monkeypatch):
    """同名文件不覆盖，自动加 (2)(3) —— 重复导出往往是留不同版本。"""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    monkeypatch.setenv("MM_DOWNLOAD_DIR", str(out_dir))

    src = tmp_path / "src.pdf"
    src.write_bytes(b"first")
    dest = desktop.publish(src, "试卷 A.pdf")
    assert dest == out_dir / "试卷 A.pdf"
    assert dest.read_bytes() == b"first"

    src.write_bytes(b"second")
    second = desktop.publish(src, "试卷 A.pdf")
    assert second == out_dir / "试卷 A (2).pdf"
    assert second.read_bytes() == b"second"

    src.write_bytes(b"third")
    third = desktop.publish(src, "试卷 A.pdf")
    assert third == out_dir / "试卷 A (3).pdf"
    # 最早那份必须还在，内容没被改
    assert dest.read_bytes() == b"first"


def test_publish_reports_locked_file(tmp_path, monkeypatch):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    monkeypatch.setenv("MM_DOWNLOAD_DIR", str(out_dir))
    src = tmp_path / "src.pdf"
    src.write_bytes(b"x")

    def boom(*args, **kwargs):  # noqa: ANN001
        raise PermissionError(13, "locked")

    monkeypatch.setattr(desktop.shutil, "copyfile", boom)
    with pytest.raises(OSError) as excinfo:
        desktop.publish(src, "a.pdf")
    assert "请先关闭" in str(excinfo.value)


def test_title_matching_is_strict():
    folder = Path("C:/Users/example/Downloads")
    ok = desktop._title_shows_folder("C:\\Users\\example\\Downloads - 文件资源管理器", folder)
    assert ok is True
    assert desktop._title_shows_folder("C:\\Users\\example\\Downloads - File Explorer", folder) is True
    # 别把相似目录当成本目录
    assert desktop._title_shows_folder("C:\\Users\\example\\Downloads2 - 文件资源管理器", folder) is False
    assert desktop._title_shows_folder("C:\\Users\\example - 文件资源管理器", folder) is False


def test_reveal_folder_opens_once(tmp_path, no_explorer):
    assert desktop.reveal_folder(tmp_path) == "opened"
    assert no_explorer == [["explorer.exe", str(tmp_path)]]
    # 连点两次不再开第二个窗口
    assert desktop.reveal_folder(tmp_path) == "already"
    assert len(no_explorer) == 1


def test_reveal_folder_skips_when_already_open(tmp_path, no_explorer):
    open_dir = tmp_path / "已打开"
    open_dir.mkdir()
    desktop._recently_opened.clear()
    # 直接伪造成「窗口还开着」
    assert desktop.reveal_folder(open_dir) == "opened"
    desktop._recently_opened.clear()  # 清掉抑制，靠窗口枚举判断
    desktop_popen_calls = len(no_explorer)

    def fake_open(folder: Path) -> bool:
        return True

    original = desktop.folder_is_open
    desktop.folder_is_open = fake_open
    try:
        assert desktop.reveal_folder(open_dir) == "already"
    finally:
        desktop.folder_is_open = original
    assert len(no_explorer) == desktop_popen_calls


def test_export_endpoint_writes_to_download_dir(client, tmp_path, monkeypatch):
    """POST /export 直接落盘，返回可读路径。"""
    out_dir = tmp_path / "下载"
    out_dir.mkdir()
    monkeypatch.setenv("MM_DOWNLOAD_DIR", str(out_dir))
    monkeypatch.setattr(desktop, "reveal_folder", lambda folder: "already")

    ids = []
    for i in range(2):
        res = client.post("/api/questions", json={"stem": f"落盘测试{i}", "answer": "1"})
        ids.append(res.json()["question"]["uid"])
    paper = client.post("/api/papers", json={"title": "落盘测试卷", "question_uids": ids})
    uid = paper.json()["paper"]["uid"]

    res = client.post(f"/api/papers/{uid}/export?fmt=docx")
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["ok"] is True
    landed = Path(data["path"])
    assert landed.parent == out_dir
    assert landed.name == "落盘测试卷.docx"
    assert landed.read_bytes()[:2] == b"PK"

    # 再导一次不覆盖，自动加序号
    again = client.post(f"/api/papers/{uid}/export?fmt=docx")
    assert again.status_code == 200
    assert again.json()["name"] == "落盘测试卷 (2).docx"
    assert sorted(p.name for p in out_dir.iterdir()) == ["落盘测试卷 (2).docx", "落盘测试卷.docx"]

    assert client.delete(f"/api/papers/{uid}").status_code == 200


def test_cutout_layout_appends_suffix_to_filename(client, tmp_path, monkeypatch):
    """排版方式选「裁切粘贴」时，导出文件名默认带 -裁切，免得和整卷版混在一起。"""
    out_dir = tmp_path / "下载"
    out_dir.mkdir()
    monkeypatch.setenv("MM_DOWNLOAD_DIR", str(out_dir))
    monkeypatch.setattr(desktop, "reveal_folder", lambda folder: "already")

    uid_q = client.post("/api/questions", json={"stem": "裁切版式测试"}).json()["question"]["uid"]
    paper = client.post(
        "/api/papers",
        json={"title": "版式测试卷", "question_uids": [uid_q], "options": {"layout": "cutout"}},
    ).json()["paper"]

    data = client.post(f"/api/papers/{paper['uid']}/export?fmt=docx").json()
    assert data["name"] == "版式测试卷-裁切.docx"

    # 换回整卷版式，导出的就是不带后缀的另一份
    client.put(
        f"/api/papers/{paper['uid']}",
        json={"title": "版式测试卷", "question_uids": [uid_q], "options": {"layout": "standard"}},
    )
    plain = client.post(f"/api/papers/{paper['uid']}/export?fmt=docx").json()
    assert plain["name"] == "版式测试卷.docx"

    assert client.delete(f"/api/papers/{paper['uid']}").status_code == 200
