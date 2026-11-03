"""店铺登记表：`brand` → 店铺页 / 品牌官网地址。

## 为什么需要这张表

一条上新 JSON 里的 `link` 和 `image` 都是**可选**的，而"这家店的页面在哪"其实是
**品牌级**的事实——它不随系列变化。把它抄进每一条上新等于两处维护同一份事实：
店名改一次要改几十个文件，迟早就漏一个（和 `brand_index` 不另建品牌表是同一个理由，
区别是这里的事实真的只有品牌级粒度，抄进条目才是错的那种）。

有了这张表，两件事才有地方落：

1. **取图的入口地址**（`page_url`）：`scripts/fetch_images.py` 拿它去抓候选图。
2. **链接兜底**：条目自己没写 `link` 时，用 `shop_url`——用户点「去店铺」总比
   什么都没有强，但**必须标清楚这是店铺链接，不是商品链接**，两者指向的东西不一样。

## 不自动填

这里只存手录的地址。抓取发生在离线脚本里，不在请求路径上——一个页面取不到图
不该让推荐页变慢或报错。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.domain import urls


@dataclass(frozen=True, slots=True)
class Shop:
    brand: str
    brand_name: str = ""
    shop_url: str = ""
    page_url: str = ""
    note: str = ""
    source: str = ""
    # 取图用：page_url 是什么形态，决定用哪种解析器。
    #   "shopify" —— 官方公开的商品 JSON（store.xxx/products.json），字段齐全，最可靠
    #   "html"    —— 普通页面，按 item_href_pattern 认出"哪条 <a> 是商品页"
    catalog: str = "html"
    # 商品详情页的 URL 特征。淘宝是 "item.htm"，AP 官网是 "ProductDetail.aspx"，
    # 用特征串而不是完整正则：配置越简单越不容易写错，而这里只需要"是不是商品页"。
    item_href_pattern: str = ""
    # 这家店的报价币种（ISO 代码）。取图时若条目没写价格，价格和币种会一起带上——
    # 只写数字不写币种，等于把一个 55000 的日元价冒充成人民币。
    currency: str = ""


def load_shops(directory: Path) -> tuple[dict[str, Shop], list[str]]:
    """读 `content/shops/*.json`。下划线开头是模板/说明，跳过。

    返回 ``(店铺表, 问题)``。单条坏数据**不让整页挂掉**：跳过它、收集问题、
    其余照常工作——和 `load_newarrivals` 同一条纪律。店铺表坏一份，不该导致
    推荐页空白。
    """
    shops: dict[str, Shop] = {}
    problems: list[str] = []
    if not directory.exists():
        return shops, problems

    for path in sorted(directory.glob("*.json")):
        if path.name.startswith("_"):
            continue
        try:
            raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            problems.append(f"{path.name}：不是合法 JSON（{exc}）")
            continue
        brand = str(raw.get("brand") or path.stem).strip()
        if not brand:
            problems.append(f"{path.name}：缺少 brand")
            continue
        shops[brand] = Shop(
            brand=brand,
            brand_name=str(raw.get("brand_name") or brand),
            # 出口校验：表里被塞进 javascript: 的链接不该变成可点的按钮
            shop_url=urls.safe_url(raw.get("shop_url") or ""),
            page_url=urls.safe_url(raw.get("page_url") or ""),
            note=str(raw.get("note") or ""),
            source=str(raw.get("source") or ""),
            catalog=str(raw.get("catalog") or "html").strip().lower() or "html",
            item_href_pattern=str(raw.get("item_href_pattern") or "").strip(),
            currency=str(raw.get("currency") or "").strip().upper(),
        )
    return shops, problems


def link_for(brand: str, entry_link: str, shops: dict[str, Shop]) -> tuple[str, str]:
    """返回 ``(链接, 类型)``。类型是 ``item``（条目自己的商品链接）、``shop``
    （退到店铺页）或空串（两者都没有）。

    顺序不可反：**条目自己的链接一定优先**。拿品牌首页顶掉用户录的商品详情页，
    会让"去商品"变成"去逛店"，比没有链接更浪费人一次点击。
    """
    own = urls.safe_url(entry_link)
    if own:
        return own, "item"
    shop = shops.get(brand)
    if shop is not None and shop.shop_url:
        return shop.shop_url, "shop"
    return "", ""
