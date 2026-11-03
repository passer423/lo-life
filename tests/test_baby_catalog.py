"""Baby 官方商品目录 → 草稿的纯转换测试。

这一层不做网络、不写盘；钉住四件事：

* 官方字段（标题/价格/链接/图片/尺寸）原样带进草稿；
* 已存在的 handle、过旧商品、其他品牌必须跳过；
* 品类推断不猜风格；
* 草稿不会混进 `content/newarrivals`（由脚本层保证）。
"""

from __future__ import annotations

from datetime import date

import pytest

from app.domain import baby_catalog

TODAY = date(2026, 11, 3)

PRODUCT = {
    "id": 15468560515165,
    "title": "【予約】Dark Night Sweets柄 ジャンパースカートII",
    "handle": "p22oj208",
    "vendor": "ALICE and the PIRATES",
    "product_type": "Jumper Skirt",
    "published_at": "2026-10-30T00:00:00+09:00",
    "created_at": "2026-10-20T00:00:00+09:00",
    "tags": ["20261030_dark-night-sweets", "Dark Night Sweets", "pre-order"],
    "variants": [{"price": "41800.00", "title": "オフ白"}],
    "images": [{"src": "https://cdn.shopify.com/s/files/1/0000/p22oj208.jpg"}],
    "body_html": (
        "<p>Dark Night Sweets 柄。</p>"
        "<h2>サイズ</h2>"
        "<p>着丈：約98cm<br>バスト：約86～104cm<br>ウエスト：約74～92cm</p>"
    ),
}


def payload(*products: dict) -> dict:
    return {"products": list(products)}


def test_build_draft_uses_official_fields():
    draft, reason = baby_catalog.build_draft(PRODUCT, today=TODAY, lookback_days=30)
    assert reason == ""
    assert draft is not None

    assert draft["id"] == "2026-10-baby-p22oj208"
    assert draft["brand"] == "baby"
    assert draft["brand_name"] == "Baby"
    assert draft["series"] == "Dark Night Sweets柄 ジャンパースカートII"
    assert draft["release_date"] == "2026-10-30"
    assert draft["price_range"] == "41800"
    assert draft["currency"] == "JPY"
    assert draft["items"] == ["JSK"]
    assert draft["styles"] == []                     # 不猜风格
    assert draft["link"] == "https://store.babyssb.co.jp/products/p22oj208"
    assert draft["image"].startswith("https://cdn.shopify.com/")
    assert "products.json" in draft["source"]
    assert "2026-11-03" in draft["source"]
    assert "予約" in draft["note"]
    assert "PIRATES" in draft["note"]
    # 系列名已经在标题里，不需要重复写进 aliases
    assert "aliases" not in draft
    assert draft["size"]["bust"] == [86.0, 104.0]
    assert draft["size"]["waist"] == [74.0, 92.0]
    assert "p22oj208" in draft["size"]["source"]


def test_price_range_handles_multiple_variants():
    product = {**PRODUCT, "variants": [{"price": "41800"}, {"price": "57600"}]}
    draft, _reason = baby_catalog.build_draft(product, today=TODAY)
    assert draft["price_range"] == "41800 - 57600"


def test_other_vendor_is_skipped():
    draft, reason = baby_catalog.build_draft(
        {**PRODUCT, "vendor": "Innocent World"}, today=TODAY
    )
    assert draft is None
    assert reason == "other_vendor"


def test_old_product_is_skipped():
    draft, reason = baby_catalog.build_draft(
        {**PRODUCT, "published_at": "2026-01-01T00:00:00+09:00"},
        today=TODAY,
        lookback_days=30,
    )
    assert draft is None
    assert reason == "old"


def test_known_handle_is_not_drafted():
    drafts, _notes, counters = baby_catalog.scan_products(
        payload(PRODUCT),
        existing_handles={"p22oj208"},
        today=TODAY,
    )
    assert drafts == []
    assert counters["known"] == 1


def test_duplicate_handle_in_payload_is_counted():
    drafts, _notes, counters = baby_catalog.scan_products(
        payload(PRODUCT, PRODUCT),
        today=TODAY,
    )
    assert len(drafts) == 1
    assert counters["duplicate"] == 1


def test_limit_stops_and_explains():
    second = {**PRODUCT, "handle": "b50js252", "title": "Fallin' Ribbonジャンパースカート"}
    drafts, notes, _counters = baby_catalog.scan_products(
        payload(PRODUCT, second),
        today=TODAY,
        limit=1,
    )
    assert len(drafts) == 1
    assert any("上限" in note for note in notes)


@pytest.mark.parametrize(
    "title, product_type, expected",
    [
        ("ジャンパースカート", "", ["JSK"]),
        ("Jumper Skirt", "", ["JSK"]),
        ("ワンピース", "", ["OP"]),
        ("One-Piece Dress", "", ["OP"]),
        ("スカート", "", ["SK"]),
        ("カチューシャ", "", ["KC"]),
        ("ブラウス", "", ["BL"]),
        ("トートバッグ", "", []),
    ],
)
def test_infer_items(title, product_type, expected):
    assert baby_catalog.infer_items(title, product_type) == expected


def test_handle_from_link():
    assert baby_catalog.handle_from_link(
        "https://store.babyssb.co.jp/products/p22oj208?utm=1"
    ) == "p22oj208"
    assert baby_catalog.handle_from_link("https://example.com/item") == ""


def test_series_name_skips_internal_slug():
    assert baby_catalog.series_name(
        ["20261030_dark-night-sweets", "Dark Night Sweets", "pre-order"]
    ) == "Dark Night Sweets"


def test_scan_rejects_bad_payload():
    with pytest.raises(ValueError, match="products"):
        baby_catalog.scan_products({"items": []}, today=TODAY)
