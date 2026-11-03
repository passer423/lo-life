"""HTTP 接口测试。

几条刻意约束：

* **不建知识库索引**：既不需要加载 BGE，又把"索引还没建"这条路径测掉（503 + 可执行提示）。
* **不断言依赖"今天"的数字**：新鲜度随日期变，写死"7 天内必须有 N 条"过两周就红。
* **认证与授权分开测**：401（没登录）和 400/404（业务校验）是两件事。
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

PASSWORD = "password123"


def bearer(token: str) -> dict:
    return {"Authorization": "Bearer " + token}


def register(client, username: str = "tester") -> str:
    response = client.post("/api/auth/register", json={"username": username, "password": PASSWORD})
    assert response.status_code == 201, response.text
    return response.json()["token"]


@pytest.fixture()
def token(client) -> str:
    return register(client)


# --------------------------------------------------------------------- 基础


def test_settings_are_isolated_from_real_data():
    """测试绝不能碰真实 data/。

    具体目录由 conftest 决定——现在是 `tests/.data/pid<pid>`，让**并行跑的两套测试
    各自有一份库**（共用一个目录时，两边的清理会把对方的库删掉，表现成随机的
    OperationalError）。所以这里断言"在 tests/.data 之下"，而不是写死某个路径：
    写死就等于把这条测试绑在 conftest 的实现细节上。
    """
    from app.config import settings

    probe = settings.data_dir.resolve()
    assert "tests" in probe.parts
    assert ".data" in probe.parts
    assert settings.llm_enabled is False


def test_health(client):
    payload = client.get("/api/health").json()
    assert payload["ok"] is True
    assert payload["users"] == 0
    assert payload["orphan_shops"] == 0
    assert payload["newarrivals"] > 0
    assert payload["top_k"] == 1
    assert payload["kb"]["ready"] is False


def test_kb_status_explains_missing_index(client):
    payload = client.get("/api/kb/status").json()
    assert payload["ready"] is False
    assert "build_kb_index" in payload["error"]


def test_kb_ask_without_index_returns_503(client):
    assert client.get("/api/kb/ask", params={"q": "JSK 是什么"}).status_code == 503


def test_kb_ask_rejects_empty(client):
    assert client.get("/api/kb/ask", params={"q": "   "}).status_code == 400
    assert client.get("/api/kb/ask").status_code == 422


# --------------------------------------------------------------------- 认证


def test_register_and_me(client):
    token = register(client)
    assert client.get("/api/auth/me").json()["user"] is None   # 未登录返回 200 + null
    assert client.get("/api/auth/me", headers=bearer(token)).json()["user"]["username"] == "tester"


def test_register_duplicate_is_409(client):
    register(client)
    again = client.post("/api/auth/register", json={"username": "tester", "password": PASSWORD})
    assert again.status_code == 409


def test_register_weak_password_is_400(client):
    response = client.post("/api/auth/register", json={"username": "tester", "password": "short"})
    assert response.status_code == 400
    assert "至少 8 位" in response.json()["detail"]


def test_login_success_and_failure(client):
    register(client)
    ok = client.post("/api/auth/login", json={"username": "tester", "password": PASSWORD})
    assert ok.status_code == 200 and ok.json()["token"]
    bad = client.post("/api/auth/login", json={"username": "tester", "password": "wrongpass1"})
    assert bad.status_code == 401


def test_logout_invalidates_token(client):
    token = register(client)
    assert client.post("/api/auth/logout", headers=bearer(token)).status_code == 200
    assert client.get("/api/auth/me", headers=bearer(token)).json()["user"] is None


def test_logout_without_token_is_401(client):
    assert client.post("/api/auth/logout").status_code == 401


def test_bad_token_is_treated_as_anonymous(client):
    assert client.get("/api/auth/me", headers=bearer("garbage")).json()["user"] is None


def test_delete_account_removes_data(client):
    """注销必须是真删：同名账号要能重新注册，偏好一起消失。"""
    token = register(client)
    client.put("/api/preferences", json={"styles": ["甜系"]}, headers=bearer(token))

    assert client.delete("/api/auth/me", headers=bearer(token)).status_code == 200
    assert client.get("/api/auth/me", headers=bearer(token)).json()["user"] is None
    again = client.post("/api/auth/register", json={"username": "tester", "password": PASSWORD})
    assert again.status_code == 201, "同名账号应该能重新注册（说明是真删而不是软删除）"


# --------------------------------------------------------------------- 偏好


def test_preference_options_expose_axis_metadata(client):
    """前端靠这份元数据渲染标签组——加新轴时前端不该改代码。"""
    payload = client.get("/api/preferences/options").json()
    keys = [axis["key"] for axis in payload["axes"]]
    assert keys == ["shops", "styles", "keywords", "categories"]
    for axis in payload["axes"]:
        assert axis["label"] and axis["hint"]
        assert axis["bonus"] > 0
        assert "allow_custom" in axis
    # 只有自定义标签允许自由输入
    flags = {axis["key"]: axis["allow_custom"] for axis in payload["axes"]}
    assert flags["keywords"] is True
    assert not any(flags[key] for key in ("shops", "styles", "categories"))
    assert payload["options"]["shops"], "content 里应该有店家"
    assert payload["options"]["categories"], "content 里应该有款式"
    assert payload["options"]["styles"]
    assert payload["options"]["keywords"] == []      # 自定义轴没有预置选项


def test_preferences_anonymous(client):
    payload = client.get("/api/preferences").json()
    assert payload["logged_in"] is False
    assert payload["preferences"] == {
        "shops": [], "styles": [], "keywords": [], "categories": [],
    }
    assert payload["described"] == []


def test_preferences_write_requires_login(client):
    response = client.put("/api/preferences", json={"styles": ["甜系"]})
    assert response.status_code == 401


def test_preferences_roundtrip_all_axes(client, token):
    saved = client.put("/api/preferences", json={
        "shops": ["baby"], "styles": ["甜系", "古典"], "categories": ["JSK"],
    }, headers=bearer(token))
    assert saved.status_code == 200
    body = saved.json()
    assert body["preferences"]["shops"] == ["baby"]
    assert body["preferences"]["categories"] == ["JSK"]
    assert {row["key"] for row in body["described"]} == {"shops", "styles", "categories"}

    fetched = client.get("/api/preferences", headers=bearer(token)).json()
    assert fetched["preferences"]["styles"] == ["甜系", "古典"]


def test_preferences_partial_update_keeps_other_axes(client, token):
    """只提交 shops 时，已有的 styles 必须保留——这是最容易让人暴怒的一类 bug。"""
    client.put("/api/preferences", json={"styles": ["甜系"]}, headers=bearer(token))
    client.put("/api/preferences", json={"shops": ["baby"]}, headers=bearer(token))

    prefs = client.get("/api/preferences", headers=bearer(token)).json()["preferences"]
    assert prefs["shops"] == ["baby"]
    assert prefs["styles"] == ["甜系"]


def test_preferences_can_be_cleared(client, token):
    client.put("/api/preferences", json={"styles": ["甜系"]}, headers=bearer(token))
    client.put("/api/preferences", json={"styles": []}, headers=bearer(token))
    assert client.get("/api/preferences", headers=bearer(token)).json()["preferences"]["styles"] == []


def test_preferences_reject_unknown_values(client, token):
    shop = client.put("/api/preferences", json={"shops": ["没这牌子"]}, headers=bearer(token))
    assert shop.status_code == 400 and "没有这些选项" in shop.json()["detail"]

    style = client.put("/api/preferences", json={"styles": ["甜美"]}, headers=bearer(token))
    assert style.status_code == 400

    category = client.put("/api/preferences", json={"categories": ["和服"]}, headers=bearer(token))
    assert category.status_code == 400


def test_preferences_custom_keywords_roundtrip(client, token):
    """自定义标签是唯一的自由输入轴：任意字符串都能存，空白会折叠。"""
    saved = client.put("/api/preferences", json={"keywords": ["缎带", "  茶  会 "]}, headers=bearer(token))
    assert saved.status_code == 200
    assert saved.json()["preferences"]["keywords"] == ["缎带", "茶 会"]

    fetched = client.get("/api/preferences", headers=bearer(token)).json()
    assert fetched["preferences"]["keywords"] == ["缎带", "茶 会"]


def test_preferences_custom_keyword_too_long_is_400(client, token):
    response = client.put("/api/preferences", json={"keywords": ["字" * 21]}, headers=bearer(token))
    assert response.status_code == 400
    assert "最多 20 个字" in response.json()["detail"]


def _unique_keyword(entries):
    """从当前内容里挑一个只属于一条的 2 字片段。

    写死「齿轮」这类词等于把测试绑在某一条上新的系列名上：那条被换掉/归档，
    测试就红，而它要测的是"关键词命中会加分并给出原因"。
    """
    from app.domain.axes import entry_haystack

    haystacks = {entry.id: entry_haystack(entry) for entry in entries}
    for entry in entries:
        for index in range(len(entry.series) - 1):
            piece = entry.series[index:index + 2]
            if sum(1 for text in haystacks.values() if piece.lower() in text) == 1:
                return entry, piece
    raise AssertionError("当前内容里找不到只属于一条上新的关键词")


def test_preferences_match_report_flags_unmatched_keywords(client, token):
    """自由输入的安全阀：写了内容里没有的词，要明确告诉用户。"""
    from app.config import settings
    from app.domain import newarrivals as na

    entries, _ = na.load_newarrivals(settings.newarrivals_dir)
    _, matched_keyword = _unique_keyword(entries)

    saved = client.put("/api/preferences", json={
        "keywords": [matched_keyword, "量子计算"],
    }, headers=bearer(token))
    report = saved.json()["match_report"]["keywords"]
    assert report["matched"] == [matched_keyword]
    assert report["unmatched"] == ["量子计算"]

    fetched = client.get("/api/preferences", headers=bearer(token)).json()
    assert fetched["match_report"]["keywords"]["unmatched"] == ["量子计算"]


def test_feed_ranks_by_custom_keywords(client, token):
    from app.config import settings
    from app.domain import newarrivals as na

    entries, _ = na.load_newarrivals(settings.newarrivals_dir)
    target, keyword = _unique_keyword(entries)

    client.put("/api/preferences", json={"keywords": [keyword]}, headers=bearer(token))
    feed = client.get("/api/feed", headers=bearer(token)).json()

    rows = {item["id"]: item for item in feed["items"]}
    row = rows[target.id]
    assert any(reason.startswith("关键词：" + keyword) for reason in row["reasons"]), row["reasons"]
    # 不写死"排第 1"：并列时由 age_days 决定，那属于排序细节，不是这条要测的东西
    assert row["score"] == max(item["score"] for item in feed["items"])


def test_custom_keywords_match_notes_too(client, token):
    """关键词要能命中备注——用户写的是"我关心的词"，不只系列名。

    关键词从**当前内容**里取前两个字：写死「可拆」这类词，等于把这条测试绑在
    某一条上新的文案上，改一条 note 就红，而它要测的是"备注也参与命中"。
    """
    from app.config import settings
    from app.domain import newarrivals as na

    entries, _ = na.load_newarrivals(settings.newarrivals_dir)
    note = next(entry.note for entry in entries if entry.note)
    keyword = note[:2]

    client.put("/api/preferences", json={"keywords": [keyword]}, headers=bearer(token))
    feed = client.get("/api/feed", headers=bearer(token)).json()
    reasons = [reason for item in feed["items"] for reason in item["reasons"]]
    assert any(reason.startswith("关键词：" + keyword) for reason in reasons), reasons


# --------------------------------------------------------------------- 推荐


def test_feed_works_anonymously(client):
    payload = client.get("/api/feed").json()
    assert payload["count"] > 0
    assert payload["logged_in"] is False
    # 首页现在全部是真实数据（占位条目已归档），这条提示不该再出现
    assert payload["has_sample_data"] is False
    assert payload["described"] == []
    assert all(not item["reasons"] for item in payload["items"])


def _a_brand_with_entries() -> str:
    """从当前内容里拿一个真的存在的品牌，别把测试绑在某个牌子还在不在。"""
    from app.config import settings
    from app.domain import newarrivals as na

    entries, _ = na.load_newarrivals(settings.newarrivals_dir)
    assert entries, '内容目录里没有上新条目'
    return entries[0].brand

def test_feed_ranks_by_shops_axis(client, token):
    brand = _a_brand_with_entries()
    client.put("/api/preferences", json={"shops": [brand]}, headers=bearer(token))
    feed = client.get("/api/feed", headers=bearer(token)).json()
    top = feed["items"][0]
    assert top["brand"] == brand
    assert "你喜欢的店" in top["reasons"]
    assert top["preferred_shop"] is True
    assert feed["preferences"]["shops"] == [brand]


def test_feed_ranks_by_categories_axis(client, token):
    client.put("/api/preferences", json={"categories": ["OP"]}, headers=bearer(token))
    feed = client.get("/api/feed", headers=bearer(token)).json()
    reasons = [reason for item in feed["items"] for reason in item["reasons"]]
    assert any(reason.startswith("款式：OP") for reason in reasons), reasons


def test_feed_only_preferred(client, token):
    client.put("/api/preferences", json={"shops": ["baby"]}, headers=bearer(token))
    payload = client.get("/api/feed", params={"only_preferred": True}, headers=bearer(token)).json()
    assert payload["items"], "偏好里有店家时应该还有条目"
    assert all(item["preferred_shop"] for item in payload["items"])
    assert all(item["reasons"] == [] for item in payload["items"])


def test_feed_is_personalized_per_user(client):
    """同一个请求，两个账号拿到的排序依据不同。"""
    alice = register(client, "alice")
    bob = register(client, "bob")
    brand = _a_brand_with_entries()
    client.put("/api/preferences", json={"shops": [brand]}, headers=bearer(alice))

    alice_feed = client.get("/api/feed", headers=bearer(alice)).json()
    bob_feed = client.get("/api/feed", headers=bearer(bob)).json()

    assert alice_feed["items"][0]["brand"] == brand
    assert all(item["reasons"] == [] for item in bob_feed["items"])
    assert bob_feed["logged_in"] is True


def test_feed_days_filter_returns_only_recent(client):
    payload = client.get("/api/feed", params={"days": 7}).json()
    assert all(item["age_days"] <= 7 for item in payload["items"])


def test_follows_endpoints_are_gone(client):
    """「关注」已经并进「偏好」。旧接口必须真的消失，否则两套开关会同时存在。"""
    assert client.post("/api/follows", json={"brand": "baby"}).status_code in (404, 405)
    assert client.get("/api/brands").status_code in (404, 405)


# --------------------------------------------------------------------- 工具


SIZE_CHART = {
    "brand": "t",
    "display_name": "T",
    "chart_version": "test",
    "source": "测试官方尺码表",
    "fit": "正码",
    "verified": True,
    "categories": {
        "JSK": {
            "key": ["bust", "waist"],
            "ease": {"bust": 4, "waist": 2},
            "sizes": {
                "S": {"bust": 80, "waist": 64, "length": 88},
                "M": {"bust": 84, "waist": 68, "length": 92},
                "L": {"bust": 90, "waist": 74, "length": 96},
            },
        }
    },
}


def _write_chart(workdir) -> Path:
    import json

    content = workdir / "content"
    size_dir = content / "size"
    size_dir.mkdir(parents=True, exist_ok=True)
    (size_dir / "t.json").write_text(json.dumps(SIZE_CHART, ensure_ascii=False), encoding="utf-8")
    return content


def test_size_brands_expose_official_kinds(client):
    payload = client.get("/api/size/brands").json()
    brands = {item["brand"]: item for item in payload["brands"]}
    assert brands["ap"]["kind"] == "item_level"
    assert brands["ap"]["verified"] is True
    assert brands["baby"]["kind"] == "item_level"
    assert brands["huayuzhenzhuxia"]["kind"] == "item_level"
    assert brands["huayuzhenzhuxia"]["verified"] is False


def test_size_recommend_end_to_end(client, workdir, monkeypatch):
    from app.config import AppSettings, settings
    from app.routers import tools as tools_router

    app = AppSettings(data_dir=settings.data_dir, content_dir=_write_chart(workdir))
    monkeypatch.setattr(tools_router, "settings", app)

    body = client.post("/api/size/recommend", json={
        "brand": "t", "category": "JSK", "bust": 86, "waist": 68,
    }).json()
    assert body["recommended"] == "L"
    assert body["fit"] == "正码"


def test_size_unknown_brand_is_404(client):
    response = client.post("/api/size/recommend", json={
        "brand": "没这个牌子", "category": "JSK", "bust": 86,
    })
    assert response.status_code == 404


def test_size_without_measurement_is_400(client, workdir, monkeypatch):
    from app.config import AppSettings, settings
    from app.routers import tools as tools_router

    app = AppSettings(data_dir=settings.data_dir, content_dir=_write_chart(workdir))
    monkeypatch.setattr(tools_router, "settings", app)

    assert client.post("/api/size/recommend", json={
        "brand": "t", "category": "JSK",
    }).status_code == 400


def test_care_options(client):
    assert "蕾丝" in client.get("/api/care/options").json()["materials"]


def test_care_advice_stacks_rules(client):
    body = client.post("/api/care/advice", json={"materials": ["蕾丝"], "crafts": ["烫金"]}).json()
    assert body["machine_washable"] is False
    assert len(body["matched"]) >= 2


def test_care_advice_empty_selection_is_400(client):
    assert client.post("/api/care/advice", json={}).status_code == 400


# --------------------------------------------------------------------- 媒体与备份


def test_media_rejects_non_image(client):
    response = client.post("/api/media", files={"file": ("note.txt", b"hello", "text/plain")})
    assert response.status_code == 400


def test_media_accepts_png_and_serves_it(client):
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000d4944415478da63f8cfc0500f0006b50180a4a98c21000000004945"
        "4e44ae426082"
    )
    response = client.post("/api/media", files={"file": ("a.png", png, "image/png")})
    assert response.status_code == 201
    assert client.get(response.json()["url"]).status_code == 200


def test_export_contains_database_content_and_manifest(client):
    response = client.get("/api/export")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = archive.namelist()
        assert "app.db" in names
        assert "MANIFEST.json" in names
        assert any(name.startswith("content/newarrivals/") for name in names), names


def test_pwa_shell_is_served(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "<title>" in page.text
    assert client.get("/manifest.webmanifest").json()["display"] == "standalone"
    assert client.get("/sw.js").status_code == 200
