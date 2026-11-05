"""Baby 官网商品目录 → 待确认的上新草稿。

`products.json` 是官方结构化数据；这里只做**纯转换**，不联网、不写盘。
真正的抓取、草稿落盘与人工确认都在 `scripts/scan_baby.py`。

口径：

* 只收 BABY / ALICE and the PIRATES 两条品牌线；
* 品类只从标题/商品类型推断，**不猜风格**；
* 价格、链接、图片、尺寸都来自官方字段，`source` 必须写清；
* 输出是草稿，必须人工 `--approve` 后才进 `content/newarrivals/`。
"""

from __future__ import annotations

import html as html_lib
import re
from collections.abc import Iterable
from datetime import date, timedelta
from typing import Any

from app.domain import garment

BABY_HOME = "https://store.babyssb.co.jp"
BABY_VENDORS = ("BABY, THE STARS SHINE BRIGHT", "ALICE and the PIRATES")

_SCRIPT_RE = re.compile(r"<(script|style|noscript)\b[^>]*>.*?</\1>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]{1,200}>")
_LEADING_TAGS = re.compile(r"^(?:【[^】]{1,16}】)+")
_PRODUCT_HANDLE = re.compile(r"/products/([^/?#]+)")
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")

# 品类词表。顺序关键：`ジャンパースカート` 必须先于 `スカート`，
# 否则 JSK 会被当成 SK。
_ITEM_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("JSK", ("ジャンパースカート", "jumper skirt", "jsk")),
    ("OP", ("ワンピース", "one-piece", "one piece", "op")),
    ("SK", ("スカート", "skirt")),
    ("KC", ("カチューシャ", "ヘッドドレス", "head dress", "kc")),
    ("BL", ("ブラウス", "blouse")),
)


def html_text(raw: str) -> str:
    """去脚本/样式与标签，保留换行。商品描述里的尺寸是纯文本。"""
    text = _SCRIPT_RE.sub("\n", raw or "")
    text = _TAG_RE.sub("\n", text)
    text = html_lib.unescape(text)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def vendor_label(vendor: str) -> str:
    """``-ALICE and the PIRATES-`` → ``ALICE and the PIRATES``。"""
    return str(vendor or "").strip().strip("-").strip()


def clean_title(raw: str) -> str:
    """去掉标题前的【予約】等状态标签；那些是状态，不是系列名。"""
    value = str(raw or "").strip()
    cleaned = _LEADING_TAGS.sub("", value).strip()
    return cleaned or value


def series_name(tags: Iterable[Any]) -> str:
    """从 tags 里取可读系列名；``20261030_dark-night-sweets`` 这类内部 slug 不算。"""
    for tag in tags or []:
        text = str(tag).strip()
        if not text or re.match(r"^\d{8}_", text):
            continue
        if re.fullmatch(r"[a-z0-9_\-]+", text.lower()):
            continue
        return text
    return ""


def price_range(variants: Iterable[Any]) -> str:
    """官方 variants 的价格 → `"41800"` / `"41800 - 57600"`。

    只取整数元；币种由条目的 `currency` 单独表达，不把「日元」塞进价格字符串。
    """
    amounts: set[int] = set()
    for variant in variants or []:
        if not isinstance(variant, dict):
            continue
        raw = str(variant.get("price") or "").strip()
        try:
            amount = int(float(raw))
        except (TypeError, ValueError):
            continue
        if amount > 0:
            amounts.add(amount)
    if not amounts:
        return ""
    low, high = min(amounts), max(amounts)
    return str(low) if low == high else f"{low} - {high}"


def infer_items(title: str, product_type: str = "") -> list[str]:
    """从标题/商品类型推断品类。认不出就返回空列表——不猜。"""
    haystack = f"{title} {product_type}".lower()
    for item, keys in _ITEM_PATTERNS:
        for key in keys:
            # 避免 "op" 命中 "top"/"shop" 这类英文子串
            if key in ("op", "sk", "kc", "bl") and not re.search(rf"(?<![a-z]){key}(?![a-z])", haystack):
                continue
            if key in haystack:
                return [item]
    return []


def handle_of_product(product: dict) -> str:
    return str(product.get("handle") or "").strip()


def handle_from_link(url: str) -> str:
    match = _PRODUCT_HANDLE.search(str(url or ""))
    return match.group(1) if match else ""


def product_date(product: dict) -> str:
    for key in ("published_at", "created_at"):
        match = _DATE_RE.search(str(product.get(key) or ""))
        if match:
            return match.group(1)
    return ""


def first_image(product: dict) -> str:
    for image in product.get("images") or []:
        if isinstance(image, dict):
            src = str(image.get("src") or "").strip()
            if src:
                return src
    return ""


def build_draft(
    product: Any,
    *,
    today: date,
    lookback_days: int = 30,
) -> tuple[dict | None, str]:
    """一条官方商品 → 一条草稿。返回 ``(draft, reason)``；失败时 draft 为 None。"""
    if not isinstance(product, dict):
        return None, "skipped"
    raw_title = str(product.get("title") or "").strip()
    if not raw_title:
        return None, "no_title"

    vendor = vendor_label(str(product.get("vendor") or ""))
    wanted = {name.upper() for name in BABY_VENDORS}
    if vendor and vendor.upper() not in wanted:
        return None, "other_vendor"

    handle = handle_of_product(product)
    if not handle:
        return None, "no_handle"

    published = product_date(product)
    if not published:
        return None, "no_date"
    try:
        released = date.fromisoformat(published)
    except ValueError:
        return None, "no_date"
    if released < today - timedelta(days=max(0, lookback_days)):
        return None, "old"

    title = clean_title(raw_title)
    link = f"{BABY_HOME}/products/{handle}"
    tags = [str(tag).strip() for tag in product.get("tags") or [] if str(tag).strip()]
    series_tag = series_name(tags)
    preorder = "予約" in raw_title or any("pre-order" in tag.lower() for tag in tags)
    description = html_text(product.get("body_html") or "")
    size = garment.size_from_page(
        description,
        source=f"Baby 官网商品 {link}（{today.isoformat()} 取）",
        note="单一尺码；区间来自商品页实测。",
    )

    note_parts = ["自动采集：标题/价格/链接取自 Baby 官网 products.json；发售日/风格/款式需人工确认。"]
    if preorder:
        note_parts.append("状态：予約（可预约）。")
    if "PIRATES" in vendor.upper():
        note_parts.append("品牌线：ALICE and the PIRATES。")
    if series_tag and series_tag.lower() not in title.lower():
        note_parts.append(f"系列：{series_tag}。")

    draft: dict = {
        "id": f"{published[:7]}-baby-{handle.lower()}",
        "brand": "baby",
        "brand_name": "Baby",
        "series": title,
        "release_date": published,
        "price_range": price_range(product.get("variants") or []),
        "currency": "JPY",
        "items": infer_items(title, str(product.get("product_type") or "")),
        "styles": [],
        "image": first_image(product),
        "link": link,
        "note": " ".join(note_parts),
        "source": f"依据：Baby 官网 products.json（{today.isoformat()} 取）",
        "sample": False,
    }
    if size:
        draft["size"] = size
    if series_tag and series_tag.lower() not in title.lower():
        draft["aliases"] = [series_tag]
    return draft, ""


def scan_products(
    payload: Any,
    *,
    existing_handles: Iterable[str] = (),
    today: date | None = None,
    lookback_days: int = 30,
    limit: int = 20,
) -> tuple[list[dict], list[str], dict[str, int]]:
    """把 ``products.json`` 的响应转成草稿列表。

    返回 ``(drafts, notes, counters)``。counters 让「为什么这次只生成 3 条」
    能在输出里回答，而不是靠猜。
    """
    today = today or date.today()
    products = payload.get("products") if isinstance(payload, dict) else None
    if not isinstance(products, list):
        raise ValueError("products.json 里没有 products 数组，官网结构可能变了")

    known = {str(handle).strip().lower() for handle in existing_handles if str(handle).strip()}
    seen: set[str] = set()
    counters = {
        "total": len(products),
        "known": 0,
        "old": 0,
        "other_vendor": 0,
        "no_title": 0,
        "no_handle": 0,
        "no_date": 0,
        "duplicate": 0,
        "skipped": 0,
    }
    drafts: list[dict] = []
    notes: list[str] = []

    for product in products:
        if limit > 0 and len(drafts) >= limit:
            notes.append(f"已达单轮上限 {limit} 条；新品在列表顶部，其余留到下一轮")
            break
        if not isinstance(product, dict):
            counters["skipped"] += 1
            continue

        handle = handle_of_product(product).lower()
        if handle and handle in known:
            counters["known"] += 1
            continue
        if handle and handle in seen:
            counters["duplicate"] += 1
            continue
        if handle:
            seen.add(handle)

        draft, reason = build_draft(product, today=today, lookback_days=lookback_days)
        if draft is None:
            counters[reason] = counters.get(reason, 0) + 1
            continue
        drafts.append(draft)

    return drafts, notes, counters
