"""价格区间的容错解析。

## 为什么存储是字符串、解析在读取侧

`content/newarrivals/_README.md` 明确要求 `price_range` 写成字符串，因为真实的
上新价格常常长这样：

    295 - 880        ¥699 起        一件 480，整套 1100        1280

这些都不能直接当数字用。如果为了"能按价格排序"而把存储改成 `{low, high}`，
录入就变成一件麻烦事——而录入是这个项目里唯一的人工环节，让它变难等于让内容断更。

所以分界线画在这里：**存储保持原样，解析只发生在读取侧**。

## 解析失败时不丢条目

`parse_range()` 解析不出来返回 ``None``。调用方（`newarrivals.feed`）据此把
``price`` 置空、原样展示 `price_range` 字符串——**绝不因为价格写错就让整条上新消失**。
这和 `load_newarrivals` 里"单条坏数据不让整页挂掉"是同一条纪律。
"""

from __future__ import annotations

import re

# 千分位分隔符必须先去掉，否则 "¥1,280" 会被切成 1 和 280 两个数，
# 前者再把区间压成 (1.0, 1280.0)——排序和"500 以下"的筛选会当场错掉。
_THOUSANDS = re.compile(r"(?<=\d)[,，](?=\d)")

_NUMBER = re.compile(r"\d+(?:\.\d+)?")

# 超过这个值当成"不是价格"（误填的手机号、QQ、完整日期）。lo 装再贵也到不了 10 万。
#
# 刻意**不做**"看起来像年份就丢弃"这类猜测：`2026` 无法和"2026 元"区分，
# 硬猜只会在真有一件 2026 元的裙子时把它悄悄吞掉。宁可漏挡，不可错杀。
_MAX_REASONABLE = 100_000.0


def parse_range(text: str) -> tuple[float, float] | None:
    """把 ``price_range`` 解析成 ``(最低价, 最高价)``。单个数字返回 ``(x, x)``。

    解析不出来（空串、纯文字、只有 0）返回 ``None``——调用方要能区分
    "没有价格信息"和"价格是 0 元"。
    """
    if not text:
        return None
    cleaned = _THOUSANDS.sub("", str(text))
    values = []
    for token in _NUMBER.findall(cleaned):
        try:
            value = float(token)
        except ValueError:      # 正则保证不会走到这里，但不要靠保证
            continue
        if 0 < value <= _MAX_REASONABLE:
            values.append(value)
    if not values:
        return None
    return (min(values), max(values))


def within(
    bounds: tuple[float, float] | None,
    *,
    price_min: float | None = None,
    price_max: float | None = None,
) -> bool:
    """区间是否落在筛选范围内。

    **没有价格信息的条目一律不参与价格筛选**（返回 ``False``），而不是当成 0 元
    或"未知但放行"。理由：用户勾了"500 以下"却看到一条不知道多少钱的裙子，
    会以为筛选没生效；而悄悄放行比漏掉更让人恼火。前端的空结果文案要说清这一点。
    """
    if bounds is None:
        return price_min is None and price_max is None
    low, high = bounds
    if price_min is not None and high < price_min:
        return False
    if price_max is not None and low > price_max:
        return False
    return True
