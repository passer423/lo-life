"""上新推荐与偏好。

## 「关注」已经并进「偏好」

之前有两套开关（关注品牌 / 偏好的店），都表示"我喜欢这个牌子"。现在只有一套偏好，
由多个**轴**组成：喜欢的店家、喜欢的风格、常穿的款式。轴的定义在
`app/domain/axes.py`，是声明式的——加一个新轴不用改这里的任何逻辑。

## 接口分层

* `GET /api/preferences/options` **不需要登录**：前端要先能把标签组渲染出来，
  才谈得上让人注册去保存。返回的是**元数据**（轴的 key / 标题 / 说明 / 权重）
  加上每个轴的可选标签，所以前端渲染标签组时不需要知道有哪些轴。
* `GET /api/preferences` / `PUT /api/preferences`：读写的都是按轴的字典。
* `PUT` **只替换传进来的轴**，没传的保持不变——避免"少传一个字段就清空其他偏好"。
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app import auth, bridge, db
from app.config import settings
from app.domain import newarrivals as na
from app.domain import preferences as pref
from app.domain import recommend as rec
from app.domain import shops as shop_table
from app.domain import size
from app.domain.axes import AXES

router = APIRouter(prefix="/api", tags=["feed"])

MEASURE_KEYS = ("bust", "waist", "hip", "shoulder")


class PreferencesIn(BaseModel):
    """按轴的偏好。所有轴都是可选的；只替换传进来的那些。"""

    shops: list[str] | None = Field(None, description="喜欢的店家（brand id）")
    styles: list[str] | None = Field(None, description="喜欢的风格")
    keywords: list[str] | None = Field(None, description="自定义标签（自由输入，按关键词匹配）")
    categories: list[str] | None = Field(None, description="常穿的款式")


def _load() -> tuple[list[na.NewArrival], list[str]]:
    return na.load_newarrivals(settings.newarrivals_dir)


def _size_charts(entries: list[na.NewArrival]) -> tuple[dict, list[str]]:
    """每个品牌的「尺码能说到什么程度」，给卡片直接显示可选码与版型。

    读表失败**不让推荐页挂掉**：尺码表是内容，内容写错不该把上新列表一起拖下去。
    返回 ``(摘要, 问题)``，问题交给前端用同一条 problems 提示显示。
    """
    try:
        charts = size.load_charts(settings.size_dir)
    except ValueError as exc:
        return {}, [f"尺码表读取失败：{exc}"]

    summary: dict[str, dict] = {}
    for brand in sorted({entry.brand for entry in entries}):
        chart = charts.get(brand)
        if chart is None:
            continue
        summary[brand] = {
            "display_name": chart.display_name,
            "fit": chart.fit,
            "kind": chart.kind,
            "note": chart.note,
            # 没核对过的表，卡片上只能显示"尺码表未核对"，不能把数字当事实
            "verified": chart.verified,
            # 每个品类有哪些码——前端据此在卡片上直接标「可选 S/M/L/XL」
            "categories": {
                name: size.sizes_of(chart, name) for name in sorted(chart.categories)
            },
            "source": chart.source,
        }
    return summary, []


def _prefs_of(user: dict | None) -> dict[str, list[str]]:
    if user is None:
        return {axis.key: [] for axis in AXES}
    return db.get_preferences(settings.db_path, int(user["id"]))


# --------------------------------------------------------------------- 推荐


@router.get("/feed")
def get_feed(
    days: int | None = Query(None, ge=1, le=365, description="只看最近 N 天"),
    limit: int = Query(50, ge=1, le=200),
    only_preferred: bool = Query(False, description="只看偏好的店家"),
    q: str = Query("", max_length=60, description="关键词；空白分隔，多个词是 AND"),
    sort: Literal["score", "date", "price_asc", "price_desc"] = Query(
        "score", description="score=按偏好+新鲜度；date=按发售日；price_*=按价格"
    ),
    price_min: float | None = Query(None, ge=0, le=100000),
    price_max: float | None = Query(None, ge=0, le=100000),
    user: dict | None = Depends(auth.optional_user),
) -> dict:
    if price_min is not None and price_max is not None and price_min > price_max:
        raise HTTPException(status_code=400, detail="price_min 不能大于 price_max")

    entries, problems = _load()
    preferences = _prefs_of(user)
    options = pref.build_options(entries)
    charts, chart_problems = _size_charts(entries)
    shops, shop_problems = shop_table.load_shops(settings.shops_dir)

    items = na.feed(
        entries,
        preferences=preferences,
        days=days,
        only_preferred=only_preferred,
        limit=limit,
        q=q,
        sort=sort,
        price_min=price_min,
        price_max=price_max,
        shops=shops,
    )
    return {
        "items": items,
        "count": len(items),
        "summary": na.summary(entries, preferences),
        "preferences": preferences,
        # 把偏好翻成"人话"+ 标签显示名，前端不用再查一遍选项
        "described": pref.describe(preferences, options),
        "logged_in": user is not None,
        # 回显查询条件：前端刷新后要能复原搜索框和 chip，而不是静默回到默认
        "query": q,
        "sort": sort,
        "filters": {"days": days, "only_preferred": only_preferred,
                    "price_min": price_min, "price_max": price_max},
        # 品牌 → 可选码/版型。卡片只按 brand 查这一张表，不重复传输
        "size_charts": charts,
        # 品牌 → 店铺地址。前端用它渲染「去店铺」（条目自己没有 link 时），
        # 以及「取图」按钮要不要用已登记的 page_url 直接开跑。
        "shops": {
            brand: {
                "shop_url": shop.shop_url,
                "page_url": shop.page_url,
                "brand_name": shop.brand_name,
            }
            for brand, shop in shops.items()
        },
        "problems": problems + chart_problems + shop_problems,
        # 示例数据必须在界面上说清，否则用户会把演示数据当真上新
        "has_sample_data": any(entry.sample for entry in entries),
    }


class FeedSizesIn(BaseModel):
    """给一批上新条目算「你的码」。

    三围走 **POST body 而不是 query string**，有两个具体理由：GET 的 URL 会进
    浏览器历史和访问日志，而三围是这个应用里最私人的几个数字；另外 URL 长度
    在条目多时会顶到上限。

    **不落库。** 这里只用一次、返回完就丢——要持久化是另一个决定（见 README
    「尺码能不能记住」），不该藏在"加个搜索框"里顺手做掉。
    """

    ids: list[str] = Field(default_factory=list, max_length=200, description="上新条目 id")
    bust: float | None = Field(None, gt=0, le=200, description="胸围 cm")
    waist: float | None = Field(None, gt=0, le=200, description="腰围 cm")
    hip: float | None = Field(None, gt=0, le=200, description="臀围 cm")
    shoulder: float | None = Field(None, gt=0, le=100, description="肩宽 cm")


@router.post("/feed/sizes")
def feed_sizes(payload: FeedSizesIn) -> dict:
    """批量把「这批上新」翻译成「你的码」。

    三档结果各自分得清，谁也不冒充谁：没尺码表 / 品类不在表里 / 缺尺寸。
    前端把它们显示成三种不同的灰字，而不是统一一句"暂无"。
    """
    measurements = {
        key: getattr(payload, key)
        for key in MEASURE_KEYS
        if getattr(payload, key) is not None
    }
    if not measurements:
        raise HTTPException(status_code=400, detail="至少要给一项身体尺寸（胸围/腰围/臀围/肩宽）")

    try:
        charts = size.load_charts(settings.size_dir)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    entries, _ = _load()
    by_id = {entry.id: entry for entry in entries}
    wanted = list(dict.fromkeys(payload.ids))       # 去重但保留顺序

    results: dict[str, dict] = {}
    for entry_id in wanted:
        entry = by_id.get(entry_id)
        if entry is None:
            results[entry_id] = {"status": "unknown_id"}
            continue
        results[entry_id] = size.recommend_for_entry(
            charts, entry.brand, entry.items, measurements, entry_size=entry.size
        )

    return {
        "sizes": results,
        "count": len(results),
        "measured": measurements,
        "note": "尺寸只用于这次计算，服务端不保存。",
    }


# --------------------------------------------------------------------- 偏好


@router.get("/preferences/options")
def preference_options() -> dict:
    """轴的元数据 + 每个轴的可选标签。前端据此渲染标签组，不需要硬编码任何轴。"""
    entries, problems = _load()
    return {
        "axes": [
            {
                "key": axis.key,
                "label": axis.label,
                "hint": axis.hint,
                "bonus": axis.bonus,
                "max_matches": axis.max_matches,
                # 前端据此决定这一组是"从选项里挑"还是"让人自由输入"
                "allow_custom": axis.allow_custom,
            }
            for axis in AXES
        ],
        "options": pref.build_options(entries),
        "problems": problems,
    }


@router.get("/preferences")
def get_preferences(user: dict | None = Depends(auth.optional_user)) -> dict:
    entries, _ = _load()
    preferences = _prefs_of(user)
    return {
        "logged_in": user is not None,
        "preferences": preferences,
        "described": pref.describe(preferences, pref.build_options(entries)),
        # 自定义标签的命中回报：告诉用户哪些词在当前内容里找不到，
        # 否则他会以为"我写的标签没生效"
        "match_report": pref.match_report(entries, preferences),
        "updated_at": db.preferences_updated_at(settings.db_path, int(user["id"])) if user else "",
    }


@router.put("/preferences")
def put_preferences(payload: PreferencesIn, user: dict = Depends(auth.require_user)) -> dict:
    entries, _ = _load()
    options = pref.build_options(entries)

    submitted = {
        key: value
        for key, value in payload.model_dump().items()
        if value is not None          # 没传的轴不动
    }
    cleaned, error = pref.validate(submitted, options)
    if error:
        raise HTTPException(status_code=400, detail=error)

    saved = db.save_preferences(settings.db_path, int(user["id"]), cleaned)
    return {
        "logged_in": True,
        "preferences": saved,
        "described": pref.describe(saved, options),
        "match_report": pref.match_report(entries, saved),
        "updated_at": db.preferences_updated_at(settings.db_path, int(user["id"])),
    }


# --------------------------------------------------------------------- 专属推荐


@router.get("/recommendations")
def get_recommendations(
    limit: int = Query(10, ge=1, le=200),
    sort: Literal["date", "score"] = Query(
        "date", description="date=按上新日期倒序（默认）；score=按偏好加权"
    ),
    user: dict = Depends(auth.require_user),
) -> dict:
    """按**你真实的偏好 tag** 筛出该看的上新，并给每条挂一条相关知识。

    ## 三段，各干各的

    1. **召回**：偏好 tag 与条目求交（`app/domain/recommend.py`，纯函数）。
    2. **排序**：复用 `feed()` 的加权（店家 +100 / 风格 +40 / 类别 +25）。
    3. **背景**：拿条目的**品类**（JSK / OP）去知识库检索，命中就挂一条原文标题。

    第 3 段是 RAG 在这里唯一站得住脚的位置。**它不参与召回**：
    知识库里只有术语、尺码、洗护、指南，拿「甜系」「baby」去检索实测命中 0 条——
    让它决定推荐，等于让一个没见过商品的语料库来决定买哪条裙子。

    知识库没建、或检索失败时，这条请求**照样 200**，只是 `knowledge` 为 null：
    推荐本身是确定性计算，不该被另一个子系统的状态拖下水。
    """
    entries, problems = _load()
    preferences = _prefs_of(user)
    options = pref.build_options(entries)

    if not any(preferences.values()):
        return {
            "items": [],
            "groups": [],
            "count": 0,
            "logged_in": True,
            "empty_reason": "还没有设置偏好。去「偏好」页勾几个标签，这里就会按你的口味推荐。",
            "described": [],
            "problems": problems,
        }

    # 先按 200 条取全量再筛、再截断：反过来先截断会把"命中偏好但排在 21 名之后"
    # 的条目直接丢掉，而它们恰恰可能是用户要看的。
    ranked = na.feed(entries, preferences=preferences, sort=sort, limit=200)
    by_id = {entry.id: entry for entry in entries}

    items: list[dict] = []
    pairs: list[tuple[str, list]] = []
    for row in ranked:
        entry = by_id.get(row["id"])
        if entry is None:
            continue
        tags = rec.matches_of(entry, preferences)
        if not tags:                     # 没命中任何偏好就不算"专属推荐"
            continue
        row["matches"] = [tag.as_dict() for tag in tags]
        items.append(row)
        pairs.append((entry.id, tags))
        if len(items) >= limit:
            break

    kb_ready = True
    for row in items:
        entry = by_id[row["id"]]
        row["knowledge"] = _knowledge_for(entry)
        if row["knowledge"] is None:
            kb_ready = False

    shops, shop_problems = shop_table.load_shops(settings.shops_dir)
    charts, chart_problems = _size_charts(entries)

    return {
        "items": items,
        "groups": [group.as_dict() for group in rec.group_by_axis(pairs)],
        "count": len(items),
        "limit": limit,
        "sort": sort,
        "logged_in": True,
        "kb_ready": kb_ready,
        "described": pref.describe(preferences, options),
        # 自带这两张表：推荐页在"只看推荐"模式下不再额外请求 /api/feed，
        # 否则卡片上的尺码徽标和「取图」按钮会因为缺数据而消失。
        "size_charts": charts,
        "shops": {
            brand: {
                "shop_url": shop.shop_url,
                "page_url": shop.page_url,
                "brand_name": shop.brand_name,
            }
            for brand, shop in shops.items()
        },
        "has_sample_data": any(entry.sample for entry in entries),
        "problems": problems + chart_problems + shop_problems,
    }


def _knowledge_for(entry) -> dict | None:
    """这条上新在知识库里有没有一段背景可挂。查不到就返回 None，不编。"""
    query = rec.knowledge_query(entry)
    if not query:
        return None
    try:
        hits = bridge.search_kb(query, settings, top_k=1)
    except Exception:                    # noqa: BLE001 - 索引没建 / 上游不可用都不该拖垮推荐
        return None
    if not hits:
        return None
    hit = hits[0]
    text = str(hit.get("text") or "")
    return {
        "post_id": hit.get("post_id", ""),
        # 知识库条目的正文第一行就是标题（`ingest_kb.py` 把 title 并进了 text）
        "title": text.split("\n", 1)[0][:60],
        "score": hit.get("score", 0.0),
        "query": query,
    }
