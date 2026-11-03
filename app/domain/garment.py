"""从商品页正文里读**成衣实测尺寸**。

## 为什么这个模块必须存在

真实的两家官网都**不是**按 S/M/L 给尺寸的。它们给的是"一件衣服的实测范围"：

    AP  ：・着丈:約89㎝  ・バスト:約91～101㎝  ・ウエスト切替位置:約74～86㎝
    Baby：サイズ 着丈：約99～104cm バスト：約90～99cm ウエスト：約74～85cm

范围来自シャーリング（抽褶）之类的弹性结构——它本来就是"能撑到多少"，
不是"某个码多宽"。硬把它塞进 `sizes: {S:{...}, M:{...}}` 只能靠编，
而编出来的码数会让用户按一个不存在的 M 码去下单。

所以：**尺码是商品级的事实，不是品牌级的。** 这里只做解析，判断在 `size.py`。

## 解析口径

- 只认带单位的（`cm` / `㎝`），裸数字不当尺寸；
- `約` 是"约"，允许出现也允许不出现；
- `～` / `~` / `〜` / `-` 都当区间分隔符（三家站写法各不相同）；
- `ウエスト切替位置` 记成 `waist`：那是抽褶裙腰的实际位置，判断能不能穿用的就是它，
  但**原文照抄到 source**，让人知道它不叫"腰围"。
"""

from __future__ import annotations

import re

# 日文写法 → 应用里的维度名（和 `size.DIM_LABEL` 一致）
DIM_KEYS = {
    "バスト": "bust",
    "ウエスト切替位置": "waist",
    "ウエスト": "waist",
    "ヒップ": "hip",
    "肩幅": "shoulder",
    "着丈": "length",
}

# 长键在前：`ウエスト切替位置` 必须先于 `ウエスト` 匹配，否则只会吃掉前半截
_ORDERED = sorted(DIM_KEYS, key=len, reverse=True)
_MEASURE = re.compile(
    r"(" + "|".join(_ORDERED) + r")\s*[:：]\s*約?\s*(\d+(?:\.\d+)?)"   # 维度：约 89
    r"(?:\s*[～~〜\-]\s*約?\s*(\d+(?:\.\d+)?))?"                       # ～101
    r"\s*(?:cm|㎝)",                                                   # 必须带单位
    re.I,
)


def parse_measurements(text: str) -> dict[str, list[float]]:
    """返回 ``{"bust": [91.0, 101.0], "length": [89.0, 89.0], ...}``，读不到就空字典。"""
    found: dict[str, list[float]] = {}
    for match in _MEASURE.finditer(text or ""):
        key = DIM_KEYS[match.group(1)]
        low = float(match.group(2))
        high = float(match.group(3)) if match.group(3) else low
        if key in found:                       # 同一维出现两次：保留第一次
            continue
        found[key] = [min(low, high), max(low, high)]
    return found


def size_from_page(
    text: str,
    *,
    source: str,
    note: str = "",
    label: str = "F",
) -> dict | None:
    """把商品页正文转成 ``newarrivals`` 的 ``size`` 字段。

    只做"解析 + 组字段"，不联网、不写盘。抓取由 ``scripts/fetch_sizes.py`` 负责。
    没有带单位的尺寸就返回 ``None``——**不拿裸数字当尺寸**。
    """
    measurements = parse_measurements(text)
    if not measurements:
        return None
    result: dict = {
        "kind": "free",
        "label": label,
        **measurements,
        "source": source,
    }
    if note:
        result["note"] = note
    return result


def summary(measurements: dict[str, list[float]]) -> str:
    """「胸 91–101 / 腰 74–86」这样的一行人话，给卡片和错误文案用。"""
    labels = {"bust": "胸", "waist": "腰", "hip": "臀", "shoulder": "肩", "length": "长"}
    parts = []
    for key in ("bust", "waist", "hip", "shoulder", "length"):
        bounds = measurements.get(key)
        if not bounds:
            continue
        low, high = bounds
        text = f"{low:g}" if low == high else f"{low:g}–{high:g}"
        parts.append(f"{labels[key]} {text}")
    return " / ".join(parts)
