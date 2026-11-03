"""Lolibrary 档案层：匹配与读取。

这一层只回答"这条上新在档案馆里对应哪一条"，不碰当前价格/购买链接。
匹配失败必须返回 None，不能猜。
"""

from __future__ import annotations

import json

import pytest

from app.config import AppSettings
from app.domain import catalog as catalog_domain
from app.domain import newarrivals as na
from app.services import catalog_store


def record(**overrides) -> dict:
    base = {
        "slug": "baby-dark-night-sweets-2026",
        "url": "https://lolibrary.org/items/baby-dark-night-sweets-2026",
        "title": "Dark Night Sweets Jumper Skirt",
        "brand": "BABY, THE STARS SHINE BRIGHT",
        "category": "Jumper Skirt",
        "product_number": "P22OJ208",
        "release_year": "2026",
        "original_price": "¥41800 JPY",
        "archived_at": "2026-11-01",
        "image": "https://example.com/a.jpg",
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize(
    "url, expected",
    [
        (
            "https://angelicpretty.com/Form/Product/ProductDetail.aspx?shop=0&pid=262J10-020738",
            "262J10-020738",
        ),
        ("https://store.babyssb.co.jp/products/p22oj208", "P22OJ208"),
        ("https://example.com/item", ""),
    ],
)
def test_product_number_from_link(url, expected):
    assert catalog_domain.product_number_from_link(url) == expected


def test_normalize_title_removes_spaces_and_punctuation():
    assert catalog_domain.normalize_title("【予約】Dark Night Sweets柄 ジャンパースカートII") == (
        "darknightsweets柄ジャンパースカートii"
    )


def test_match_by_product_number():
    index = catalog_domain.build_index([record()])
    entry = na.NewArrival(
        id="e1", brand="baby", brand_name="Baby", series="别的名字",
        release_date="2026-10-30", link="https://store.babyssb.co.jp/products/p22oj208",
    )
    matched = catalog_domain.match_entry(entry, index)
    assert matched["product_number"] == "P22OJ208"
    assert matched["matched_by"] == "product_number"
    assert matched["original_price"] == "¥41800 JPY"


def test_match_by_title_when_number_missing():
    index = catalog_domain.build_index([record(product_number="")])
    entry = na.NewArrival(
        id="e1", brand="baby", brand_name="Baby",
        series="Dark Night Sweets  Jumper Skirt",
        release_date="2026-10-30", link="",
    )
    matched = catalog_domain.match_entry(entry, index)
    assert matched["matched_by"] == "title"


def test_duplicate_title_falls_back_to_number_only():
    index = catalog_domain.build_index([
        record(),
        record(slug="other", title="Dark Night Sweets Jumper Skirt", product_number="B50JS252"),
    ])
    entry = na.NewArrival(
        id="e1", brand="baby", brand_name="Baby",
        series="Dark Night Sweets Jumper Skirt",
        release_date="2026-10-30", link="",
    )
    assert catalog_domain.match_entry(entry, index) is None


def test_feed_row_attaches_catalog_without_overriding_price():
    index = catalog_domain.build_index([record()])
    entry = na.NewArrival(
        id="e1", brand="baby", brand_name="Baby", series="Dark Night Sweets",
        release_date="2026-10-30", price_range="41800", currency="JPY",
        link="https://store.babyssb.co.jp/products/p22oj208",
    )
    row = na.feed([entry], catalog=index)[0]
    assert row["price_range"] == "41800"            # 当前价格仍由条目决定
    assert row["catalog"]["release_year"] == "2026"
    assert row["catalog"]["matched_by"] == "product_number"


def test_catalog_store_loads_jsonl(workdir):
    catalog_store.clear_cache()
    data_dir = workdir / "data"
    path = data_dir / "lolibrary" / "catalog.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(record(), ensure_ascii=False) + "\n", encoding="utf-8")
    app = AppSettings(data_dir=data_dir, content_dir=workdir / "content")
    index = catalog_store.index(app)
    assert index["count"] == 1
    assert "P22OJ208" in index["number"]


def test_catalog_store_missing_file_is_empty(workdir):
    catalog_store.clear_cache()
    app = AppSettings(data_dir=workdir / "data", content_dir=workdir / "content")
    assert catalog_store.index(app) == {}
