"""尺码推荐：确定性查表，不走模型。

为什么不塞进 RAG：尺码是**纯计算**（身体数据 + 尺码表 → 结论）。交给模型只会得到
一个"每次略有不同"的答案，而这个问题用户是拿着钱在店里问的，错一次就丢信任。

三个刻意的设计：

* **没有这个品牌的表就直说没有**，不猜、不套用别家。和知识问答的拒答是同一条原则。
* **富余量（ease）显式建模**。成衣必须比身体尺寸松，紧身数据直接比大小一定选小一码。
* **版型偏移单独建模**。同标的 M 实际可能差两三厘米，`fit` 字段就是拿来纠这个的。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# 各维度的默认富余量（厘米）。裙子胸腰需要放量，肩宽几乎不放——肩宽放多了会塌肩。
DEFAULT_EASE: dict[str, float] = {
    "bust": 4.0,
    "waist": 2.0,
    "hip": 4.0,
    "shoulder": 0.0,
}

# 版型偏移：标"偏小"的牌子，同一尺码实际更紧，所以要把门槛往上抬。
FIT_OFFSET: dict[str, float] = {"偏小": 2.0, "正码": 0.0, "偏大": -2.0}

DIM_LABEL = {
    "bust": "胸围",
    "waist": "腰围",
    "hip": "臀围",
    "shoulder": "肩宽",
    "length": "裙长",
}

# 单维松量超过这个值算"偏松"，值不值得提醒
SLACK_WARN = 8.0


class ChartNotFound(LookupError):
    """没有这个品牌的尺码表。这不是故障，是"我们没有数据"。"""


@dataclass(slots=True)
class Chart:
    brand: str
    display_name: str
    fit: str
    chart_version: str
    source: str
    note: str
    categories: dict[str, Any]
    # 表的类型：
    #   size_chart —— 品牌级 S/M/L 表，能直接算码；
    #   item_level —— 官方不提供品牌级分码表，只在商品页给单件实测尺寸。
    # AP / Baby 属于后者：硬拆成 S/M/L 只能靠编，所以 categories 为空，
    # 判断走 newarrivals 的 size 字段（check_fit）。
    kind: str = "size_chart"
    # 这份表是不是按品牌官方尺码表录的。
    #
    # 默认 **False**，即"没声明就是没核对"。理由：推荐卡片会把它当事实展示
    # （`JSK S/M/L/XL` + 偏小/正码），而"偏小"直接决定选哪一码——猜错一次就是买错一码。
    # 宁可少显示一行，也不要把占位数据当真的印在首页上。
    verified: bool = False

    @property
    def fit_offset(self) -> float:
        return FIT_OFFSET.get(self.fit, 0.0)


def load_charts(size_dir: Path) -> dict[str, Chart]:
    """读 content/size/*.json。以下划线开头的文件是模板/说明，跳过。"""
    charts: dict[str, Chart] = {}
    if not size_dir.exists():
        return charts
    for path in sorted(size_dir.glob("*.json")):
        if path.name.startswith("_"):
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"尺码表不是合法 JSON：{path.name} —— {exc}") from exc
        brand = raw.get("brand") or path.stem
        charts[brand] = Chart(
            brand=brand,
            display_name=raw.get("display_name") or brand,
            fit=raw.get("fit") or "正码",
            chart_version=str(raw.get("chart_version") or ""),
            source=raw.get("source") or "",
            note=raw.get("note") or "",
            categories=raw.get("categories") or {},
            kind=str(raw.get("kind") or "size_chart"),
            verified=bool(raw.get("verified")),
        )
    return charts


def list_brands(charts: dict[str, Chart]) -> list[dict]:
    return [
        {
            "brand": chart.brand,
            "display_name": chart.display_name,
            "fit": chart.fit,
            "chart_version": chart.chart_version,
            "source": chart.source,
            "kind": chart.kind,
            "note": chart.note,
            "verified": chart.verified,
            "categories": sorted(chart.categories),
            # 每个品类看哪几维，前端据此只渲染需要的输入框——
            # 让用户填一个根本不参与判断的臀围，是在制造"我填了它却没影响"的困惑
            "category_keys": {
                name: list(group.get("key") or [])
                for name, group in chart.categories.items()
            },
        }
        for chart in sorted(charts.values(), key=lambda item: item.brand)
    ]


def recommend(
    charts: dict[str, Chart],
    brand: str,
    category: str,
    measurements: dict[str, float],
) -> dict:
    """给一个品牌 + 品类 + 身体数据，返回推荐尺码。

    ``measurements`` 只认该品类 ``key`` 里列出的维度；缺任何一个都还能算，
    但参与判断的维度越少，结论越不可靠——所以会明确回报置信度。
    """
    chart = charts.get(brand)
    if chart is None:
        raise ChartNotFound(
            f"没有「{brand}」的尺码表。可用的有：{', '.join(sorted(charts)) or '（一个都没有）'}"
        )

    if chart.kind == "item_level":
        hint = f"「{chart.display_name}」官方只提供商品级实测尺寸，不提供品牌级分码表。"
        if chart.note:
            hint += chart.note
        raise ChartNotFound(hint)

    group = chart.categories.get(category)
    if not group:
        raise ChartNotFound(
            f"「{chart.display_name}」还没有 {category} 的尺码数据。"
            f"已有品类：{', '.join(sorted(chart.categories)) or '（无）'}"
        )

    key_dims: list[str] = list(group.get("key") or [])
    ease: dict[str, float] = {**DEFAULT_EASE, **(group.get("ease") or {})}
    sizes: dict[str, dict[str, float]] = group.get("sizes") or {}

    provided = {
        dim: float(value)
        for dim, value in measurements.items()
        if dim in key_dims and value is not None and float(value) > 0
    }
    if not provided:
        raise ValueError(
            f"{category} 至少要给一项尺寸：{'、'.join(DIM_LABEL.get(d, d) for d in key_dims)}"
        )

    required = {dim: value + ease.get(dim, 0.0) + chart.fit_offset for dim, value in provided.items()}

    evaluated: list[dict] = []
    fitting: list[str] = []
    for size_name in sorted(sizes, key=_size_order):
        garment = sizes[size_name]
        shortfall: dict[str, float] = {}
        slack: dict[str, float] = {}
        for dim, need in required.items():
            if dim not in garment:
                continue
            value = float(garment[dim])
            if value < need:
                shortfall[dim] = round(need - value, 1)
            else:
                slack[dim] = round(value - need, 1)
        ok = not shortfall
        if ok:
            fitting.append(size_name)
        evaluated.append(
            {
                "size": size_name,
                "ok": ok,
                "shortfall": shortfall,
                "slack": slack,
                "garment": {dim: garment[dim] for dim in provided if dim in garment},
            }
        )

    warnings: list[str] = []
    advice: list[str] = []

    if fitting:
        recommended: str | None = fitting[0]      # 最小可穿码；再大只会更松
        chosen = next(item for item in evaluated if item["size"] == recommended)
        roomy = {dim: gap for dim, gap in chosen["slack"].items() if gap > SLACK_WARN}
        if roomy:
            detail = "、".join(f"{DIM_LABEL[d]} 松 {gap:.0f}cm" for d, gap in roomy.items())
            warnings.append(f"{recommended} 码对你偏松（{detail}），可考虑小一码，或确认是否要套厚内搭。")
    else:
        recommended = None
        largest = evaluated[-1] if evaluated else None
        if largest:
            gap = largest["shortfall"]
            detail = "、".join(f"{DIM_LABEL[d]} 还差 {value:.0f}cm" for d, value in gap.items())
            warnings.append(
                f"这个品牌的 {category} 最大只有 {largest['size']} 码，你差 {detail}——建议不要买这一款。"
            )
        advice.append("可以换一个版型更宽的品牌，或者找同款的大码版／定制。")

    # 置信度：只靠一维判断不可靠；维度齐全且有余量才算高
    if len(provided) >= 2 and recommended:
        confidence = "high"
    elif recommended:
        confidence = "medium"
        missing = [DIM_LABEL.get(d, d) for d in key_dims if d not in provided]
        if missing:
            advice.append(f"还缺 {'、'.join(missing)}，补上会更准。")
    else:
        confidence = "low"

    if chart.fit != "正码":
        advice.append(
            f"这个品牌标「{chart.fit}」，判断时已按 {chart.fit_offset:+.0f}cm 修正。"
        )
    if chart.note:
        advice.append(chart.note)

    return {
        "brand": chart.brand,
        "display_name": chart.display_name,
        "category": category,
        "fit": chart.fit,
        "chart_version": chart.chart_version,
        "source": chart.source,
        "measured": provided,
        "required": {dim: round(value, 1) for dim, value in required.items()},
        "recommended": recommended,
        "confidence": confidence,
        "evaluated": evaluated,
        "advice": advice,
        "warnings": warnings,
    }


_SIZE_ALPHABET = ["XXS", "XS", "S", "M", "L", "XL", "XXL", "3XL", "4XL"]


def _size_order(name: str) -> tuple[int, str]:
    """S/M/L 按字母序，数字码按数值——不能直接字符串排序（否则 10 排在 2 前面）。"""
    upper = name.upper()
    if upper in _SIZE_ALPHABET:
        return (_SIZE_ALPHABET.index(upper), upper)
    digits = "".join(ch for ch in upper if ch.isdigit())
    if digits:
        return (100 + int(digits), upper)
    return (900, upper)        # 认不出来的排最后，不猜


def dimensions_of(charts: dict[str, Chart]) -> Iterable[str]:
    """所有表里出现过的维度，给前端做输入框提示用。"""
    seen: set[str] = set()
    for chart in charts.values():
        for group in chart.categories.values():
            seen.update(group.get("key") or [])
    return sorted(seen)


def sizes_of(chart: Chart, category: str) -> list[str]:
    """某品类的可选码，从大到小按穿着顺序排（S < M < L < XL）。"""
    sizes = (chart.categories.get(category) or {}).get("sizes") or {}
    return sorted(sizes, key=_size_order)


def pick_category(chart: Chart, candidates: Iterable[str]) -> str | None:
    """从上新条目的 ``items``（JSK / SK / KC）里挑一个该尺码表**真的有的**品类。

    大小写不敏感，其余一律不猜：条目只有「KC」而表里只有 JSK/OP/SK 时返回 ``None``，
    **不会退化成用 JSK 的码去算**——KC 是发饰，跟裙子不是一个东西，拿裙子的胸围
    去判断「这个 KC 你戴多大的」是错误答案，而不是不精确的答案。
    """
    folded = {name.lower(): name for name in chart.categories}
    for candidate in candidates:
        hit = folded.get(str(candidate).strip().lower())
        if hit is not None:
            return hit
    return None


def recommend_for_entry(
    charts: dict[str, Chart],
    brand: str,
    categories: Iterable[str],
    measurements: dict[str, float],
    *,
    entry_size: dict | None = None,
) -> dict:
    """给**一条上新**算码，返回带状态的结果而不是抛异常。

    优先用**商品自己的实测区间**（``entry_size``，来自官网商品页）：真实的两家官网
    根本不按 S/M/L 给尺寸，给的是一件衣服的实测范围（弹性的抽褶腰、可调肩带）。
    只有在没有商品级数据时才退回品牌尺码表。

    状态有四种，前端各显示一种文案，谁也冒充不了谁：

    * ``ok`` —— 算出来了（``recommended`` 可能是 ``None``，那是"最大码都不够"）
    * ``no_chart`` —— 这个牌子没录尺码表，也没有商品级尺寸
    * ``no_category`` —— 有表，但条目里的品类不在表里（KC / 斗篷 / 定制款…）
    * ``need_measurements`` —— 有数据，但缺判断需要的维度

    批量场景下（`POST /api/feed/sizes`）不能因为一条没数据就整个请求 404：
    列表里 10 条有 8 条能算，用户要的是那 8 条的结果 + 2 条"没数据"的说明。
    """
    if entry_size:
        return judge_ranges(entry_size, measurements)

    chart = charts.get(brand)
    if chart is None:
        return {"status": "no_chart", "brand": brand}

    if chart.kind == "item_level":
        return {
            "status": "item_level_only",
            "brand": brand,
            "source": chart.source,
            "note": chart.note,
        }

    category = pick_category(chart, categories)
    if category is None:
        return {
            "status": "no_category",
            "brand": brand,
            "available": sorted(chart.categories),
        }

    try:
        result = recommend(charts, brand, category, measurements)
    except ValueError as exc:
        return {"status": "need_measurements", "brand": brand, "category": category, "detail": str(exc)}

    return {
        "status": "ok",
        "brand": brand,
        "category": category,
        "fit": chart.fit,
        "sizes": sizes_of(chart, category),
        "recommended": result["recommended"],
        "confidence": result["confidence"],
        "warnings": result["warnings"],
        "advice": result["advice"],
    }


def judge_ranges(entry_size: dict, measurements: dict[str, float]) -> dict:
    """按**成衣实测区间**判断这一件能不能穿。

    ## 为什么不是"选哪个码"

    真实的两家官网不按 S/M/L 给尺寸，给的是一件衣服的实测范围：

        バスト：約91～101㎝   ウエスト切替位置：約74～86㎝

    范围不是"码数"，是抽褶/可调肩带带来的**弹性区间**。所以这里不产出 `M` 这种答案，
    只产出三种判断：能穿 / 偏松 / 穿不下。硬报一个码数才是伪精确。

    富余量沿用 `DEFAULT_EASE`：判断用的是"身体 + 放量"，不是裸身体尺寸——
    拿胸围 84 去比成衣 84，会稳定得出"刚好"，实际穿不上。
    """
    label = str(entry_size.get("label") or "F")
    kind = str(entry_size.get("kind") or "free")
    ranges = {
        dim: [float(bounds[0]), float(bounds[1])]
        for dim, bounds in entry_size.items()
        if dim in DIM_LABEL and isinstance(bounds, (list, tuple)) and len(bounds) == 2
    }
    if not ranges:
        return {"status": "no_chart", "kind": kind}

    provided = {
        dim: float(value)
        for dim, value in measurements.items()
        if dim in ranges and value is not None and float(value) > 0
    }
    if not provided:
        return {
            "status": "need_measurements",
            "kind": kind,
            "label": label,
            "ranges": ranges,
            "detail": f"这件衣服给的是 {'、'.join(DIM_LABEL[d] for d in ranges)}，至少要填一项才能判断。",
        }

    needed = {dim: value + DEFAULT_EASE.get(dim, 0.0) for dim, value in provided.items()}
    shortfall = {
        dim: round(need - ranges[dim][1], 1)
        for dim, need in needed.items()
        if need > ranges[dim][1]
    }
    slack = {
        dim: round(ranges[dim][0] - need, 1)
        for dim, need in needed.items()
        if need < ranges[dim][0]
    }

    warnings: list[str] = []
    advice: list[str] = []
    if shortfall:
        verdict = "too_small"
        detail = "、".join(f"{DIM_LABEL[d]} 差 {value:.0f}cm" for d, value in shortfall.items())
        headline = f"穿不下：{detail}"
        advice.append("这件是单一尺码，撑不到你的尺寸。可以找同系列的大码版，或者换版型更宽的牌子。")
    elif any(value > SLACK_WARN for value in slack.values()):
        verdict = "loose"
        detail = "、".join(f"{DIM_LABEL[d]} 松 {value:.0f}cm" for d, value in slack.items() if value > SLACK_WARN)
        headline = f"{label} 偏松（{detail}）"
        warnings.append("偏松的部分靠抽褶/系带能收一些，但收太多会跑形。")
    else:
        verdict = "fits"
        headline = f"{label} 能穿（在弹性范围内）"

    if entry_size.get("note"):
        advice.append(str(entry_size["note"]))

    return {
        "status": "ok",
        "kind": kind,
        "label": label,
        "verdict": verdict,
        "headline": headline,
        # 前端「你的码」那条路径读的是 recommended：区间型没有码数，就用标签（F）
        "recommended": None if verdict == "too_small" else label,
        "ranges": ranges,
        "needed": {dim: round(value, 1) for dim, value in needed.items()},
        "provided": provided,
        "shortfall": shortfall,
        "slack": slack,
        "warnings": warnings,
        "advice": advice,
        "source": str(entry_size.get("source") or ""),
    }

