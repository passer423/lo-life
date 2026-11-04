"""身材参数：校验、"这件我穿得上吗"的结论，以及几件**不做**的事。

## 一条纪律：体重不参与选码

尺码表是按**围度**定的（胸围/腰围/臀围/肩宽）。体重和合身没有稳定函数关系：
同样 50kg，155cm 和 170cm 是两种身材。拿体重去"推算"码数就是编。

所以体重只做两件事：

1. 和身高一起算 BMI，作为**录入是否填错**的提示（比如 45kg 配 100cm 胸围）；
2. 在回答里作为背景出现，不参与推荐。

身高同样不参与选码，但会和条目的 ``length``（衣长）一起给出**客观比值**。
穿衣的人自己知道 0.56 意味着什么；让模型替她猜"到膝盖还是过膝"就是编——
不同版型的腰线位置差很多，这个数字只能由人看。

## 为什么单独成模块

``size.py`` 管"表怎么算"，这里管"人怎么算"：参数的合法范围、BMI、以及把
``size.recommend_for_entry`` 的四种状态翻译成人话。分开之后 ``size.py`` 仍然是
纯表驱动、可单独测试。
"""

from __future__ import annotations

from app.domain import size

# 允许范围。宽到能容纳真实身材，窄到能挡住"手滑多打一位"：
# 178 打成 1178、88 打成 888 都会被挡下来，而不是算出个荒唐的推荐。
LIMITS: dict[str, tuple[float, float, str]] = {
    "height": (120.0, 220.0, "身高"),
    "weight": (30.0, 150.0, "体重"),
    "bust": (50.0, 160.0, "胸围"),
    "waist": (40.0, 150.0, "腰围"),
    "hip": (50.0, 170.0, "臀围"),
    "shoulder": (25.0, 60.0, "肩宽"),
}

CIRCUMFERENCE = ("bust", "waist", "hip", "shoulder")

UNIT = {"height": "cm", "weight": "kg", "bust": "cm", "waist": "cm", "hip": "cm", "shoulder": "cm"}


def sanitize(payload: dict) -> tuple[dict, list[str]]:
    """校验并归一化。返回 ``(合法字段, 问题列表)``。

    非法值**不静默丢弃**：丢弃会让人以为存上了。这里返回问题列表，由接口回 400。
    """
    clean: dict[str, float | None] = {}
    problems: list[str] = []
    for field, (low, high, label) in LIMITS.items():
        if field not in payload:
            continue
        raw = payload[field]
        if raw is None or raw == "":
            clean[field] = None            # 显式清空
            continue
        if isinstance(raw, bool):
            problems.append(f"{label} 必须是数字")
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            problems.append(f"{label} 必须是数字")
            continue
        if not low <= value <= high:
            problems.append(f"{label} 超出合理范围（{low:.0f}–{high:.0f}{UNIT[field]}）")
            continue
        clean[field] = round(value, 1)
    return clean, problems


def bmi(height: float | None, weight: float | None) -> dict | None:
    """BMI + 中国标准的分档。**只用于提示，不参与选码。**"""
    if not height or not weight:
        return None
    meters = height / 100
    value = round(weight / (meters * meters), 1)
    if value < 18.5:
        band = "偏瘦"
    elif value < 24:
        band = "正常"
    elif value < 28:
        band = "偏胖"
    else:
        band = "肥胖"
    return {"value": value, "band": band}


def length_note(height: float | None, length: float | None) -> dict | None:
    """衣长与身高的**客观比值**，不做位置猜测。"""
    if not height or not length:
        return None
    return {
        "length": length,
        "height": height,
        "ratio": round(length / height, 3),
        "tip": "衣长/身高 只是客观比值：不同版型的腰线位置差别很大，具体到哪里要对着实物图看。",
    }


def _measurements(body: dict, overrides: dict | None = None) -> dict[str, float]:
    values = {dim: body[dim] for dim in CIRCUMFERENCE if body.get(dim)}
    for dim, value in (overrides or {}).items():
        if dim in CIRCUMFERENCE and value:
            values[dim] = float(value)
    return values


_STATUS_TEXT = {
    "no_chart": "这个品牌还没录尺码表，我不能判断合不合身——不猜。",
    "no_category": "这个品牌的尺码表里没有这个品类（可能是 KC、斗篷或定制款），判断不了。",
    "item_level_only": "这个品牌官方只提供商品级实测尺寸，不提供品牌级分码表；请指定具体商品。",
}


def fit_report(
    charts: dict,
    body: dict,
    *,
    entry: dict | None = None,
    brand: str = "",
    category: str = "",
    overrides: dict | None = None,
    label: str = "",
) -> dict:
    """给"我穿得上吗"出一个可回答的结论。

    复用 ``size.recommend_for_entry`` 的四种状态，不在这里重新实现表的计算。
    """
    measurements = _measurements(body or {}, overrides)
    name = label or (f"{entry.get('brand_name') or entry.get('brand')} {entry.get('series')}" if entry else brand)

    if not body and not measurements:
        return {
            "status": "no_profile",
            "ok": False,
            "verdict": "还不知道你的身材数据",
            "detail": "先在「我的 → 身材参数」里填胸围、腰围（有臀围/肩宽更准），我才能按尺码表判断。",
            "missing": list(CIRCUMFERENCE),
            "label": name,
        }

    entry_brand = str(entry.get("brand") or "") if entry is not None else brand
    chart = charts.get(entry_brand)

    if entry is not None:
        result = size.recommend_for_entry(
            charts,
            entry_brand,
            entry.get("items") or [],
            measurements,
            # 商品页实测区间优先于品牌表：这是**这一件**的尺寸，不是品牌平均
            entry_size=entry.get("size"),
        )
    elif chart is None:
        result = {"status": "no_chart", "brand": brand}
    else:
        result = size.recommend_for_entry(charts, brand, [category] if category else [], measurements)

    report: dict = {
        "status": result.get("status"),
        "label": name,
        "category": result.get("category", category),
        "measured": measurements,
        "body": {key: value for key, value in (body or {}).items() if key != "updated_at"},
    }
    height = (body or {}).get("height")
    mass = (body or {}).get("weight")
    report["bmi"] = bmi(height, mass)

    if result.get("status") != "ok":
        # need_measurements 的 detail 来自 size.recommend 的 ValueError 文案（会说缺哪一维）
        detail = result.get("detail") or _STATUS_TEXT.get(result.get("status"), "判断不了。")
        if result.get("status") == "item_level_only" and result.get("note"):
            detail = f"{detail}{result['note']}"
        report.update({
            "ok": False,
            "verdict": "判断不了",
            "detail": detail,
            "available": result.get("available"),
            "source": result.get("source") or (chart.source if chart is not None else ""),
            "verified": chart.verified if chart is not None else False,
            "raw": result,
        })
        return report

    # 商品级实测区间（官网商品页给的「这一件」尺寸），不产出 S/M/L
    if result.get("kind"):
        source = str(result.get("source") or (chart.source if chart is not None else ""))
        warnings = list(result.get("warnings") or [])
        if not source:
            warnings.append("商品级尺寸没有标注来源，建议核对官网商品页。")
        advice = result.get("advice") or []
        report.update({
            "status": "ok",
            "category": result.get("label") or "F",
            "ok": result.get("verdict") != "too_small",
            "verdict": result.get("headline") or "能穿",
            # detail 只放建议，不再重复 headline；证据标题里已经有 verdict
            "detail": " ".join(advice),
            "recommended": result.get("recommended"),
            "confidence": "high" if result.get("verdict") == "fits" else "medium",
            "advice": advice,
            "warnings": warnings,
            "fit": result.get("kind") or "free",
            "sizes": [],
            "ranges": result.get("ranges") or {},
            "shortfall": result.get("shortfall") or {},
            "slack": result.get("slack") or {},
            "source": source,
            "verified": bool(source),
        })
        return report

    recommended = result.get("recommended")
    warnings = result.get("warnings") or []
    if recommended:
        verdict = f"能穿，{recommended} 码"
        detail = f"{report['label']} 的 {result['category']} 推荐 {recommended} 码（置信度 {result['confidence']}）。"
        if warnings:
            detail += " " + " ".join(warnings)
        else:
            detail += " 各维度都在余量范围内。"
        ok = True
    else:
        verdict = "不建议买"
        detail = " ".join(warnings) or f"这个品牌的 {result['category']} 最大码也不够。"
        ok = False

    # 衣长：只有算出了码才有具体数字
    if recommended:
        category_name = result.get("category")
        if chart is not None and category_name in chart.categories:
            spec = (chart.categories[category_name].get("sizes") or {}).get(recommended) or {}
            report["length"] = length_note(height, spec.get("length"))

    report.update({
        "ok": ok,
        "verdict": verdict,
        "detail": detail,
        "recommended": recommended,
        "confidence": result.get("confidence"),
        "advice": result.get("advice") or [],
        "warnings": warnings,
        "fit": result.get("fit"),
        "sizes": result.get("sizes"),
        "verified": chart.verified if chart is not None else False,
        "source": chart.source if chart is not None else "",
    })
    return report
