"""商品级尺寸：解析 + 判断。

这一层比品牌尺码表更要紧——它直接来自官网商品页，是**这一件衣服**的实测值。
写错一个数字，用户就可能买回一件穿不下的裙子。

所以钉三件事：

* 只认带单位的数字（商品编号、年份、尺码字都不能被当成尺寸）；
* `ウエスト切替位置` 要归到 waist，且不能被 `ウエスト` 先吃掉半截；
* 判断用的是"身体 + 放量"，而且**不产出 S/M/L**——官网给的是弹性区间。
"""

from __future__ import annotations

import json

import pytest

from app.domain import garment
from app.domain import newarrivals as na
from app.domain import size

AP_LINE = "・着丈:約89㎝ ・バスト:約91～101㎝ ・ウエスト切替位置:約74～86㎝ ・身丈:BNP～約35cm"
BABY_LINE = "サイズ 着丈：約99～104cm バスト：約90～99cm ウエスト：約74～85cm"
OP_LINE = "バスト：91cm ウエスト：70cm 肩幅：34cm 着丈：93cm"


# --------------------------------------------------------------------- 解析


def test_parse_ap_ranges():
    assert garment.parse_measurements(AP_LINE) == {
        "length": [89.0, 89.0],
        "bust": [91.0, 101.0],
        "waist": [74.0, 86.0],
    }


def test_parse_baby_ranges():
    assert garment.parse_measurements(BABY_LINE) == {
        "length": [99.0, 104.0],
        "bust": [90.0, 99.0],
        "waist": [74.0, 85.0],
    }


def test_waist_seam_position_maps_to_waist_not_a_prefix():
    """`ウエスト切替位置` 必须整段吃掉，否则会解析出半截数字。"""
    parsed = garment.parse_measurements("ウエスト切替位置:約74～86㎝")
    assert parsed == {"waist": [74.0, 86.0]}


def test_single_values_and_shoulder():
    assert garment.parse_measurements(OP_LINE) == {
        "bust": [91.0, 91.0],
        "waist": [70.0, 70.0],
        "shoulder": [34.0, 34.0],
        "length": [93.0, 93.0],
    }


@pytest.mark.parametrize(
    "text",
    [
        "没有尺寸信息",
        "商品コード：262J10-020738",
        "バスト:91",              # 不带单位，不当尺寸
        "2026年10月31日",          # 日期不是尺寸
    ],
)
def test_parse_ignores_text_without_units(text):
    assert garment.parse_measurements(text) == {}


def test_summary_is_human_readable():
    parsed = garment.parse_measurements(AP_LINE)
    assert garment.summary(parsed) == "胸 91–101 / 腰 74–86 / 长 89"


def test_size_from_page_builds_item_level_size():
    size = garment.size_from_page(
        AP_LINE,
        source="AP 官网商品页（2026-11-02 取）",
        note="单一尺码；区间来自抽褶结构。",
    )
    assert size["kind"] == "free"
    assert size["label"] == "F"
    assert size["bust"] == [91.0, 101.0]
    assert size["source"] == "AP 官网商品页（2026-11-02 取）"
    assert "抽褶" in size["note"]


def test_size_from_page_returns_none_without_units():
    assert garment.size_from_page("商品编号 262J10", source="x") is None


# --------------------------------------------------------------------- 判断

ENTRY_SIZE = {
    "kind": "free",
    "label": "F",
    "bust": [91.0, 101.0],
    "waist": [74.0, 86.0],
    "length": [89.0, 89.0],
    "source": "AP 官网商品页",
}


def test_judge_says_fits_when_inside_the_elastic_range():
    result = size.judge_ranges(ENTRY_SIZE, {"bust": 84})   # 84 + 4 = 88 < 91 → 有余量
    assert result["status"] == "ok"
    assert result["verdict"] == "fits"
    assert result["recommended"] == "F"


def test_judge_says_too_small_when_body_exceeds_the_max():
    result = size.judge_ranges(ENTRY_SIZE, {"bust": 100})  # 100 + 4 = 104 > 101
    assert result["verdict"] == "too_small"
    assert result["shortfall"]["bust"] == 3.0
    assert result["recommended"] is None                   # 不硬推一个码


def test_judge_says_loose_when_far_below_the_minimum():
    result = size.judge_ranges(ENTRY_SIZE, {"bust": 70})   # 74 需要，成衣 91 起
    assert result["verdict"] == "loose"
    assert result["warnings"]


def test_judge_needs_at_least_one_measurement():
    result = size.judge_ranges(ENTRY_SIZE, {})
    assert result["status"] == "need_measurements"


def test_judge_without_ranges_is_no_chart():
    assert size.judge_ranges({"kind": "free", "label": "F"}, {"bust": 84})["status"] == "no_chart"


def test_recommend_for_entry_prefers_product_level_size(workdir):
    """有条目级尺寸时，不再去碰品牌表——哪怕那个牌子有表。"""
    charts = size.load_charts(workdir / "nonexistent")
    result = size.recommend_for_entry(
        charts, "ap", ["JSK"], {"bust": 84}, entry_size=ENTRY_SIZE
    )
    assert result["status"] == "ok"
    assert result["ranges"]["bust"] == [91.0, 101.0]


# --------------------------------------------------------------------- 数据流


def test_feed_row_carries_entry_size():
    entry = na.NewArrival(
        id="e1", brand="ap", brand_name="AP", series="白鸟笼",
        release_date="2026-10-20", items=["JSK"], size=ENTRY_SIZE,
    )
    row = na.feed([entry])[0]
    assert row["size"]["bust"] == [91.0, 101.0]


def test_loader_reads_entry_size(workdir):
    directory = workdir / "newarrivals"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "e1.json").write_text(
        json.dumps(
            {
                "id": "e1", "brand": "ap", "series": "X", "release_date": "2026-10-20",
                "size": {"kind": "free", "label": "F", "bust": [91, 101]},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    entries, problems = na.load_newarrivals(directory)
    assert problems == []
    assert entries[0].size["label"] == "F"


def test_endpoint_uses_product_size_when_present(client):
    """真实内容里每条都带 size：接口应当走区间判断，而不是去查品牌表。"""
    feed = client.get("/api/feed", params={"limit": 1}).json()
    entry_id = feed["items"][0]["id"]
    body = client.post(
        "/api/feed/sizes", json={"ids": [entry_id], "bust": 84, "waist": 66}
    ).json()
    result = body["sizes"][entry_id]
    assert result["status"] == "ok"
    assert "ranges" in result
    assert result["kind"] == "free"
