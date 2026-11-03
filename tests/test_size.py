"""尺码引擎测试。

重点不在"能算出个数"，而在三条口径：

* 富余量必须生效——否则紧身数据会一路选小一码；
* 版型偏移必须改变结论（`偏小` 和 `正码` 用同一组尺码表，应给出不同答案）；
* 不合适时**必须说没有**，不能硬推一个最大的码。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.domain import size

ROOT = Path(__file__).resolve().parents[1]


def write_chart(root: Path, name: str, **overrides) -> Path:
    payload = {
        "brand": name,
        "display_name": name,
        "chart_version": "test",
        "source": "test",
        "fit": "正码",
        "categories": {
            "JSK": {
                "key": ["bust", "waist"],
                "ease": {"bust": 4, "waist": 2},
                "sizes": {
                    "S": {"bust": 88, "waist": 70},
                    "M": {"bust": 92, "waist": 74},
                    "L": {"bust": 96, "waist": 78},
                },
            }
        },
    }
    payload.update(overrides)
    directory = root / "size"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return directory


def test_loader_skips_underscore_files(workdir):
    directory = write_chart(workdir, "brand-a")
    (directory / "_template.json").write_text("{ not json", encoding="utf-8")   # 会被跳过
    charts = size.load_charts(directory)
    assert set(charts) == {"brand-a"}


def test_loader_raises_on_broken_json(workdir):
    directory = workdir / "size"
    directory.mkdir(parents=True)
    (directory / "bad.json").write_text("{ nope", encoding="utf-8")
    with pytest.raises(ValueError, match="不是合法 JSON"):
        size.load_charts(directory)


def test_ease_is_applied(workdir):
    charts = size.load_charts(write_chart(workdir, "brand-a"))
    # 胸围 84 + 富余 4 = 88，刚好等于 S 的成衣尺寸 → 可穿 S
    result = size.recommend(charts, "brand-a", "JSK", {"bust": 84})
    assert result["recommended"] == "S"
    assert result["required"]["bust"] == 88.0


def test_ease_pushes_to_next_size(workdir):
    charts = size.load_charts(write_chart(workdir, "brand-a"))
    # 胸围 85 + 4 = 89 > 88 → S 不够，落到 M
    assert size.recommend(charts, "brand-a", "JSK", {"bust": 85})["recommended"] == "M"


def test_fit_offset_changes_verdict(workdir):
    regular = size.load_charts(write_chart(workdir, "regular"))
    small = size.load_charts(write_chart(workdir, "small", fit="偏小"))

    # 同一组身体数据、同一份尺码表，只因为标了「偏小」就该往上走一码
    assert size.recommend(regular, "regular", "JSK", {"bust": 84})["recommended"] == "S"
    assert size.recommend(small, "small", "JSK", {"bust": 84})["recommended"] == "M"


def test_fit_big_goes_down(workdir):
    charts = size.load_charts(write_chart(workdir, "big", fit="偏大"))
    # 胸围 87 + 4 - 2 = 89 → 仍略高于 88，取 M；把尺度再压一格看它确实更宽松
    result = size.recommend(charts, "big", "JSK", {"bust": 86})
    assert result["recommended"] == "S"          # 86+4-2=88 ≤ 88
    assert result["fit"] == "偏大"


def test_too_large_returns_no_recommendation(workdir):
    charts = size.load_charts(write_chart(workdir, "brand-a"))
    result = size.recommend(charts, "brand-a", "JSK", {"bust": 120})
    assert result["recommended"] is None
    assert result["confidence"] == "low"
    assert any("最大只有" in text for text in result["warnings"])
    assert all(not row["ok"] for row in result["evaluated"])


def test_missing_brand_is_explicit(workdir):
    charts = size.load_charts(write_chart(workdir, "brand-a"))
    with pytest.raises(size.ChartNotFound, match="没有「nope」的尺码表"):
        size.recommend(charts, "nope", "JSK", {"bust": 84})


def test_missing_category_is_explicit(workdir):
    charts = size.load_charts(write_chart(workdir, "brand-a"))
    with pytest.raises(size.ChartNotFound, match="还没有 OP"):
        size.recommend(charts, "brand-a", "OP", {"bust": 84})


def test_no_measurement_is_rejected(workdir):
    charts = size.load_charts(write_chart(workdir, "brand-a"))
    with pytest.raises(ValueError, match="至少要给一项尺寸"):
        size.recommend(charts, "brand-a", "JSK", {})


def test_irrelevant_dimensions_are_ignored(workdir):
    charts = size.load_charts(write_chart(workdir, "brand-a"))
    # 臀围不在 JSK 的 key 里，不参与判断（只给臀围就等于什么都没给）
    with pytest.raises(ValueError):
        size.recommend(charts, "brand-a", "JSK", {"hip": 90})


def test_numeric_size_ordering(workdir):
    directory = write_chart(workdir, "numeric")
    path = directory / "numeric.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["categories"]["JSK"]["sizes"] = {
        "2": {"bust": 100, "waist": 80},
        "10": {"bust": 104, "waist": 84},
        "1": {"bust": 96, "waist": 76},
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    charts = size.load_charts(directory)
    result = size.recommend(charts, "numeric", "JSK", {"bust": 90})
    # 必须按数值排（1 → 2 → 10），不能按字符串排（1 → 10 → 2）
    assert [row["size"] for row in result["evaluated"]] == ["1", "2", "10"]
    assert result["recommended"] == "1"


def test_medium_confidence_when_only_one_dimension(workdir):
    charts = size.load_charts(write_chart(workdir, "brand-a"))
    assert size.recommend(charts, "brand-a", "JSK", {"bust": 84})["confidence"] == "medium"
    assert size.recommend(charts, "brand-a", "JSK", {"bust": 84, "waist": 64})["confidence"] == "high"


def test_real_size_directory_loads():
    """真实 content/size 必须能解析，且每条都写了来源。"""
    charts = size.load_charts(ROOT / "content" / "size")
    assert charts, "content/size 下没有任何尺码表"
    for chart in charts.values():
        assert chart.source, f"{chart.brand} 没写 source"
        assert chart.fit in size.FIT_OFFSET
        if chart.kind == "item_level":
            # 官方只给商品级实测：不允许硬编品牌级 S/M/L
            assert chart.categories == {}
        else:
            assert chart.categories


def test_list_brands_exposes_kind_and_category_keys(workdir):
    charts = size.load_charts(write_chart(workdir, "brand-a"))
    brand = size.list_brands(charts)[0]
    assert brand["kind"] == "size_chart"
    assert brand["verified"] is False
    assert "bust" in brand["category_keys"]["JSK"]


def test_item_level_chart_does_not_recommend_brand_sizes(workdir):
    directory = workdir / "size"
    directory.mkdir(parents=True)
    (directory / "ap.json").write_text(json.dumps({
        "brand": "ap", "display_name": "AP", "kind": "item_level",
        "chart_version": "test", "source": "官方商品页给实测尺寸",
        "fit": "正码", "note": "请指定具体商品", "categories": {}, "verified": True,
    }, ensure_ascii=False), encoding="utf-8")
    charts = size.load_charts(directory)

    with pytest.raises(size.ChartNotFound, match="商品级"):
        size.recommend(charts, "ap", "JSK", {"bust": 84})

    result = size.recommend_for_entry(charts, "ap", ["JSK"], {"bust": 84})
    assert result["status"] == "item_level_only"
