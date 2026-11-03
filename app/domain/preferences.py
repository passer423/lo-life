"""偏好的选项、校验与展示。

轴的定义在 `axes.py`（避免循环导入），这里负责把"一堆上新条目"变成
"每个轴可选哪些标签"，校验用户提交的值，并回报自定义标签的命中情况。

## 自由输入的安全阀

允许自由输入的轴只有"自定义标签"。它有两个必须存在的护栏：

1. **边界**：单词最长 ``CUSTOM_MAX_LEN``、每个轴最多 ``CUSTOM_MAX_ITEMS`` 个。
   不设上限的话，存储和排序都会被无限增长的关键词拖垮。
2. **回报命中情况**（``match_report``）：告诉用户"这个词在当前内容里有没有匹配到"。
   没有这个回报，用户写了一个永远不命中的词，只会以为功能坏了——
   而这正是我一开始反对自由输入的理由。现在它被明确告知了。
"""

from __future__ import annotations

from app.domain import newarrivals as na
from app.domain.axes import AXES, AXIS_BY_KEY, AXIS_KEYS, CUSTOM_MAX_ITEMS, CUSTOM_MAX_LEN


def build_options(entries: list[na.NewArrival]) -> dict[str, list[dict]]:
    """每个轴可选的标签（外加各标签下的上新条数）。

    条数**不显示在界面上**（标签上只留名字），但接口照样返回：调试内容、
    排查"为什么勾了没效果"、以及将来做"标签按热度排序"都要用它。
    自定义轴没有预置选项，返回空列表——它的值来自用户输入。
    """
    shop_counts: dict[str, int] = {}
    shop_names: dict[str, str] = {}
    style_counts: dict[str, int] = {}
    category_counts: dict[str, int] = {}

    for entry in entries:
        shop_counts[entry.brand] = shop_counts.get(entry.brand, 0) + 1
        shop_names[entry.brand] = entry.brand_name
        for style in entry.styles:
            style_counts[style] = style_counts.get(style, 0) + 1
        for item in entry.items:
            category_counts[item] = category_counts.get(item, 0) + 1

    options: dict[str, list[dict]] = {
        "shops": [
            {"value": brand, "label": shop_names[brand], "count": count}
            for brand, count in sorted(shop_counts.items(), key=lambda pair: (-pair[1], pair[0]))
        ],
        # 风格是固定词表：没被任何条目用到的也列出来，否则用户以为没有这个选项
        "styles": [
            {"value": style, "label": style, "count": style_counts.get(style, 0)}
            for style in na.STYLE_OPTIONS
        ],
        "categories": [
            {"value": name, "label": name, "count": count}
            for name, count in sorted(category_counts.items(), key=lambda pair: (-pair[1], pair[0]))
        ],
    }
    for axis in AXES:
        options.setdefault(axis.key, [])
    return options


def validate(axes: dict[str, list[str]], options: dict[str, list[dict]]) -> tuple[dict[str, list[str]], str]:
    """校验并去重。返回 ``(清理后的偏好, 错误信息)``，错误信息为空表示通过。"""
    cleaned: dict[str, list[str]] = {}
    for key, values in axes.items():
        axis = AXIS_BY_KEY.get(key)
        if axis is None:
            return {}, f"未知的偏好轴：{key}（可选：{'、'.join(AXIS_KEYS)}）"

        if axis.allow_custom:
            result, error = _validate_custom(axis, values)
            if error:
                return {}, error
            cleaned[key] = result
            continue

        allowed = {row["value"] for row in options.get(key, [])}
        deduped = list(dict.fromkeys(str(value) for value in (values or [])))
        unknown = [value for value in deduped if value not in allowed]
        if unknown:
            return {}, f"「{axis.label}」里没有这些选项：{'、'.join(unknown)}"
        cleaned[key] = deduped
    return cleaned, ""


def _validate_custom(axis, values) -> tuple[list[str], str]:
    cleaned: list[str] = []
    for raw in values or []:
        # 折叠空白：容忍" 缎 带 "这种输入，但把它归一到同一个值
        text = " ".join(str(raw).split())
        if not text:
            continue
        if len(text) > CUSTOM_MAX_LEN:
            return [], (f"「{axis.label}」里单个标签最多 {CUSTOM_MAX_LEN} 个字：{text[:8]}…")
        if text not in cleaned:
            cleaned.append(text)
    if len(cleaned) > CUSTOM_MAX_ITEMS:
        return [], f"「{axis.label}」最多 {CUSTOM_MAX_ITEMS} 个标签"
    return cleaned, ""


def match_report(entries: list[na.NewArrival], preferences: dict[str, list[str]]) -> dict[str, dict]:
    """自定义轴上"哪些词在当前内容里能匹配、哪些不能"。

    这是自由输入的**安全阀**：没有它，用户写了一个永远不命中的词只会以为功能坏了。
    每次请求现算（条目数量是几十条级别，成本可以忽略）。
    """
    report: dict[str, dict] = {}
    for axis in AXES:
        if not axis.allow_custom:
            continue
        chosen = preferences.get(axis.key) or []
        if not chosen:
            continue
        matched = [
            keyword for keyword in chosen
            if any(axis.match(entry, [keyword]) for entry in entries)
        ]
        report[axis.key] = {
            "matched": matched,
            "unmatched": [keyword for keyword in chosen if keyword not in matched],
        }
    return report


def describe(preferences: dict[str, list[str]], options: dict[str, list[dict]]) -> list[dict]:
    """把偏好渲染成"人话"，给界面上的「当前排序依据」用。"""
    rows: list[dict] = []
    for axis in AXES:
        chosen = preferences.get(axis.key) or []
        if not chosen:
            continue
        labels = {row["value"]: row["label"] for row in options.get(axis.key, [])}
        rows.append(
            {
                "key": axis.key,
                "label": axis.label,
                "values": [labels.get(value, value) for value in chosen],
                "bonus": axis.bonus,
            }
        )
    return rows
