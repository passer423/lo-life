"""agent 的工具表：它能碰到的世界只有这些。

三条设计约束：

1. **这一版全部只读。** 写操作（录内容、改偏好）必须走"预览 + 人工确认"，那是下一步的事。
   先把只读闭环做扎实——写工具的破坏力不可逆，不值得为了"看起来功能多"提前放进来。
2. **参数用 pydantic 校验。** 非法参数变成一句可读的错误回给模型（它有机会改），
   而不是抛到 HTTP 层变成 500。
3. **返回值必须短。** 每个工具把结果压成 ``summary``（一句话结论）+ ``evidence``
   （可引用的条目）。原始数据不往上下文里灌——上下文有限，灌进去就是给自己制造噪声。

每个 handler 都复用现成的 domain/bridge 函数，不重写任何检索或计算逻辑。
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from pydantic import BaseModel, Field, ValidationError

from app import bridge, db
from app.config import AppSettings
from app.config import settings as default_settings
from app.domain import body as body_mod
from app.domain import care, garment, size
from app.domain import newarrivals as na
from app.domain import shops as shop_table
from app.services import catalog_store

logger = logging.getLogger(__name__)

EVIDENCE_TEXT_LIMIT = 300      # 单条证据最多留多少字
SUMMARY_ITEMS = 3              # summary 里最多点名几条，太长模型会觉得"已经答完了"


class ToolError(RuntimeError):
    """工具执行失败。会被翻译成 observation 回给模型，不抛到 HTTP 层。"""


@dataclass(slots=True)
class Context:
    """一次对话的上下文。偏好是**按用户**的，所以工具需要知道是谁在问。"""

    user: dict | None = None
    # AppSettings 是可变对象，不能直接当 dataclass 默认值（会抛 ValueError）
    settings: AppSettings = field(default_factory=lambda: default_settings)

    @property
    def preferences(self) -> dict[str, list[str]]:
        if self.user is None:
            return {}
        return db.get_preferences(self.settings.db_path, int(self.user["id"]))

    @property
    def body(self) -> dict:
        """用户存下来的身材参数。没有登录或没填时是空 dict。

        **只在服务端读**：模型不需要（也不应该）在对话里反复索要三围，
        工具直接把存好的数字填进尺码表。
        """
        if self.user is None:
            return {}
        return db.get_body(self.settings.db_path, int(self.user["id"]))


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]
    handler: Callable[[BaseModel, Context], dict]

    def schema(self) -> dict:
        """给 planner 看的 JSON Schema。**由 pydantic 生成**，不手写第二份。"""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.args_model.model_json_schema(),
        }

    def run(self, raw, ctx: Context) -> dict:
        if not isinstance(raw, dict):
            raise ToolError("参数必须是一个 JSON 对象")
        try:
            args = self.args_model(**raw)
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(part) for part in first["loc"]) or "参数"
            raise ToolError(f"{where} 不合法：{first['msg']}") from exc

        try:
            return self.handler(args, ctx)
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001
            # 工具的意外异常绝不能漏到 HTTP 层，也不能让整个 run 崩掉：
            # 包成可读的失败结果回给模型，它还有机会换个工具或收手。
            logger.warning("工具 %s 执行失败：%s: %s", self.name, type(exc).__name__, exc)
            raise ToolError(f"{self.name} 执行失败：{type(exc).__name__}: {exc}") from exc


def _clip(text: str) -> str:
    text = (text or "").strip()
    return text if len(text) <= EVIDENCE_TEXT_LIMIT else text[:EVIDENCE_TEXT_LIMIT] + "…"


# --------------------------------------------------------------------- 上新


class SearchFeedArgs(BaseModel):
    q: str = Field("", max_length=60, description="关键词，空白分隔，多个词是 AND")
    styles: list[str] = Field(default_factory=list, description="风格，如 甜系/古典/哥特")
    categories: list[str] = Field(default_factory=list, description="款式，如 JSK/OP/SK")
    price_min: float | None = Field(None, ge=0, le=100000)
    price_max: float | None = Field(None, ge=0, le=100000)
    days: int | None = Field(None, ge=1, le=365, description="只看最近 N 天")
    limit: int = Field(5, ge=1, le=20)


def _entry_text(item: dict) -> str:
    parts = [
        f"{item.get('brand_name') or item.get('brand')} {item.get('series')}",
        f"发售 {item.get('release_date')}",
    ]
    if item.get("future"):
        parts.append("尚未发售")
    if item.get("price_range"):
        amount = f"价位 {item['price_range']}"
        currency = str(item.get("currency") or "")
        if currency and currency != "CNY":
            amount += f" {currency}"
        parts.append(amount)
    if item.get("styles"):
        parts.append("风格 " + "/".join(item["styles"]))
    if item.get("items"):
        parts.append("款式 " + "/".join(item["items"]))
    size_text = garment.summary(item.get("size") or {})
    if size_text:
        parts.append("尺码 " + size_text)
    if item.get("note"):
        parts.append(item["note"])
    if item.get("source"):
        parts.append("来源 " + str(item["source"])[:90])
    if item.get("sample"):
        parts.append("示例数据")
    catalog = item.get("catalog")
    if catalog:
        bits = ["档案 Lolibrary"]
        if catalog.get("release_year"):
            bits.append(f"发售年份 {catalog['release_year']}")
        if catalog.get("original_price"):
            bits.append(f"历史原价 {catalog['original_price']}")
        if catalog.get("archived_at"):
            bits.append(f"收录 {catalog['archived_at']}")
        if catalog.get("url"):
            bits.append(str(catalog["url"]))
        parts.append("｜".join(bits))
    return _clip("｜".join(parts))


def _search_feed(args: SearchFeedArgs, ctx: Context) -> dict:
    entries, _problems = na.load_newarrivals(ctx.settings.newarrivals_dir)
    if not entries:
        return {"ok": True, "empty": True, "summary": "内容里一条上新都没有。", "evidence": []}

    shops, _ = shop_table.load_shops(ctx.settings.shops_dir)
    catalog_index = catalog_store.index(ctx.settings)
    # 先取宽一点再按风格/款式二次筛选，否则 limit 会在筛选之前就把候选砍掉
    items = na.feed(
        entries,
        preferences=ctx.preferences,
        days=args.days,
        limit=200,
        q=args.q,
        sort="score",
        price_min=args.price_min,
        price_max=args.price_max,
        shops=shops,
        catalog=catalog_index,
    )
    if args.styles:
        items = [item for item in items if set(args.styles) & set(item.get("styles") or [])]
    if args.categories:
        items = [item for item in items if set(args.categories) & set(item.get("items") or [])]
    items = items[: args.limit]

    if not items:
        return {
            "ok": True,
            "empty": True,
            "summary": (
                "没有匹配的上新（价格/风格/款式/关键词任一条件都可能过严）。"
                "如果用户问的是洗护/术语而不是找商品，改用 care_advice 或 search_kb，"
                "不要直接拒答。"
            ),
            "evidence": [],
            "filters": args.model_dump(),
        }

    named = "；".join(
        f"{item['brand_name']} {item['series']}（{item['release_date']}"
        + (f"，{item['price_range']}" if item.get("price_range") else "")
        + "）"
        for item in items[:SUMMARY_ITEMS]
    )
    return {
        "ok": True,
        "empty": False,
        "summary": f"命中 {len(items)} 条：{named}" + ("…" if len(items) > SUMMARY_ITEMS else ""),
        "evidence": [
            {"ref": item["id"], "title": f"{item['brand_name']} {item['series']}", "text": _entry_text(item)}
            for item in items
        ],
        "filters": args.model_dump(),
    }


# --------------------------------------------------------------------- 知识库


class SearchKbArgs(BaseModel):
    question: str = Field(..., min_length=1, max_length=120, description="要查的问题或术语")
    top_k: int = Field(3, ge=1, le=6)


def _search_kb(args: SearchKbArgs, ctx: Context) -> dict:
    hits = bridge.search_kb(args.question, ctx.settings, top_k=args.top_k)
    if not hits:
        return {
            "ok": True,
            "empty": True,
            "summary": "知识库里没有相关条目（这本身是有效结论，不要换个说法反复查）。",
            "evidence": [],
        }
    top = hits[0]
    return {
        "ok": True,
        "empty": False,
        "summary": f"知识库命中 {len(hits)} 条，最相关的是「{_clip(top['text'])[:24]}…」（相关度 {top['score']:.3f}）",
        "evidence": [
            {
                "ref": hit["chunk_id"],
                "title": " ".join(part for part in (hit.get("author"), hit.get("created_at")) if part) or "知识库",
                "text": _clip(hit["text"]),
            }
            for hit in hits
        ],
    }


# --------------------------------------------------------------------- 洗护


class CareArgs(BaseModel):
    materials: list[str] = Field(default_factory=list, description="面料，如 蕾丝/真丝/雪纺")
    crafts: list[str] = Field(default_factory=list, description="工艺，如 烫金/亮片/印花")
    notes: list[str] = Field(
        default_factory=list,
        description=(
            "情况，优先用标准标签：只能干洗 / 有钢圈裙撑 / 深色易掉色 / "
            "红色/酒红易掉色 / 白色/米白易泛黄 / 彩色/拼色易串色 / 首次洗"
        ),
    )


def _care_advice(args: CareArgs, ctx: Context) -> dict:
    if not (args.materials or args.crafts or args.notes):
        raise ToolError("至少要给一项面料、工艺或情况")
    rules, _options = care.load_rules(ctx.settings.care_dir)
    try:
        result = care.advise(rules, materials=args.materials, crafts=args.crafts, notes=args.notes)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc

    if not result.get("known"):
        return {"ok": True, "empty": True, "summary": result["headline"] + "：" + " ".join(result["advice"]), "evidence": []}

    text = _clip("；".join(result["advice"]))
    return {
        "ok": True,
        "empty": False,
        "summary": result["headline"],
        "evidence": [{"ref": ",".join(result.get("refs") or []) or "care", "title": result["headline"], "text": text}],
    }


# --------------------------------------------------------------------- 尺码


class SizeArgs(BaseModel):
    brand: str = Field(..., min_length=1, max_length=60, description="品牌 id，如 baby / ap")
    category: str = Field("JSK", max_length=20, description="JSK / OP / SK")
    bust: float | None = Field(None, gt=0, le=200, description="胸围 cm")
    waist: float | None = Field(None, gt=0, le=200, description="腰围 cm")
    hip: float | None = Field(None, gt=0, le=200, description="臀围 cm")
    shoulder: float | None = Field(None, gt=0, le=100, description="肩宽 cm")


def _size_recommend(args: SizeArgs, ctx: Context) -> dict:
    brand = args.brand.strip().lower()
    category = args.category.strip().upper()
    measurements = {
        key: getattr(args, key)
        for key in body_mod.CIRCUMFERENCE
        if getattr(args, key) is not None
    }
    if not measurements:
        # 参数里没给，就用存下来的身材——用户不该每次重新报三围
        stored = ctx.body or {}
        measurements = {dim: stored[dim] for dim in body_mod.CIRCUMFERENCE if stored.get(dim)}
    charts = size.load_charts(ctx.settings.size_dir)
    chart = charts.get(brand)
    try:
        result = size.recommend(charts, brand, category, measurements)
    except size.ChartNotFound as exc:
        # 没有这个牌子/品类的表 —— 这是"我们没有数据"，不是错误。
        # 给一条结论证据，agent 才能回答「AP 官方不提供分码表」而不是「没查到」。
        text = str(exc)
        return {
            "ok": True,
            "empty": False,
            "status": "no_data",
            "summary": text,
            "evidence": [{"ref": f"{brand}:{category}", "title": "没有品牌级尺码表", "text": _clip(text)}],
        }
    except ValueError as exc:
        # 缺三围是"需要用户补数据"，不是工具坏了；同样要可回答。
        text = f"{exc}。先到「我的 → 身材参数」补上，或直接给出三围。"
        return {
            "ok": True,
            "empty": False,
            "status": "need_measurements",
            "summary": text,
            "evidence": [{"ref": f"{brand}:{category}", "title": "还缺身材数据", "text": _clip(text)}],
        }

    verified = bool(chart and chart.verified)
    source = str(chart.source) if chart is not None else ""
    if not result["recommended"]:
        summary = f"{result['display_name']} {category} 没有合适的码：" + " ".join(result["warnings"])
    else:
        summary = (
            f"{result['display_name']} {category} 推荐 {result['recommended']} 码"
            f"（置信度 {result['confidence']}，版型{result['fit']}）"
        )
    if not verified:
        summary += "（尺码表未核对，仅供参考）"
    advice = "；".join(result["advice"] + result["warnings"])
    if source:
        advice += f"；来源：{source}"
    return {
        "ok": True,
        "empty": False,
        "summary": summary,
        "evidence": [{"ref": f"{brand}:{category}", "title": summary, "text": _clip(advice)}],
        "recommended": result["recommended"],
        "verified": verified,
    }


# --------------------------------------------------------------------- 合身判断


class CheckFitArgs(BaseModel):
    entry: str = Field(
        "", max_length=60,
        description="上新条目 id 或系列名/别名关键词，如 huayuzhenzhuxia-duandai、缎带与齿轮",
    )
    brand: str = Field("", max_length=60, description="品牌 id；没有具体条目时用")
    category: str = Field("", max_length=20, description="品类，如 JSK / OP / SK")
    bust: float | None = Field(None, gt=0, le=200, description="临时覆盖存下来的胸围")
    waist: float | None = Field(None, gt=0, le=200)
    hip: float | None = Field(None, gt=0, le=200)
    shoulder: float | None = Field(None, gt=0, le=100)


def _find_entry(entries, needle: str):
    needle = (needle or "").strip().lower()
    if not needle:
        return None, []
    for entry in entries:
        if entry.id.lower() == needle:
            return entry, []
    hits = [
        entry
        for entry in entries
        if needle in entry.series.lower()
        or needle in (entry.brand_name or "").lower()
        or needle in entry.brand.lower()
        or any(needle in alias.lower() for alias in (getattr(entry, "aliases", None) or ()))
    ]
    return (hits[0], []) if len(hits) == 1 else (None, hits)


def _check_fit(args: CheckFitArgs, ctx: Context) -> dict:
    """用**存下来的身材**判断某一条合不合身。用户不必每次报三围。"""
    overrides = {
        key: getattr(args, key)
        for key in body_mod.CIRCUMFERENCE
        if getattr(args, key) is not None
    }
    brand = args.brand.strip().lower()
    category = args.category.strip().upper()
    entries, _ = na.load_newarrivals(ctx.settings.newarrivals_dir)
    charts = size.load_charts(ctx.settings.size_dir)

    entry = None
    if args.entry:
        found, candidates = _find_entry(entries, args.entry)
        if found is None:
            if candidates:
                names = "、".join(f"{item.brand_name} {item.series}" for item in candidates[:4])
                return {
                    "ok": True, "empty": False, "status": "ambiguous",
                    "summary": f"「{args.entry}」匹配到多条：{names}。请给更具体的系列名。",
                    "evidence": [
                        {
                            "ref": item.id,
                            "title": f"{item.brand_name} {item.series}",
                            "text": f"id={item.id}｜发售 {item.release_date}",
                        }
                        for item in candidates[:4]
                    ],
                }
            return {
                "ok": True, "empty": False, "status": "no_data",
                "summary": f"没找到「{args.entry}」这条上新。",
                "evidence": [{
                    "ref": "entry:none",
                    "title": "没有匹配的上新条目",
                    "text": f"内容里没有「{args.entry}」；可能是别名/系列名写法不同。",
                }],
            }
        entry = found

    if entry is None and not brand:
        return {
            "ok": True, "empty": False, "status": "need_input",
            "summary": "要说清是哪一件：给 entry（条目名），或给 brand + category。",
            "evidence": [{
                "ref": "fit:need_input",
                "title": "需要更具体的商品",
                "text": "没有指定具体条目，也没有给品牌和品类。",
            }],
        }

    payload = dataclasses.asdict(entry) if entry is not None else None
    label = (
        f"{payload.get('brand_name') or payload.get('brand')} {payload.get('series')}"
        if payload else f"{brand} {category}".strip()
    )
    report = body_mod.fit_report(
        charts,
        ctx.body or {},
        entry=payload,
        brand=brand,
        category=category,
        overrides=overrides,
        label=label,
    )

    detail = str(report.get("detail", ""))
    source = str(report.get("source") or "")
    verified = report.get("verified")
    warnings = list(report.get("warnings") or [])
    if verified is False and not any("未核对" in item for item in warnings):
        warnings.append("尺码表未核对，仅供参考。")

    extra = " ".join(item for item in warnings if item not in detail)
    evidence_text = " ".join(filter(None, [detail, extra]))
    if source:
        evidence_text += f"；来源：{source}"

    summary = f"{label}：{report.get('verdict')}。{report.get('detail', '')}"
    if verified is False:
        summary += "（尺码表未核对，仅供参考）"

    # 刻意**不把 BMI 写进证据**：它是由身高体重推出来的，同属身体数据；
    # BMI 只在「我的」页本地显示，不让它随观察进入模型上下文。
    return {
        "ok": True,
        "empty": False,
        "summary": summary,
        "evidence": [{
            "ref": payload["id"] if payload else f"{brand}:{category}",
            "title": f"{label}｜{report.get('verdict')}",
            "text": _clip(evidence_text),
        }],
        "status": report.get("status"),
        "verdict": report.get("verdict"),
        "recommended": report.get("recommended"),
        "verified": verified,
    }


# --------------------------------------------------------------------- 商品档案（并行取证）


class ItemProfileArgs(BaseModel):
    entry: str = Field(
        ..., min_length=1, max_length=60,
        description="上新条目 id、系列名或别名，如 2026-10-baby-qiuri、秋日私语",
    )


PROFILE_EVIDENCE_LIMIT = 5


def _profile_fit(entry, ctx: Context) -> tuple[list[dict], str]:
    """合身判断。没存身材就**不调** check_fit——档案里不需要一句"请先填三围"。"""
    if not ctx.body:
        return [], "未存身材，未判断合身"
    report = _check_fit(CheckFitArgs(entry=entry.id), ctx)
    evidence = [
        {**item, "ref": f"fit:{entry.id}", "title": f"合身｜{item.get('title', '')}"}
        for item in (report.get("evidence") or [])
    ]
    return evidence, str(report.get("verdict") or "").strip()[:60]


def _profile_shop(entry, ctx: Context) -> tuple[list[dict], str]:
    shops, _problems = shop_table.load_shops(ctx.settings.shops_dir)
    link, kind = shop_table.link_for(entry.brand, entry.link, shops)
    if not link:
        return [], ""
    label = "商品链接" if kind == "item" else "店铺页（不是商品页）"
    return (
        [{
            "ref": f"shop:{entry.brand}",
            "title": f"{entry.brand_name}｜{label}",
            "text": _clip(f"{label}：{link}"),
        }],
        "有链接",
    )


def _profile_kb(entry, ctx: Context) -> tuple[list[dict], str]:
    """挂一条品类相关术语。索引没建/检索失败都只影响这一路，不拖垮整份档案。"""
    category = (entry.items or [""])[0]
    if not category:
        return [], ""
    try:
        hits = bridge.search_kb(category, ctx.settings, top_k=1)
    except Exception as exc:  # noqa: BLE001 - 子源失败要局部化
        logger.info("商品档案的知识检索跳过：%s: %s", type(exc).__name__, exc)
        return [], ""
    if not hits:
        return [], ""
    return [{"ref": hits[0]["chunk_id"], "title": "相关知识", "text": _clip(hits[0]["text"])}], "相关知识"


def _item_profile(args: ItemProfileArgs, ctx: Context) -> dict:
    """一次并行取证，返回**这一件**的全部档案。

    为什么是一个工具，而不是让 planner 连调三次：

    1. 一次问答的模型调用次数直接等于成本，三个工具就是三轮规划；
    2. 三个子任务互不依赖（合身、链接、术语），并行做比串行做省时间；
    3. 子源失败必须局部化——知识库没建不该让合身结论也拿不到。

    刻意**不是**第二个对话 agent：没有独立提示词、没有循环、没有重试。
    它就是一张固定的小 DAG，终止条件写在代码里。
    """
    entries, _problems = na.load_newarrivals(ctx.settings.newarrivals_dir)
    if not entries:
        return {"ok": True, "empty": True, "summary": "内容里一条上新都没有。", "evidence": []}

    found, candidates = _find_entry(entries, args.entry)
    if found is None:
        if candidates:
            names = "、".join(f"{item.brand_name} {item.series}" for item in candidates[:4])
            return {
                "ok": True, "empty": False, "status": "ambiguous",
                "summary": f"「{args.entry}」匹配到多条：{names}。请给更具体的系列名。",
                "evidence": [
                    {
                        "ref": item.id,
                        "title": f"{item.brand_name} {item.series}",
                        "text": _clip(f"id={item.id}｜发售 {item.release_date}"),
                    }
                    for item in candidates[:4]
                ],
            }
        return {
            "ok": True, "empty": False, "status": "no_data",
            "summary": (
                f"没找到「{args.entry}」这条上新。"
                "如果用户问的是洗护/术语，不要停：改用 care_advice（按问题里的"
                "面料/颜色给通用建议）或 search_kb。"
            ),
            "evidence": [{
                "ref": "profile:none",
                "title": "没有匹配的上新条目",
                "text": _clip(f"内容里没有「{args.entry}」；可能是别名/系列名写法不同。"),
            }],
        }

    entry = found
    tasks = {
        "fit": lambda: _profile_fit(entry, ctx),
        "shop": lambda: _profile_shop(entry, ctx),
        "kb": lambda: _profile_kb(entry, ctx),
    }
    results: dict[str, tuple[list[dict], str]] = {}
    with ThreadPoolExecutor(max_workers=len(tasks), thread_name_prefix="profile") as pool:
        futures = {name: pool.submit(task) for name, task in tasks.items()}
        for name, future in futures.items():
            try:
                results[name] = future.result()
            except Exception as exc:  # noqa: BLE001 - 单个子任务失败不影响其它证据
                logger.warning("商品档案子任务 %s 失败：%s: %s", name, type(exc).__name__, exc)
                results[name] = ([], "")

    evidence = [{
        "ref": entry.id,
        "title": f"{entry.brand_name} {entry.series}",
        "text": _entry_text(dataclasses.asdict(entry)),
    }]
    for name in ("fit", "shop", "kb"):
        evidence.extend(results[name][0])
    evidence = evidence[:PROFILE_EVIDENCE_LIMIT]

    head = f"「{entry.brand_name} {entry.series}」发售 {entry.release_date}"
    if entry.price_range:
        amount = entry.price_range if entry.currency == "CNY" else f"{entry.price_range} {entry.currency}"
        head += f"，价位 {amount}"
    lines = [head]
    for name in ("fit", "shop", "kb"):
        if results[name][1]:
            lines.append(results[name][1])
    return {
        "ok": True,
        "empty": False,
        "status": "ok",
        "summary": "｜".join(lines),
        "evidence": evidence,
        "entry": entry.id,
    }


TOOLS: dict[str, Tool] = {
    tool.name: tool
    for tool in (
        Tool(
            name="search_feed",
            description="在上新列表里按关键词/风格/款式/价格/时间筛选。要回答「有没有某类新款」只用它。",
            args_model=SearchFeedArgs,
            handler=_search_feed,
        ),
        Tool(
            name="search_kb",
            description="在知识库里检索术语、洗护、尺码常识等条目，返回原文片段。",
            args_model=SearchKbArgs,
            handler=_search_kb,
        ),
        Tool(
            name="care_advice",
            description=(
                "按面料/工艺/情况给出洗护结论（能不能机洗、怎么洗）。确定性规则，不要自己推断。"
                "商品名查不到时，仍可按问题里的材质/颜色给通用建议，不要直接拒答。"
            ),
            args_model=CareArgs,
            handler=_care_advice,
        ),
        Tool(
            name="size_recommend",
            description=(
                "按**品牌级 S/M/L 表**算推荐码。不传三围时自动用用户存好的身材数据；"
                "没有该品牌表时会明确说没有。AP/Baby 官方不提供品牌级分码表，"
                "这两家要判断具体商品请改用 check_fit。"
            ),
            args_model=SizeArgs,
            handler=_size_recommend,
        ),
        Tool(
            name="check_fit",
            description=(
                "判断「这一件我穿得上吗」：优先用商品页实测尺寸（newarrivals 的 size），"
                "没有商品级数据才回退品牌尺码表。用用户已保存的身材（胸/腰/臀/肩），"
                "给出能穿/不能穿与具体码。**不要自己推算，也不要在对话里索要三围**——"
                "用户没填它会明确说缺什么。"
            ),
            args_model=CheckFitArgs,
            handler=_check_fit,
        ),
        Tool(
            name="item_profile",
            description=(
                "一次取回**某一条上新**的全部档案：发售/价格/款式/实测尺码、按已存身材的合身判断、"
                "商品或店铺链接、相关术语。知道具体系列名或条目 id 时优先用它——"
                "它把「查条目 + 判合身 + 找链接 + 挂术语」并行做完，比逐个工具查少几轮。"
                "只问「有哪些新款」用 search_feed；只问通用知识用 search_kb。"
            ),
            args_model=ItemProfileArgs,
            handler=_item_profile,
        ),
    )
}


def catalog() -> list[dict]:
    return [{"name": tool.name, "description": tool.description, "parameters": tool.args_model.model_json_schema()} for tool in TOOLS.values()]
