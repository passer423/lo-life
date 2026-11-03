"""上新推荐引擎测试。

排序 = 新鲜度 + 每个偏好轴的匹配加分。轴是数据驱动的，所以测试要证明：
**每个轴都能独立改变结论**、**每个轴都写进 reasons**、**轴之间有明确的强弱**，
以及"全选"不会把分数刷满。
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.domain import newarrivals as na
from app.domain.axes import AXIS_BY_KEY

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 10, 31)
SHOP = AXIS_BY_KEY["shops"].bonus
STYLE = AXIS_BY_KEY["styles"].bonus
KEYWORD = AXIS_BY_KEY["keywords"].bonus
CATEGORY = AXIS_BY_KEY["categories"].bonus


def day(offset: int) -> str:
    return (TODAY + timedelta(days=offset)).isoformat()


def write_entry(directory: Path, name: str, **fields) -> Path:
    payload = {
        "id": name,
        "brand": "brand-x",
        "brand_name": "X 牌",
        "series": "系列 " + name,
        "release_date": TODAY.isoformat(),
        "source": "test",
    }
    payload.update(fields)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture()
def directory(workdir):
    return workdir / "newarrivals"


# --------------------------------------------------------------------- 加载


def test_loader_skips_underscore_and_collects_problems(directory):
    write_entry(directory, "ok-1")
    (directory / "_template.json").write_text("{ broken", encoding="utf-8")
    (directory / "bad-json.json").write_text("{ broken", encoding="utf-8")
    write_entry(directory, "no-series", series="")
    write_entry(directory, "bad-date", release_date="2026/10/01")

    entries, problems = na.load_newarrivals(directory)

    assert [entry.id for entry in entries] == ["ok-1"]
    assert len(problems) == 3
    assert any("不是合法 JSON" in text for text in problems)
    assert any("缺少字段" in text for text in problems)
    assert any("release_date" in text for text in problems)


def test_unknown_styles_are_dropped(directory):
    """风格是固定词表：错别字不该悄悄变成一个永不匹配的标签。"""
    write_entry(directory, "s1", styles=["甜系", "甜美", "哥特"])
    entries, _ = na.load_newarrivals(directory)
    assert entries[0].styles == ["甜系", "哥特"]


def test_duplicate_id_is_reported(directory):
    write_entry(directory, "dup")
    (directory / "copy.json").write_text(
        json.dumps({"id": "dup", "brand": "b", "series": "s", "release_date": day(0)}),
        encoding="utf-8",
    )
    entries, problems = na.load_newarrivals(directory)
    assert len(entries) == 1
    assert any("重复" in text for text in problems)


def test_missing_directory_is_a_problem_not_a_crash(workdir):
    entries, problems = na.load_newarrivals(workdir / "nope")
    assert entries == []
    assert problems and "没有上新目录" in problems[0]


# --------------------------------------------------------------------- 基础排序


def test_freshness_decides_order_without_preferences(directory):
    write_entry(directory, "old", release_date=day(-20))
    write_entry(directory, "new", release_date=day(-1))
    entries, _ = na.load_newarrivals(directory)

    rows = na.feed(entries, today=TODAY)
    assert [row["id"] for row in rows] == ["new", "old"]
    assert rows[0]["score"] == 29.0        # (30-1)/30*30
    assert rows[0]["reasons"] == []        # 没偏好就没有加分项


def test_exact_tie_breaks_by_id_stably(directory):
    write_entry(directory, "b-entry", brand="brand-a", release_date=day(-1))
    write_entry(directory, "a-entry", brand="brand-a", release_date=day(-1))
    entries, _ = na.load_newarrivals(directory)

    rows = na.feed(entries, preferences={"shops": ["brand-a"]}, today=TODAY)
    assert [row["score"] for row in rows] == [SHOP + 29.0, SHOP + 29.0]
    assert [row["id"] for row in rows] == ["a-entry", "b-entry"]      # 同分时按 id 稳定排序


# --------------------------------------------------------------------- 偏好的各轴


def test_shops_axis_outranks_freshness(directory):
    write_entry(directory, "fresh-other", brand="brand-a", release_date=day(-1))
    write_entry(directory, "stale-preferred", brand="brand-b", release_date=day(-20))
    entries, _ = na.load_newarrivals(directory)

    rows = na.feed(entries, preferences={"shops": ["brand-b"]}, today=TODAY)
    assert [row["id"] for row in rows] == ["stale-preferred", "fresh-other"]
    assert rows[0]["score"] == SHOP + 10.0
    assert rows[0]["reasons"] == ["你喜欢的店"]
    assert rows[0]["preferred_shop"] is True
    assert rows[1]["preferred_shop"] is False


def test_styles_axis_adds_score_and_reason(directory):
    write_entry(directory, "sweet", brand="brand-a", release_date=day(-1), styles=["甜系"])
    write_entry(directory, "plain", brand="brand-b", release_date=day(-1))
    entries, _ = na.load_newarrivals(directory)

    rows = na.feed(entries, preferences={"styles": ["甜系"]}, today=TODAY)
    assert rows[0]["id"] == "sweet"
    assert rows[0]["score"] == 29.0 + STYLE
    assert rows[0]["reasons"] == ["风格：甜系"]


def test_categories_axis_matches_items(directory):
    write_entry(directory, "with_op", brand="brand-a", release_date=day(-1), items=["OP", "KC"])
    write_entry(directory, "with_jsk", brand="brand-b", release_date=day(-1), items=["JSK"])
    entries, _ = na.load_newarrivals(directory)

    rows = na.feed(entries, preferences={"categories": ["OP"]}, today=TODAY)
    assert rows[0]["id"] == "with_op"
    assert rows[0]["score"] == 29.0 + CATEGORY
    assert rows[0]["reasons"] == ["款式：OP"]


def test_keywords_axis_matches_series_and_note(directory):
    """自定义标签是自由输入，所以它按**子串**匹配条目文本，而不是要求值先存在。"""
    write_entry(directory, "by-series", brand="brand-a", release_date=day(-1), series="缎带与齿轮")
    write_entry(directory, "by-note", brand="brand-b", release_date=day(-1), note="可以机洗，注意烫金")
    write_entry(directory, "plain", brand="brand-c", release_date=day(-1))
    entries, _ = na.load_newarrivals(directory)

    rows = na.feed(entries, preferences={"keywords": ["缎带", "烫金"]}, today=TODAY)
    reasons = {row["id"]: row["reasons"] for row in rows}
    assert reasons["by-series"] == ["关键词：缎带"]
    assert reasons["by-note"] == ["关键词：烫金"]
    assert reasons["plain"] == []
    assert rows[0]["score"] > 29.0


def test_keywords_axis_is_case_insensitive(directory):
    write_entry(directory, "e1", release_date=day(-1), series="Lolita 茶会")
    entries, _ = na.load_newarrivals(directory)

    rows = na.feed(entries, preferences={"keywords": ["lolita"]}, today=TODAY)
    assert rows[0]["reasons"] == ["关键词：lolita"]


def test_keywords_axis_matches_brand_and_items(directory):
    write_entry(directory, "e1", brand="brand-a", brand_name="缎带屋", release_date=day(-1), items=["OP"])
    entries, _ = na.load_newarrivals(directory)

    rows = na.feed(entries, preferences={"keywords": ["缎带屋"]}, today=TODAY)
    assert rows[0]["reasons"] == ["关键词：缎带屋"]

    rows = na.feed(entries, preferences={"keywords": ["OP"]}, today=TODAY)
    assert rows[0]["reasons"] == ["关键词：OP"]


def test_keywords_axis_is_capped(directory):
    write_entry(directory, "e1", release_date=day(-1), series="缎带 蕾丝 茶会")
    entries, _ = na.load_newarrivals(directory)

    rows = na.feed(entries, preferences={"keywords": ["缎带", "蕾丝", "茶会"]}, today=TODAY)
    assert rows[0]["score"] == 29.0 + KEYWORD * 2      # 最多按两个计分


def test_keywords_with_no_match_add_nothing(directory):
    write_entry(directory, "e1", release_date=day(-1), series="白鸟笼")
    entries, _ = na.load_newarrivals(directory)

    rows = na.feed(entries, preferences={"keywords": ["量子计算"]}, today=TODAY)
    assert rows[0]["score"] == 29.0
    assert rows[0]["reasons"] == []


def test_axis_strength_order():
    """店家 > 风格 > 自定义关键词 > 款式，否则各轴的区别就没有意义。"""
    assert SHOP > STYLE > KEYWORD > CATEGORY
    # 每一项都要能压过"新鲜度满格但完全不匹配"
    assert CATEGORY < 30
    assert SHOP > 30


def test_style_and_category_bonus_are_capped(directory):
    write_entry(
        directory, "many", release_date=day(-1),
        styles=["甜系", "古典", "哥特", "和风"], items=["JSK", "OP", "SK"],
    )
    entries, _ = na.load_newarrivals(directory)

    rows = na.feed(
        entries,
        preferences={"styles": ["甜系", "古典", "哥特", "和风"], "categories": ["JSK", "OP", "SK"]},
        today=TODAY,
    )
    assert rows[0]["score"] == 29.0 + STYLE * 2 + CATEGORY * 2


def test_axes_stack(directory):
    write_entry(
        directory, "all", brand="brand-a", release_date=day(-1),
        styles=["甜系"], items=["JSK"],
    )
    entries, _ = na.load_newarrivals(directory)

    rows = na.feed(
        entries,
        preferences={"shops": ["brand-a"], "styles": ["甜系"], "categories": ["JSK"]},
        today=TODAY,
    )
    assert rows[0]["score"] == SHOP + STYLE + CATEGORY + 29.0
    assert rows[0]["reasons"] == ["你喜欢的店", "风格：甜系", "款式：JSK"]


def test_unmatched_preferences_add_nothing(directory):
    write_entry(directory, "plain", brand="brand-a", release_date=day(-1), items=["JSK"])
    entries, _ = na.load_newarrivals(directory)

    rows = na.feed(
        entries,
        preferences={"shops": ["brand-z"], "styles": ["哥特"], "categories": ["OP"]},
        today=TODAY,
    )
    assert rows[0]["score"] == 29.0
    assert rows[0]["reasons"] == []


# --------------------------------------------------------------------- 过滤


def test_only_preferred_filters_and_ignores_weighting(directory):
    write_entry(directory, "a-old", brand="brand-a", release_date=day(-15))
    write_entry(directory, "a-new", brand="brand-a", release_date=day(-2))
    write_entry(directory, "b-new", brand="brand-b", release_date=day(-1))
    entries, _ = na.load_newarrivals(directory)

    rows = na.feed(
        entries,
        preferences={"shops": ["brand-a"], "styles": ["甜系"]},
        only_preferred=True,
        today=TODAY,
    )
    assert [row["id"] for row in rows] == ["a-new", "a-old"]
    assert all(row["reasons"] == [] for row in rows)   # 加权已无区分意义


def test_only_preferred_with_empty_shops_returns_nothing(directory):
    write_entry(directory, "a", brand="brand-a")
    entries, _ = na.load_newarrivals(directory)
    assert na.feed(entries, preferences={"shops": []}, only_preferred=True, today=TODAY) == []


def test_days_window(directory):
    write_entry(directory, "recent", release_date=day(-3))
    write_entry(directory, "old", release_date=day(-40))
    entries, _ = na.load_newarrivals(directory)

    assert [row["id"] for row in na.feed(entries, today=TODAY, days=7)] == ["recent"]
    assert len(na.feed(entries, today=TODAY, days=365)) == 2


def test_is_new_flag(directory):
    write_entry(directory, "brand-new", release_date=day(-1))
    write_entry(directory, "older", release_date=day(-20))
    entries, _ = na.load_newarrivals(directory)
    rows = {row["id"]: row for row in na.feed(entries, today=TODAY)}
    assert rows["brand-new"]["is_new"] is True
    assert rows["older"]["is_new"] is False


def test_future_release_is_marked_not_treated_as_very_new(directory):
    write_entry(directory, "coming", release_date=day(5))
    entries, _ = na.load_newarrivals(directory)
    row = na.feed(entries, today=TODAY)[0]
    assert row["future"] is True
    assert row["is_new"] is False
    assert row["age_days"] == -5


def test_limit(directory):
    for index in range(5):
        write_entry(directory, f"item-{index}", release_date=day(-index))
    entries, _ = na.load_newarrivals(directory)
    assert len(na.feed(entries, today=TODAY, limit=2)) == 2


# --------------------------------------------------------------------- 汇总


def test_brand_index_counts_and_latest(directory):
    write_entry(directory, "a1", brand="brand-a", release_date=day(-10))
    write_entry(directory, "a2", brand="brand-a", release_date=day(-2))
    write_entry(directory, "b1", brand="brand-b", release_date=day(-5))
    entries, _ = na.load_newarrivals(directory)

    index = {row["brand"]: row for row in na.brand_index(entries)}
    assert index["brand-a"]["count"] == 2
    assert index["brand-a"]["latest"] == day(-2)
    assert index["brand-b"]["count"] == 1


def test_summary_reports_preference_counts(directory):
    write_entry(directory, "s1", brand="brand-a", release_date=day(-1))
    write_entry(directory, "s2", brand="brand-b", release_date=day(-30))
    entries, _ = na.load_newarrivals(directory)

    info = na.summary(
        entries,
        {"shops": ["brand-a"], "styles": ["甜系", "古典"], "categories": []},
        today=TODAY,
    )
    assert info["total"] == 2
    assert info["brands"] == 2
    assert info["recent"] == 1
    assert info["latest"] == day(-1)
    assert info["preferences"] == {"shops": 1, "styles": 2, "categories": 0}


# --------------------------------------------------------------------- 真实内容


def test_real_content_is_valid():
    """真实 content/newarrivals 必须全部能读、都写了来源、风格都在词表内。"""
    entries, problems = na.load_newarrivals(ROOT / "content" / "newarrivals")
    assert not problems, f"上新数据有问题：{problems}"
    assert entries, "content/newarrivals 下没有任何条目"
    ids = [entry.id for entry in entries]
    assert len(ids) == len(set(ids))
    for entry in entries:
        assert entry.source, f"{entry.id} 没写 source"
        assert entry.released is not None
        assert entry.brand and entry.series
        assert all(style in na.STYLE_OPTIONS for style in entry.styles)
