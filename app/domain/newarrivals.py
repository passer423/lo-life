"""上新推荐：加载 `content/newarrivals/*.json` 并排序。

## 排序只有两个因子，都是可解释的

1. **关注加权**：关注的品牌直接置顶。这是"推荐"里唯一真正个性化的部分。
2. **新鲜度**：30 天内线性衰减。

刻意**没有**做「猜你喜欢」：语料里只有上新条目，没有点击/购买行为，任何"智能排序"
都只能靠编。两个因子能保证——用户看得懂为什么这条排在前面。

## 数据来源

上新内容由**你自己维护**（`content/newarrivals/`），不从任何平台抓取。
每条带 `source` 字段记录依据（官方公告 / 店铺页面标题 / 自己记录）。
没有 `source` 的条目在界面上会被标为"未标注来源"——这是让人自己知道可信度。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from app.domain import images, price, shops as shop_table, urls
from app.domain.axes import AXES, entry_haystack

FRESH_DAYS = 7          # 这个天数以内标「新」
DECAY_DAYS = 30         # 新鲜度衰减到 0 的天数

# 排序方式。**刻意不做"相关度 × 偏好"的混合分数**：搜索是过滤（AND），偏好是
# 加权（排序），两者混成一个数字之后，用户既看不出为什么这条在前面，也看不出
# 为什么那条被筛掉了。它们是两个独立字段，前端用 chip 切换。
SORTS = ("score", "date", "price_asc", "price_desc")

# 偏好各轴的加权在 `axes.py` 里声明（那里也定义了匹配字段与"最多计几个"）。
# 这里只保留与内容分类相关的东西。

# 风格词表。放进代码而不是 content：它是**分类轴**，不是内容；
# 允许自由填写会让"甜系 / 甜美 / 甜"变成三个互不匹配的标签。
STYLE_OPTIONS = ("甜系", "古典", "哥特", "蒸汽朋克", "和风", "日常", "学院", "中华风")

REQUIRED = ("id", "brand", "series", "release_date")


class NewArrivalError(RuntimeError):
    """上新数据缺失或损坏。"""


@dataclass(slots=True)
class NewArrival:
    id: str
    brand: str
    brand_name: str
    series: str
    release_date: str
    price_range: str = ""
    # 币种。默认人民币；日本官网的条目（AP / Baby）是 JPY。
    # 单独一个字段，而不是把「日元」两个字塞进 price_range：塞进去之后，
    # 价格筛选、排序、区间解析都得先把那两个字剥掉，而且 `¥55000 日元` 读起来是重复的。
    currency: str = "CNY"
    items: list[str] = field(default_factory=list)
    styles: list[str] = field(default_factory=list)
    image: str = ""
    link: str = ""
    note: str = ""
    source: str = ""
    sample: bool = False
    # 这件衣服**自己**的成衣实测尺寸（官网商品页给的是范围，不是 S/M/L）。
    #   `{"kind": "free", "label": "F", "bust": [91, 101], "waist": [74, 86], ...}`
    # 为什么不塞进品牌尺码表：两家官网根本不按码给尺寸，硬拆成 S/M/L 只能靠编。
    size: dict[str, Any] = field(default_factory=dict)
    # 别名：同一个系列的另一种写法（日本官网的汉字/假名、英文名、旧译名）。
    # 取图时一起比对——官网标题写的是「白鳥籠」，条目写的是「白鸟笼」，
    # 没有别名就永远配不上，而那会被误当成"解析器坏了"。
    aliases: list[str] = field(default_factory=list)

    @property
    def released(self) -> date | None:
        try:
            return date.fromisoformat(self.release_date[:10])
        except (TypeError, ValueError):
            return None


def load_newarrivals(directory: Path) -> tuple[list[NewArrival], list[str]]:
    """返回 (条目, 数据问题)。下划线开头的文件是模板/说明，跳过。

    单条坏数据**不让整个页面挂掉**：收集成 problems 返回，界面上提示，
    其余条目照常展示。一个新填错的 JSON 不该让「上新」整页空白。
    """
    entries: list[NewArrival] = []
    problems: list[str] = []
    seen: dict[str, str] = {}

    if not directory.exists():
        return [], [f"没有上新目录：{directory}"]

    for path in sorted(directory.glob("*.json")):
        if path.name.startswith("_"):
            continue
        try:
            raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            problems.append(f"{path.name}：不是合法 JSON（{exc}）")
            continue

        missing = [key for key in REQUIRED if not str(raw.get(key) or "").strip()]
        if missing:
            problems.append(f"{path.name}：缺少字段 {'、'.join(missing)}")
            continue

        entry = NewArrival(
            id=str(raw["id"]),
            brand=str(raw["brand"]),
            brand_name=str(raw.get("brand_name") or raw["brand"]),
            series=str(raw["series"]),
            release_date=str(raw["release_date"])[:10],
            price_range=str(raw.get("price_range") or ""),
            currency=str(raw.get("currency") or "CNY").strip().upper() or "CNY",
            items=[str(item) for item in (raw.get("items") or [])],
            styles=[str(item) for item in (raw.get("styles") or []) if str(item) in STYLE_OPTIONS],
            image=str(raw.get("image") or ""),
            link=str(raw.get("link") or ""),
            note=str(raw.get("note") or ""),
            source=str(raw.get("source") or ""),
            sample=bool(raw.get("sample")),
            aliases=[str(alias) for alias in (raw.get("aliases") or []) if str(alias).strip()],
            size=raw.get("size") if isinstance(raw.get("size"), dict) else {},
        )
        if entry.released is None:
            problems.append(f"{path.name}：release_date 不是 YYYY-MM-DD（{entry.release_date}）")
            continue
        if entry.id in seen:
            problems.append(f"{path.name}：id 与 {seen[entry.id]} 重复（{entry.id}）")
            continue
        seen[entry.id] = path.name
        entries.append(entry)

    return entries, problems


def search_match(entry: NewArrival, query: str) -> list[str] | None:
    """按查询词过滤一条上新。返回命中的词；**没命中返回 ``None``**（便于调用方 continue）。

    语义是 **AND**：``"秋日 JSK"`` 要求两个词都在同一条上。OR 会让用户搜两个词时
    拿到一堆只沾一个的条目，看起来就是"搜索坏了"。

    匹配复用的是 ``axes.entry_haystack``——和「自定义标签」轴**同一份**文本范围
    （系列名 / 备注 / 品牌名 / 款式 / 风格）。两处各写一份，迟早会出现
    "偏好里能搜到、搜索框搜不到"这种没人能解释的差异。

    子串匹配，不用正则也不用分词：条目量在几百条级，且中文没有空格断词，
    引入分词器只会带来"这个词切法不对"的新一类 bug。
    """
    tokens = [token for token in str(query or "").split() if token]
    if not tokens:
        return []
    haystack = entry_haystack(entry)
    hits = [token for token in tokens if token.lower() in haystack]
    return hits if len(hits) == len(tokens) else None


def brand_index(entries: Iterable[NewArrival]) -> list[dict]:
    """从条目里反推品牌清单——不另建品牌表，避免两处维护同一份事实。"""
    stats: dict[str, dict] = {}
    for entry in entries:
        item = stats.setdefault(
            entry.brand,
            {"brand": entry.brand, "brand_name": entry.brand_name, "count": 0, "latest": ""},
        )
        item["count"] += 1
        if entry.release_date > item["latest"]:
            item["latest"] = entry.release_date
            item["brand_name"] = entry.brand_name
    return sorted(stats.values(), key=lambda row: (-row["count"], row["brand"]))


def feed(
    entries: list[NewArrival],
    *,
    preferences: dict[str, set[str]] | None = None,
    today: date | None = None,
    days: int | None = None,
    only_preferred: bool = False,
    limit: int = 50,
    q: str = "",
    sort: str = "score",
    price_min: float | None = None,
    price_max: float | None = None,
    shops: dict[str, shop_table.Shop] | None = None,
) -> list[dict]:
    """排序并序列化。

    ## 分数 = 新鲜度 + 每个偏好轴的匹配加分，每一项都写进 ``reasons``

    轴是**数据驱动**的（见 `axes.py`）：这里不存在"如果有风格偏好就…"这类分支，
    而是遍历轴表统一算分。加一个新轴不用改这个函数。

    用户必须能看懂为什么这条排在最前面。加权一旦不可解释，推荐就成了玄学；
    玄学排错一次，那一栏就没人再看了。

    ``only_preferred`` 为真时只看偏好的店家，此时加权已无区分意义，纯粹按时间排。

    ## 三个过滤维度各自独立，顺序是「先筛后排」

    ``q``（关键词）、``price_min/max``（价位）、``only_preferred`` / ``days``
    （时间与店家）都是**过滤**；``preferences`` 与 ``sort`` 只影响**顺序**。

    这个分界必须守住：如果把关键词命中也折算成加分，用户搜「秋日」时会看到
    一堆不含秋日的条目排在下面——他会认为搜索没生效，而不是"排序偏好不同"。
    """
    today = today or date.today()
    if sort not in SORTS:
        raise ValueError(f"不支持的排序方式：{sort}（可选 {'、'.join(SORTS)}）")

    # 保留顺序（用列表而不是集合）：自定义关键词的命中按用户勾选的先后回报，
    # 排序结果里显示的原因才稳定。
    prefs = {key: list(values) for key, values in (preferences or {}).items()}
    preferred_shops = prefs.get("shops") or []
    rows: list[dict] = []

    for entry in entries:
        released = entry.released
        if released is None:
            continue
        age = (today - released).days
        is_preferred_shop = entry.brand in preferred_shops
        if only_preferred and not is_preferred_shop:
            continue
        if days is not None and age > days:
            continue

        matched = search_match(entry, q)
        if matched is None:                 # 有关键词但没命中 → 直接筛掉
            continue

        bounds = price.parse_range(entry.price_range)
        if not price.within(bounds, price_min=price_min, price_max=price_max):
            continue

        if age < 0:
            # 发布日期在未来：可能是预录入。标出来，不当成"很新"
            freshness = 0.0
        else:
            freshness = max(0.0, (DECAY_DAYS - age) / DECAY_DAYS) * DECAY_DAYS

        score = freshness
        reasons: list[str] = []

        if not only_preferred:
            for axis in AXES:
                chosen = prefs.get(axis.key) or []
                if not chosen:
                    continue
                hits = axis.match(entry, chosen)
                if not hits:
                    continue
                score += axis.bonus * len(hits)
                reasons.append(axis.reason(hits))

        # 条目自己没写 link 时退到店铺首页（见 shops.link_for 的说明）
        link, link_kind = shop_table.link_for(entry.brand, entry.link, shops or {})

        rows.append(
            {
                "id": entry.id,
                "brand": entry.brand,
                "brand_name": entry.brand_name,
                "series": entry.series,
                "release_date": entry.release_date,
                "price_range": entry.price_range,
                # 前端据此在价格后面标「日元」——不靠品牌名猜币种
                "currency": entry.currency,
                # 商品级实测尺寸（可能是空字典）。卡片据此显示「胸 91–101」而不是编出来的码数
                "size": entry.size,
                # 解析出的区间只用于筛选/排序；展示仍用上面那个原始字符串，
                # 因为 "¥699 起" 的信息量比 "699.0" 大。
                "price_low": bounds[0] if bounds else None,
                "price_high": bounds[1] if bounds else None,
                "items": entry.items,
                "styles": entry.styles,
                # 出口收一次：手录的 link/image 可能是 javascript: 或 data: 协议
                "image": urls.safe_url(entry.image),
                "link": link,
                # item = 条目自己的商品链接；shop = 退到店铺首页。前端按钮文案要跟着变，
                # 把店铺页标成"去商品"会让人点进去发现不是那条裙子。
                "link_kind": link_kind,
                # 连店铺都没登记时的入口：淘宝搜索。**不是详情页**，所以按钮写「淘宝搜」
                # ——详情页地址只能来自真实数据（link 字段 / 列表页里的 item id），拼不出来。
                "search_url": images.search_url_for(entry.brand_name, entry.series),
                "note": entry.note,
                "source": entry.source,
                "sample": entry.sample,
                "preferred_shop": is_preferred_shop,
                "is_new": 0 <= age <= FRESH_DAYS,
                "age_days": age,
                "future": age < 0,
                "score": round(score, 1),
                "reasons": reasons,
                "matched": matched,
            }
        )

    if sort == "score":
        # 分数高的在前；同分时越新越靠前（age_days 升序）；再同则按 id 稳定排序
        rows.sort(key=lambda row: (-row["score"], row["age_days"], row["id"]))
    elif sort == "date":
        # 两次稳定排序：先按 id 定序，再按日期倒序——这样同一天的条目顺序可复现，
        # 而不是依赖 json 文件的遍历顺序（换个文件名就变了）。
        rows.sort(key=lambda row: row["id"])
        rows.sort(key=lambda row: row["release_date"], reverse=True)
    else:
        # 价格排序时**没价格的一律排最后**，不参与方向。混进数值序列会让它们
        # 在升序里冒充"最便宜"、在降序里冒充"最贵"。
        ascending = sort == "price_asc"
        rows.sort(key=lambda row: row["id"])
        rows.sort(
            key=lambda row: (
                row["price_low"] is None,
                row["price_low"] if ascending else -(row["price_high"] or 0.0),
            )
        )
    return rows[:limit]


def summary(
    entries: list[NewArrival],
    preferences: dict[str, list[str]] | None = None,
    *,
    today: date | None = None,
) -> dict:
    today = today or date.today()
    prefs = preferences or {}
    valid = [entry for entry in entries if entry.released is not None]
    latest = max((entry.release_date for entry in valid), default="")
    recent = sum(
        1 for entry in valid
        if 0 <= (today - entry.released).days <= FRESH_DAYS
    )
    return {
        "total": len(valid),
        "brands": len({entry.brand for entry in valid}),
        "recent": recent,
        "latest": latest,
        "sample_count": sum(1 for entry in valid if entry.sample),
        # 每个轴选中了几项，前端「我的」页直接用
        "preferences": {key: len(values or []) for key, values in prefs.items()},
    }
