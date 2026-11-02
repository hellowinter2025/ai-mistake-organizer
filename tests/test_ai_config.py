"""AI 服务配置的增删改接口。

要点：
- ``api_key`` 不回显，只给 ``has_key`` / ``key_hint``；
- 修改时**不传 key** 必须保留原来的 key（前端拿不到明文，不能逼用户重填）；
- 删掉当前配置后要自动切到下一条，不能留下悬空 active_config_id。

测试直接改进程内单例（不碰 data/config.json 的真实内容），每个用例前后快照还原。
"""
from __future__ import annotations

import copy

import pytest


@pytest.fixture()
def restore_settings():
    from app.config import settings

    snapshot = copy.deepcopy(settings._data)
    yield settings
    settings._data = snapshot
    settings.save()


def test_create_config_masks_key(client, restore_settings):
    res = client.post(
        "/api/settings/configs",
        json={
            "name": "测试接口",
            "base_url": "https://api.example.com/v1/",
            "api_key": "sk-abcdefghijklmnop",
            "models": ["m-a", "m-b"],
        },
    )
    assert res.status_code == 200, res.text
    cfg = res.json()["config"]
    assert cfg["name"] == "测试接口"
    assert cfg["base_url"] == "https://api.example.com/v1"  # 去掉尾斜杠
    assert cfg["models"] == ["m-a", "m-b"]
    assert cfg["has_key"] is True
    assert cfg["key_hint"] == "sk-abc…mnop"
    assert "api_key" not in cfg
    assert "sk-abcdefghijklmnop" not in res.text


def test_create_requires_name_and_base_url(client, restore_settings):
    assert client.post("/api/settings/configs", json={"name": "", "base_url": "https://a.b"}).status_code == 400
    assert client.post("/api/settings/configs", json={"name": "x", "base_url": ""}).status_code == 400
    bad = client.post("/api/settings/configs", json={"name": "x", "base_url": "ftp://a.b"})
    assert bad.status_code == 400


def test_update_keeps_key_when_omitted(client, restore_settings):
    created = client.post(
        "/api/settings/configs",
        json={"name": "A", "base_url": "https://a.example.com/v1", "api_key": "sk-first-0000001111"},
    ).json()["config"]
    res = client.patch(
        f"/api/settings/configs/{created['id']}",
        json={"name": "A2", "base_url": "https://a2.example.com/v1"},  # 不带 api_key
    )
    assert res.status_code == 200, res.text
    cfg = res.json()["config"]
    assert cfg["name"] == "A2"
    assert cfg["base_url"] == "https://a2.example.com/v1"
    assert cfg["has_key"] is True  # key 没被清掉


def test_update_replaces_key_and_clears_with_null(client, restore_settings):
    created = client.post(
        "/api/settings/configs",
        json={"name": "B", "base_url": "https://b.example.com/v1", "api_key": "sk-old-0000000000"},
    ).json()["config"]

    replaced = client.patch(
        f"/api/settings/configs/{created['id']}",
        json={"name": "B", "base_url": "https://b.example.com/v1", "api_key": "sk-new-1111111111"},
    ).json()["config"]
    assert replaced["key_hint"] == "sk-new…1111"

    cleared = client.patch(
        f"/api/settings/configs/{created['id']}",
        json={"name": "B", "base_url": "https://b.example.com/v1", "api_key": None},
    ).json()["config"]
    assert cleared["has_key"] is False
    assert cleared["key_hint"] == ""


def test_update_missing_config_404(client, restore_settings):
    res = client.patch("/api/settings/configs/999999", json={"name": "x", "base_url": "https://x.y"})
    assert res.status_code == 404


def test_delete_config_moves_active(client, restore_settings):
    first = client.post(
        "/api/settings/configs",
        json={"name": "first", "base_url": "https://1.example.com/v1", "models": ["m1"]},
    ).json()["config"]
    second = client.post(
        "/api/settings/configs",
        json={"name": "second", "base_url": "https://2.example.com/v1", "models": ["m2"]},
    ).json()["config"]
    assert first["id"] != second["id"]

    client.patch("/api/settings", json={"ai": {"active_config_id": second["id"], "active_model": "m2"}})
    res = client.delete(f"/api/settings/configs/{second['id']}")
    assert res.status_code == 200, res.text
    remaining = [c["id"] for c in res.json()["settings"]["configs"]]
    assert second["id"] not in remaining
    assert first["id"] in remaining
    state = client.get("/api/state").json()
    assert state["settings"]["ai"]["active_config_id"] == first["id"]

    assert client.delete(f"/api/settings/configs/{second['id']}").status_code == 404


def test_models_accepts_comma_string(client, restore_settings):
    cfg = client.post(
        "/api/settings/configs",
        json={"name": "C", "base_url": "https://c.example.com/v1", "models": "m1, m2 m3，m4"},
    ).json()["config"]
    assert cfg["models"] == ["m1", "m2", "m3", "m4"]
