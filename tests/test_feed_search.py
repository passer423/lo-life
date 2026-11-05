"""搜索 / 排序 / 价格解析 / 出口链接 / 批量算码。

这几块共同点：**全是确定性的**，不起模型、不联网、不碰真实 `data/`。
所以测试要钉住的是"口径"而不是"能跑通"：

* 价格筛选**不偷偷放行**没标价的条目；
* 关键词是 **AND**，不是 OR；
* 价格排序时没价格的排最后，不冒充"最便宜"；
* 手录的 `link` 里写 `javascript:` 不会变成可点的按钮；
* 没有尺码表 / 品类不在表里 / 缺尺寸是**三种不同的结论**，谁也不冒充谁。
"""

from __future__ import annotations

import pytest

from app.domain import newarrivals as na
from app.domain import price, size, urls
from app.domain.newarrivals import NewArrival


def entry(**overrides) -> NewArrival:
    base = {
        "id": "e1",
        "brand": "baby",
        "brand_name": "Baby",
        "series": "秋日私语",
        "release_date": "2026-10-20",
        "price_range": "295 - 880",
        "items": ["JSK"],
        "styles": ["甜系"],
        "image": "",
        "link": "",
        "note": "",
        "source": "测试",
    }
    base.update(overrides)
    return NewArrival(**base)


# --------------------------------------------------------------------- 价格


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("295 - 880", (295.0, 880.0)),
        ("295~880", (295.0, 880.0)),
        ("¥699 起", (699.0, 699.0)),
        ("一件 480，整套 1100", (480.0, 1100.0)),
        ("¥1,280", (1280.0, 1280.0)),           # 千分位不能切成 1 和 280
        ("1280元", (1280.0, 1280.0)),
        ("", None),
        ("面议", None),
        ("0", None),                            # 0 不是价格，是没填
    ],
)
def test_parse_range(text, expected):
    assert price.parse_range(text) == expected


def test_parse_range_ignores_absurd_numbers():
    """误把手机号/QQ/完整日期填进价格栏时，宁可当成"没价格"。

    注意这里**不做**"看起来像年份就丢弃"这种猜测："2026" 无法和"2026 元"区分，
    硬猜只会在真有一件 2026 元的裙子时把它吞掉。只挡明显不可能的数字。
    """
    assert price.parse_range("13800138000") is None
    assert price.parse_range("202612") is None


def test_price_filter_drops_entries_without_price():
    """没标价的条目**不参与**价格筛选——悄悄放行比漏掉更让人恼火。"""
    assert price.within(None, price_max=500) is False
    assert price.within(None, price_min=100) is False
    assert price.within(None) is True           # 没有任何价格筛选时照常展示


def test_price_filter_uses_the_interval_not_a_single_number():
    bounds = price.parse_range("295 - 880")
    assert price.within(bounds, price_max=500) is True        # 最低价能买到
    assert price.within(bounds, price_max=200) is False
    assert price.within(bounds, price_min=900) is False       # 最高价也够不到
    assert price.within(bounds, price_min=300) is True


# --------------------------------------------------------------------- 链接


@pytest.mark.parametrize(
    "value",
    [
        "https://shop123456789.taobao.com",
        "http://example.com/a?b=1",
        "/media/cover.jpg",
        "",
    ],
)
def test_safe_url_allows_expected(value):
    assert urls.safe_url(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "//evil.com/x.jpg",                     # 协议相对地址
        "/media/../app.db",                     # 路径穿越
        "https://shop.taobao.com/a b",          # 空白字符
        "vbscript:msgbox(1)",
    ],
)
def test_safe_url_rejects_dangerous(value):
    assert urls.safe_url(value) == ""


def test_feed_serialization_drops_dangerous_link():
    rows = na.feed([entry(link="javascript:alert(1)", image="data:text/html,x")])
    assert rows[0]["link"] == ""
    assert rows[0]["image"] == ""


# --------------------------------------------------------------------- 搜索


def test_search_match_is_and_not_or():
    e = entry(series="秋日私语", items=["JSK"])
    assert na.search_match(e, "秋日") == ["秋日"]
    assert na.search_match(e, "秋日 JSK") == ["秋日", "JSK"]
    # 只命中一个词 → 整条不算命中，否则用户会以为搜索坏了
    assert na.search_match(e, "秋日 OP") is None
    assert na.search_match(e, "") == []


def test_search_match_is_case_insensitive():
    e = entry(series="JSK 特别版")
    assert na.search_match(e, "jsk") == ["jsk"]


def test_search_matches_notes_and_items():
    e = entry(series="无题", note="雾蓝色", items=["OP"])
    assert na.search_match(e, "雾蓝") == ["雾蓝"]
    assert na.search_match(e, "op") == ["op"]


def test_feed_filters_by_query():
    rows = na.feed(
        [entry(id="a", series="秋日私语"), entry(id="b", series="星屑")],
        q="秋日",
    )
    assert [row["id"] for row in rows] == ["a"]
    assert rows[0]["matched"] == ["秋日"]


def test_feed_query_does_not_affect_score():
    """搜索是过滤，不该变成加分——否则"为什么这条在前面"就说不清了。"""
    plain = na.feed([entry(id="a")])[0]
    searched = na.feed([entry(id="a")], q="秋日")[0]
    assert plain["score"] == searched["score"]


# --------------------------------------------------------------------- 排序


def test_sort_price_ascending_puts_missing_price_last():
    rows = na.feed(
        [
            entry(id="a", price_range="1200"),
            entry(id="b", price_range=""),          # 没价格
            entry(id="c", price_range="300 - 500"),
        ],
        sort="price_asc",
    )
    assert [row["id"] for row in rows] == ["c", "a", "b"]


def test_sort_price_descending_uses_high_end():
    rows = na.feed(
        [
            entry(id="a", price_range="1200 - 2000"),
            entry(id="b", price_range="900"),
        ],
        sort="price_desc",
    )
    assert [row["id"] for row in rows] == ["a", "b"]


def test_sort_date_is_newest_first():
    rows = na.feed(
        [
            entry(id="old", release_date="2026-09-01"),
            entry(id="new", release_date="2026-10-20"),
        ],
        sort="date",
    )
    assert [row["id"] for row in rows] == ["new", "old"]


def test_unknown_sort_is_rejected():
    with pytest.raises(ValueError, match="不支持的排序方式"):
        na.feed([entry()], sort="relevance")


# --------------------------------------------------------------------- 批量算码


def write_chart(root, *, brand="brand-a", fit="正码", categories=None):
    import json

    payload = {
        "brand": brand,
        "display_name": brand,
        "fit": fit,
        "source": "test",
        "chart_version": "test",
        "categories": categories
        or {
            "JSK": {
                "key": ["bust", "waist"],
                "ease": {"bust": 4, "waist": 2},
                "sizes": {
                    "S": {"bust": 88, "waist": 70},
                    "M": {"bust": 92, "waist": 74},
                },
            }
        },
    }
    directory = root / "size"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{brand}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    return directory


def test_pick_category_does_not_fall_back_to_another_category(workdir):
    """条目只有 KC 而表里只有 JSK → 返回 None，**不拿裙子的表算发饰**。"""
    charts = size.load_charts(write_chart(workdir))
    chart = charts["brand-a"]
    assert size.pick_category(chart, ["jsk"]) == "JSK"      # 大小写不敏感
    assert size.pick_category(chart, ["KC", "斗篷"]) is None


def test_recommend_for_entry_reports_no_chart(workdir):
    charts = size.load_charts(write_chart(workdir))
    result = size.recommend_for_entry(charts, "没录过的牌子", ["JSK"], {"bust": 84})
    assert result["status"] == "no_chart"


def test_recommend_for_entry_reports_no_category(workdir):
    charts = size.load_charts(write_chart(workdir))
    result = size.recommend_for_entry(charts, "brand-a", ["KC"], {"bust": 84})
    assert result["status"] == "no_category"
    assert result["available"] == ["JSK"]


def test_recommend_for_entry_reports_need_measurements(workdir):
    charts = size.load_charts(write_chart(workdir))
    result = size.recommend_for_entry(charts, "brand-a", ["JSK"], {})
    assert result["status"] == "need_measurements"


def test_recommend_for_entry_ok(workdir):
    charts = size.load_charts(write_chart(workdir))
    result = size.recommend_for_entry(charts, "brand-a", ["JSK"], {"bust": 84, "waist": 66})
    assert result["status"] == "ok"
    assert result["recommended"] == "S"
    assert result["sizes"] == ["S", "M"]


def test_recommend_for_entry_marks_unsellable_as_none(workdir):
    charts = size.load_charts(write_chart(workdir))
    result = size.recommend_for_entry(charts, "brand-a", ["JSK"], {"bust": 140})
    assert result["status"] == "ok"
    assert result["recommended"] is None       # 最大码都不够，不硬推


# --------------------------------------------------------------------- 接口


def test_feed_exposes_size_charts(client):
    payload = client.get("/api/feed").json()
    assert "size_charts" in payload
    # AP/Baby 官方只提供商品级尺寸：品牌级 categories 为空，卡片靠条目的 size 字段
    assert "baby" in payload["size_charts"]
    assert payload["size_charts"]["baby"]["kind"] == "item_level"
    assert payload["size_charts"]["baby"]["categories"] == {}
    assert any(item.get("size") for item in payload["items"])


def test_feed_query_param_filters(client):
    """关键词过滤：返回的每一条都必须真的命中。

    关键词**从内容里取**（而不是写死「秋日」）：写死的话，换个系列名这条测试就红，
    而它要测的是"过滤生效了"，不是"内容里恰好有这个字"。
    """
    everything = client.get("/api/feed").json()["items"]
    series = next(item["series"] for item in everything if item["series"])
    keyword = series[:2]

    payload = client.get("/api/feed", params={"q": keyword}).json()
    assert payload["query"] == keyword
    assert payload["items"]
    for item in payload["items"]:
        haystack = item["series"] + item["note"] + item["brand_name"] + " ".join(item["items"])
        assert keyword in haystack


def test_feed_query_without_match_is_empty_not_error(client):
    payload = client.get("/api/feed", params={"q": "不存在的系列名"}).json()
    assert payload["count"] == 0
    assert payload["items"] == []


def test_feed_sort_param_is_echoed(client):
    payload = client.get("/api/feed", params={"sort": "price_asc"}).json()
    assert payload["sort"] == "price_asc"
    lows = [item["price_low"] for item in payload["items"] if item["price_low"] is not None]
    assert lows == sorted(lows)


def test_feed_rejects_bad_sort(client):
    assert client.get("/api/feed", params={"sort": "smart"}).status_code == 422


def test_feed_rejects_inverted_price_range(client):
    response = client.get("/api/feed", params={"price_min": 900, "price_max": 800})
    assert response.status_code == 400


def test_feed_price_filter_drops_unpriced(client):
    payload = client.get("/api/feed", params={"price_max": 400}).json()
    assert all(item["price_low"] is not None for item in payload["items"])


def test_feed_sizes_endpoint(client):
    ids = [item["id"] for item in client.get("/api/feed").json()["items"]][:3]
    response = client.post("/api/feed/sizes", json={"ids": ids, "bust": 84, "waist": 66})
    assert response.status_code == 200
    body = response.json()
    assert set(body["sizes"]) == set(ids)
    assert body["measured"] == {"bust": 84.0, "waist": 66.0}


def test_feed_sizes_requires_at_least_one_measurement(client):
    ids = [item["id"] for item in client.get("/api/feed").json()["items"]][:1]
    response = client.post("/api/feed/sizes", json={"ids": ids})
    assert response.status_code == 400


def test_feed_sizes_reports_unknown_id(client):
    body = client.post("/api/feed/sizes", json={"ids": ["没有这条"], "bust": 84}).json()
    assert body["sizes"]["没有这条"]["status"] == "unknown_id"


def test_feed_sizes_never_500_on_brand_without_chart(client):
    """某个牌子没录尺码表，不能让整个批量请求失败。

    批量场景的契约是：**能算的照常返回，算不了的各自带一个状态**。
    只要有一条没表就整个 404/500，用户会连本来能算的那几条一起看不到。
    """
    ids = [item["id"] for item in client.get("/api/feed").json()["items"]]
    response = client.post("/api/feed/sizes", json={"ids": ids, "bust": 84, "waist": 66})
    assert response.status_code == 200
    statuses = {result["status"] for result in response.json()["sizes"].values()}
    assert statuses <= {"ok", "no_chart", "no_category", "need_measurements"}
