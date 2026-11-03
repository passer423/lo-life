"""偏好轴的定义、选项、校验与描述。

这一层是纯逻辑，最该被钉死的是：
* 每个轴的选项**来自内容**（店家/款式从条目反推，风格是词表），且带条数；
* 校验会挡住未知轴、未知选项；
* **自定义标签**（唯一的自由输入轴）有边界、会去重、能回报命中情况；
* 描述能把 key 翻成人话（界面上要显示"Baby"而不是 `baby`）。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from app.domain import newarrivals as na
from app.domain import preferences as pref
from app.domain.axes import AXES, AXIS_BY_KEY, AXIS_KEYS, CUSTOM_MAX_ITEMS, CUSTOM_MAX_LEN

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 10, 31)


def entry(name: str, **fields) -> na.NewArrival:
    payload = {
        "id": name,
        "brand": "brand-a",
        "brand_name": "A 牌",
        "series": "系列 " + name,
        "release_date": TODAY.isoformat(),
        "styles": ["甜系"],
        "items": ["JSK"],
        "note": "",
        "source": "test",
    }
    payload.update(fields)
    return na.NewArrival(
        id=payload["id"],
        brand=payload["brand"],
        brand_name=payload["brand_name"],
        series=payload["series"],
        release_date=payload["release_date"],
        items=list(payload["items"]),
        styles=list(payload["styles"]),
        note=payload["note"],
        source=payload["source"],
    )


# --------------------------------------------------------------------- 轴定义


def test_axes_are_declared_with_metadata():
    assert AXIS_KEYS == ("shops", "styles", "keywords", "categories")
    for axis in AXES:
        assert axis.label and axis.hint
        assert axis.bonus > 0
        assert axis.max_matches >= 1


def test_only_keywords_axis_allows_free_input():
    """自由输入是例外，不是常态：其余轴的値必须先在内容里存在。"""
    assert AXIS_BY_KEY["keywords"].allow_custom is True
    for key in ("shops", "styles", "categories"):
        assert AXIS_BY_KEY[key].allow_custom is False


def test_axis_strength_order():
    assert (
        AXIS_BY_KEY["shops"].bonus
        > AXIS_BY_KEY["styles"].bonus
        > AXIS_BY_KEY["keywords"].bonus
        > AXIS_BY_KEY["categories"].bonus
    )


# --------------------------------------------------------------------- 选项


def test_build_options_counts_and_labels():
    entries = [
        entry("e1", brand="brand-a"),
        entry("e2", brand="brand-a", styles=["甜系", "哥特"], items=["OP"]),
        entry("e3", brand="brand-b", styles=["哥特"]),
    ]
    options = pref.build_options(entries)

    shops = {row["value"]: row for row in options["shops"]}
    assert shops["brand-a"]["count"] == 2
    assert shops["brand-a"]["label"] == "A 牌"
    assert shops["brand-b"]["count"] == 1

    styles = {row["value"]: row for row in options["styles"]}
    assert styles["甜系"]["count"] == 2
    assert styles["哥特"]["count"] == 2
    assert styles["和风"]["count"] == 0        # 词表里没被用到的也要列出来

    categories = {row["value"]: row for row in options["categories"]}
    assert categories["JSK"]["count"] == 2
    assert categories["OP"]["count"] == 1

    # 自定义轴没有预置选项
    assert options["keywords"] == []


def test_build_options_on_empty_content():
    options = pref.build_options([])
    assert options["shops"] == []
    assert options["categories"] == []
    assert options["keywords"] == []
    assert len(options["styles"]) == len(na.STYLE_OPTIONS)


# --------------------------------------------------------------------- 校验


def test_validate_accepts_known_values():
    options = pref.build_options([entry("e1")])
    cleaned, error = pref.validate({"shops": ["brand-a"], "styles": ["甜系"]}, options)
    assert error == ""
    assert cleaned == {"shops": ["brand-a"], "styles": ["甜系"]}


def test_validate_rejects_unknown_axis():
    options = pref.build_options([entry("e1")])
    cleaned, error = pref.validate({"prices": ["100"]}, options)
    assert cleaned == {}
    assert "未知的偏好轴" in error


def test_validate_rejects_unknown_value():
    options = pref.build_options([entry("e1")])
    _, error = pref.validate({"styles": ["甜美"]}, options)
    assert "没有这些选项" in error


def test_validate_dedupes():
    options = pref.build_options([entry("e1")])
    cleaned, error = pref.validate({"styles": ["甜系", "甜系"]}, options)
    assert error == ""
    assert cleaned["styles"] == ["甜系"]


def test_validate_allows_empty_list():
    options = pref.build_options([entry("e1")])
    cleaned, error = pref.validate({"shops": []}, options)
    assert error == ""
    assert cleaned["shops"] == []


# --------------------------------------------------------------------- 自定义标签


def test_custom_keywords_are_accepted():
    options = pref.build_options([entry("e1")])
    cleaned, error = pref.validate({"keywords": ["缎带", "茶会"]}, options)
    assert error == ""
    assert cleaned["keywords"] == ["缎带", "茶会"]


def test_custom_keywords_collapse_whitespace():
    options = pref.build_options([entry("e1")])
    cleaned, error = pref.validate({"keywords": ["  缎  带 ", "缎 带"]}, options)
    assert error == ""
    assert cleaned["keywords"] == ["缎 带"]      # 空白折叠 + 去重


def test_custom_keywords_reject_too_long():
    options = pref.build_options([entry("e1")])
    _, error = pref.validate({"keywords": ["字" * (CUSTOM_MAX_LEN + 1)]}, options)
    assert f"最多 {CUSTOM_MAX_LEN} 个字" in error


def test_custom_keywords_accept_boundary_length():
    options = pref.build_options([entry("e1")])
    cleaned, error = pref.validate({"keywords": ["字" * CUSTOM_MAX_LEN]}, options)
    assert error == ""
    assert len(cleaned["keywords"][0]) == CUSTOM_MAX_LEN


def test_custom_keywords_reject_too_many():
    options = pref.build_options([entry("e1")])
    _, error = pref.validate({"keywords": [f"词{i}" for i in range(CUSTOM_MAX_ITEMS + 1)]}, options)
    assert f"最多 {CUSTOM_MAX_ITEMS} 个标签" in error


def test_custom_keywords_drop_blank():
    options = pref.build_options([entry("e1")])
    cleaned, error = pref.validate({"keywords": ["  ", "", "缎带"]}, options)
    assert error == ""
    assert cleaned["keywords"] == ["缎带"]


# --------------------------------------------------------------------- 命中回报


def test_match_report_separates_matched_and_unmatched():
    entries = [
        entry("e1", series="缎带与齿轮"),
        entry("e2", series="白鸟笼", note="立领设计"),
    ]
    report = pref.match_report(entries, {"keywords": ["缎带", "鸟笼", "茶会"]})
    assert report["keywords"]["matched"] == ["缎带", "鸟笼"]
    assert report["keywords"]["unmatched"] == ["茶会"]


def test_match_report_is_case_insensitive():
    entries = [entry("e1", series="Lolita 茶会")]
    report = pref.match_report(entries, {"keywords": ["lolita"]})
    assert report["keywords"]["matched"] == ["lolita"]


def test_match_report_empty_without_keywords():
    assert pref.match_report([entry("e1")], {"shops": ["brand-a"]}) == {}


def test_match_report_empty_when_no_keywords_set():
    assert pref.match_report([entry("e1")], {"keywords": []}) == {}


# --------------------------------------------------------------------- 描述


def test_describe_translates_ids_to_labels():
    options = pref.build_options([entry("e1"), entry("e2", brand="brand-b", brand_name="B 牌")])
    rows = pref.describe({"shops": ["brand-a", "brand-b"], "styles": ["甜系"]}, options)
    by_key = {row["key"]: row for row in rows}
    assert by_key["shops"]["values"] == ["A 牌", "B 牌"]
    assert by_key["styles"]["values"] == ["甜系"]
    assert by_key["shops"]["bonus"] > 0


def test_describe_passes_custom_keywords_through():
    options = pref.build_options([entry("e1")])
    rows = pref.describe({"keywords": ["缎带"]}, options)
    assert rows[0]["values"] == ["缎带"]        # 没有 label 映射时原样显示


def test_describe_skips_empty_axes():
    options = pref.build_options([entry("e1")])
    assert pref.describe({}, options) == []


# --------------------------------------------------------------------- 真实内容


def test_real_content_produces_usable_options():
    entries, problems = na.load_newarrivals(ROOT / "content" / "newarrivals")
    assert not problems
    options = pref.build_options(entries)
    assert options["shops"] and options["categories"]
    # 每个选项都必须能通过校验，否则界面上勾了却存不进去
    cleaned, error = pref.validate(
        {key: [row["value"] for row in rows] for key, rows in options.items() if rows},
        options,
    )
    assert error == ""
    assert cleaned["shops"]
