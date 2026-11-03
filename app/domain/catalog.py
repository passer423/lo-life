"""Lolibrary 档案层：把档案记录匹配到上新条目。

只做**纯匹配**，不联网、不写盘。抓取在 `scripts/import_lolibrary.py`。

匹配顺序：

1. 商品编号（AP 的 `pid`、Baby 的 Shopify handle、Lolibrary 的 product number）；
2. 归一化标题（去空格/标点/大小写）；
3. 品牌 + 归一化标题。

匹配不到就返回 ``None``——**不猜**。档案层只补充"这件是什么"，绝不覆盖
当前价格、库存与购买链接。
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlparse

_WS = re.compile(r"[\s\u3000]+")
_PUNCT = re.compile(r"[\-–—_/・:：,，.。'\"“”]+")
_LEADING = re.compile(r"^(?:【[^】]{1,16}】)+")
_PRODUCT_HANDLE = re.compile(r"/products/([^/?#]+)")

COMPACT_FIELDS = (
    "slug",
    "url",
    "title",
    "brand",
    "category",
    "product_number",
    "release_year",
    "original_price",
    "archived_at",
    "image",
)


def normalize_product_number(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "").strip().upper())


def product_number_from_link(url: str) -> str:
    """从官方链接里取商品编号。

    AP: `...ProductDetail.aspx?pid=262J10-020738` → `262J10-020738`
    Baby: `.../products/p22oj208` → `P22OJ208`
    """
    parsed = urlparse(str(url or ""))
    query = parse_qs(parsed.query)
    for key in ("pid", "product_id", "id"):
        values = query.get(key) or []
        if values:
            return normalize_product_number(values[0])
    match = _PRODUCT_HANDLE.search(parsed.path)
    if match:
        return normalize_product_number(match.group(1))
    return ""


def normalize_title(value: str) -> str:
    """标题归一化：去状态前缀、大小写、空白和常见分隔标点。"""
    text = _LEADING.sub("", str(value or "").strip()).lower()
    text = _PUNCT.sub("", text)
    return _WS.sub("", text)


def _brand_title_key(record: dict) -> str:
    brand = str(record.get("brand") or "").strip().lower()
    return f"{brand}|{normalize_title(record.get('title') or '')}"


def build_index(records: Any) -> dict[str, Any]:
    """把档案记录建成索引。重复标题会被判为歧义，只保留商品编号匹配。"""
    by_number: dict[str, dict] = {}
    by_title: dict[str, dict] = {}
    by_brand_title: dict[str, dict] = {}
    by_slug: dict[str, dict] = {}
    title_counts: dict[str, int] = {}
    brand_title_counts: dict[str, int] = {}

    for record in records or []:
        if not isinstance(record, dict):
            continue
        slug = str(record.get("slug") or "").strip()
        number = normalize_product_number(record.get("product_number") or "")
        title = normalize_title(record.get("title") or "")
        brand_title = _brand_title_key(record)

        if slug:
            by_slug[slug] = record
        if number:
            by_number[number] = record
        if title:
            title_counts[title] = title_counts.get(title, 0) + 1
            by_title.setdefault(title, record)
        if title and brand_title != "|":
            brand_title_counts[brand_title] = brand_title_counts.get(brand_title, 0) + 1
            by_brand_title.setdefault(brand_title, record)

    # 同一标题对应多个条目时，标题匹配不可靠；只保留商品编号这条路。
    for title, count in title_counts.items():
        if count > 1:
            by_title.pop(title, None)
    for key, count in brand_title_counts.items():
        if count > 1:
            by_brand_title.pop(key, None)

    return {
        "number": by_number,
        "title": by_title,
        "brand_title": by_brand_title,
        "slug": by_slug,
        "count": len(by_number) or len(by_slug),
    }


def _compact(record: dict, matched_by: str) -> dict:
    item = {key: record.get(key, "") for key in COMPACT_FIELDS}
    item["matched_by"] = matched_by
    return item


def match_entry(entry: Any, index: dict | None) -> dict | None:
    """给一条上新找档案记录；找不到返回 None。"""
    if not index:
        return None

    number = product_number_from_link(str(getattr(entry, "link", "") or ""))
    if number:
        record = (index.get("number") or {}).get(number)
        if record is not None:
            return _compact(record, "product_number")

    title = normalize_title(str(getattr(entry, "series", "") or ""))
    if title:
        record = (index.get("title") or {}).get(title)
        if record is not None:
            return _compact(record, "title")
        brand = str(getattr(entry, "brand", "") or "").strip().lower()
        record = (index.get("brand_title") or {}).get(f"{brand}|{title}")
        if record is not None:
            return _compact(record, "brand_title")

    return None
