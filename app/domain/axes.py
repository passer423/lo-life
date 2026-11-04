"""偏好轴的声明表。

单独一个模块是为了**打断循环导入**：`newarrivals` 要用轴表算分，
`preferences` 要用轴表生成选项和校验，两边都依赖它，而它谁都不依赖。

每个轴自带匹配方式、权重、最多计几个、原因怎么写。加一个新轴（比如"价位带"）
只需要在 ``AXES`` 里加一条——排序、校验、接口选项、前端标签组全都自动跟上。

## 两种匹配方式

* **集合求交**（默认）：轴从条目上取出候选值（店家、风格、款式），与用户选中的值求交。
  值必须**预先存在**，所以这类轴不接受自由输入。
* **自定义匹配器**：由 ``matcher`` 决定。目前只有"自定义标签"用它——用户写的词
  对条目文本做**子串**匹配。这是唯一一个允许自由输入的轴，因为它不要求词先存在。

> 子串匹配刻意用 ``in`` 而不是正则：用户输入直接进正则会有灾难性回溯的风险，
> 而这里不需要任何模式能力。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# 自定义标签的边界。太长的词不可能出现在系列名里，条数不设上限则存储和排序都会失控。
CUSTOM_MAX_LEN = 20
CUSTOM_MAX_ITEMS = 20


def entry_haystack(entry: Any) -> str:
    """条目里可被关键词命中的文本。统一小写，让英文词不区分大小写。"""
    parts = [
        str(getattr(entry, "series", "") or ""),
        str(getattr(entry, "note", "") or ""),
        str(getattr(entry, "brand_name", "") or ""),
        " ".join(str(item) for item in getattr(entry, "items", []) or []),
        " ".join(str(style) for style in getattr(entry, "styles", []) or []),
        # 别名也进检索：换成官网真名之后，旧的中文系列名还得搜得到——
        # 否则用户按记忆里的名字搜，会以为这条被删了。
        " ".join(str(alias) for alias in getattr(entry, "aliases", []) or []),
    ]
    return " ".join(parts).lower()


@dataclass(frozen=True, slots=True)
class Axis:
    key: str
    label: str
    hint: str
    bonus: float                                  # 每个匹配项的加分
    max_matches: int                              # 最多按几个匹配项计分，防止全选刷满
    reason_prefix: str                            # 卡片上"为什么排前面"的前缀
    values_of: Callable[[Any], list[str]] | None = None        # 集合求交：从条目取值
    matcher: Callable[[Any, list[str]], list[str]] | None = None  # 自定义匹配器
    allow_custom: bool = False                    # 是否允许自由输入
    show_values: bool = True                      # 原因里是否带上具体值

    def match(self, entry: Any, chosen: list[str]) -> list[str]:
        """返回该条上新在这个轴上命中的值（按 max_matches 截断）。"""
        if not chosen:
            return []
        if self.matcher is not None:
            return self.matcher(entry, chosen)[: self.max_matches]
        values = self.values_of(entry) if self.values_of else []
        return [value for value in dict.fromkeys(values) if value in chosen][: self.max_matches]

    def reason(self, hits: list[str]) -> str:
        return self.reason_prefix + (" / ".join(hits) if self.show_values else "")


AXES: tuple[Axis, ...] = (
    Axis(
        key="shops",
        label="喜欢的店家",
        hint="勾选之后，这家店的上新会排到最前面。",
        bonus=100.0,
        max_matches=1,
        reason_prefix="你喜欢的店",
        values_of=lambda entry: [entry.brand],
        show_values=False,
    ),
    Axis(
        key="styles",
        label="喜欢的风格",
        hint="和条目上的风格标签求交集，最多按 2 个计分。",
        bonus=40.0,
        max_matches=2,
        reason_prefix="风格：",
        values_of=lambda entry: list(entry.styles),
    ),
    Axis(
        key="keywords",
        label="自定义标签",
        hint="自己写关键词，命中系列名 / 备注 / 品牌名 / 款式 / 风格就加分。",
        bonus=30.0,
        max_matches=2,
        reason_prefix="关键词：",
        allow_custom=True,
        matcher=lambda entry, chosen: [
            keyword for keyword in chosen if keyword.lower() in entry_haystack(entry)
        ],
    ),
    Axis(
        key="categories",
        label="常穿的款式",
        hint="JSK / OP / SK 这些，最多按 2 个计分。",
        bonus=25.0,
        max_matches=2,
        reason_prefix="款式：",
        values_of=lambda entry: list(entry.items),
    ),
)

AXIS_BY_KEY: dict[str, Axis] = {axis.key: axis for axis in AXES}
AXIS_KEYS: tuple[str, ...] = tuple(axis.key for axis in AXES)
