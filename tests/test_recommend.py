"""专属推荐：偏好 tag → 命中筛选 → 挂一条相关知识。

钉住三条口径：

* 没命中任何偏好 tag 的条目**不进推荐**（"专属"不能靠把全量塞进来充数）；
* 同一条目命中多个轴时，**每个轴的分组里都要出现**；
* 知识索引没建时，推荐**照样 200**，只是 `knowledge` 为 null——
  推荐是确定性计算，不该被另一个子系统的状态拖下水。
"""

from __future__ import annotations

import pytest

from app.domain import newarrivals as na
from app.domain import recommend as rec

PASSWORD = "password123"


def bearer(token: str) -> dict:
    return {"Authorization": "Bearer " + token}


@pytest.fixture()
def token(client) -> str:
    response = client.post(
        "/api/auth/register", json={"username": "recommend-tester", "password": PASSWORD}
    )
    assert response.status_code == 201, response.text
    return response.json()["token"]


def entry(**overrides) -> na.NewArrival:
    base = dict(
        id="e1",
        brand="baby",
        brand_name="Baby",
        series="Dark Night Sweets柄 ジャンパースカートII",
        release_date="2026-10-29",
        items=["JSK"],
        styles=["甜系"],
        price_range="41800",
        currency="JPY",
    )
    base.update(overrides)
    return na.NewArrival(**base)


# --------------------------------------------------------------------- 纯逻辑


def test_matches_of_hits_every_axis():
    tags = rec.matches_of(entry(), {"shops": ["baby"], "styles": ["甜系"]})
    assert {(tag.axis, tag.tag) for tag in tags} == {("shops", "baby"), ("styles", "甜系")}


def test_matches_of_is_empty_when_nothing_overlaps():
    assert rec.matches_of(entry(), {"shops": ["ap"], "styles": ["哥特"]}) == []


def test_matches_are_shaped_like_the_api():
    tags = rec.matches_of(entry(), {"shops": ["baby"]})
    assert tags[0].as_dict() == {"axis": "shops", "label": "喜欢的店家", "tag": "baby"}


def test_groups_put_the_same_item_in_every_matching_axis():
    """一条上新同时命中店家轴和风格轴时，两个分组里都要有它。"""
    entry_a = entry(id="a")
    pairs = [
        ("a", rec.matches_of(entry_a, {"shops": ["baby"], "styles": ["甜系"]})),
        ("b", rec.matches_of(entry(id="b", styles=["哥特"]), {"styles": ["甜系"]})),
    ]
    groups = {group.axis: group for group in rec.group_by_axis(pairs)}
    assert groups["shops"].item_ids == ["a"]
    assert groups["styles"].item_ids == ["a"]


def test_groups_keep_recommendation_order():
    pairs = [
        ("first", rec.matches_of(entry(id="first"), {"shops": ["baby"]})),
        ("second", rec.matches_of(entry(id="second"), {"shops": ["baby"]})),
    ]
    assert rec.group_by_axis(pairs)[0].item_ids == ["first", "second"]


def test_knowledge_query_uses_items_not_series():
    """知识库里讲的是品类（JSK），不是系列名——系列名实测命中 0 条。"""
    assert rec.knowledge_query(entry()) == "JSK"
    assert rec.knowledge_query(entry(items=[])) == ""


# --------------------------------------------------------------------- 接口


def test_recommendations_require_login(client):
    response = client.get("/api/recommendations")
    assert response.status_code == 401


def test_recommendations_explain_empty_preferences(client, token):
    response = client.get("/api/recommendations", headers=bearer(token))
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert "偏好" in body["empty_reason"]


def test_recommendations_only_include_matched_items(client, token):
    client.put("/api/preferences", json={"shops": ["baby"]}, headers=bearer(token))
    body = client.get("/api/recommendations", headers=bearer(token)).json()

    assert body["count"] > 0
    for item in body["items"]:
        assert item["brand"] == "baby"
        assert any(match["axis"] == "shops" for match in item["matches"])


def test_recommendations_group_by_axis(client, token):
    client.put("/api/preferences", json={"shops": ["baby"], "styles": ["甜系"]}, headers=bearer(token))
    body = client.get("/api/recommendations", headers=bearer(token)).json()

    axes = {group["axis"] for group in body["groups"]}
    assert "shops" in axes
    assert body["groups"][0]["count"] > 0


def test_recommendations_are_sorted_by_release_date_desc(client, token):
    """默认按上新日期倒序——用户要的是"最近上了什么"，不是"算法觉得你多喜欢"。"""
    client.put("/api/preferences", json={"shops": ["baby", "ap"]}, headers=bearer(token))
    body = client.get("/api/recommendations", headers=bearer(token)).json()

    dates = [item["release_date"] for item in body["items"]]
    assert dates == sorted(dates, reverse=True), dates


def test_recommendations_default_limit_is_ten(client, token):
    client.put("/api/preferences", json={"shops": ["baby", "ap"]}, headers=bearer(token))
    body = client.get("/api/recommendations", headers=bearer(token)).json()

    assert body["limit"] == 10
    assert body["sort"] == "date"
    assert body["count"] <= 10


def test_recommendations_can_still_sort_by_score(client, token):
    client.put("/api/preferences", json={"shops": ["baby", "ap"]}, headers=bearer(token))
    body = client.get("/api/recommendations", params={"sort": "score"}, headers=bearer(token)).json()

    scores = [item["score"] for item in body["items"]]
    assert scores == sorted(scores, reverse=True), scores


def test_recommendations_carry_card_data(client, token):
    """推荐接口自带尺码表和店铺表。

    推荐页现在**就是**主列表：如果这两张表只由 /api/feed 返回，那么"只看推荐"模式下
    卡片上的尺码徽标和「取图」按钮会因为缺数据而悄悄消失。
    """
    client.put("/api/preferences", json={"shops": ["baby"]}, headers=bearer(token))
    body = client.get("/api/recommendations", headers=bearer(token)).json()

    assert "size_charts" in body
    assert "shops" in body
    assert "baby" in body["size_charts"]
    assert "has_sample_data" in body


def test_recommendations_survive_missing_knowledge_index(client, token):
    """测试环境没有建索引：推荐必须照常返回，只是没有相关知识。"""
    client.put("/api/preferences", json={"shops": ["baby"]}, headers=bearer(token))
    body = client.get("/api/recommendations", headers=bearer(token)).json()

    assert body["count"] > 0
    assert body["kb_ready"] is False
    assert all(item["knowledge"] is None for item in body["items"])
