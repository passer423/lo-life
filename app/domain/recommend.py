"""把「用户的偏好 tag」翻译成「他该看哪几条」。

## 为什么这一层是 `domain/`（纯函数、不联网、不调模型）

偏好匹配是**集合运算**：条目上的 shops/styles/categories/keywords 与用户勾选的值求交。
给定同样的条目和同样的偏好，答案必须永远一样——这是"推荐"能被信任的前提。
把模型放进来之后，同一批数据两次刷新给出不同的顺序，用户只会认为它在乱排。

## RAG 在这一层**不参与召回**

知识库里装的是术语、尺码、洗护、指南（`content/kb/*.md`），**没有商品，也没有风格标签**。
拿「甜系」「baby」去检索，实测命中 0 条（见 README「为什么偏好 tag 不查 RAG」）。
所以：

* **召回** = 这里的确定性匹配；
* **RAG** = 给每条命中的建议挂一条「相关知识」（如 JSK → 「JSK 和 OP 有什么区别」），
  它解释背景，不决定排名。

这个分界一旦混掉，推荐就变成"每次说得不一样"的玄学——而用户是拿钱在做决定。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.domain.axes import AXES


@dataclass(frozen=True, slots=True)
class TagMatch:
    axis: str
    label: str
    tag: str

    def as_dict(self) -> dict:
        return {"axis": self.axis, "label": self.label, "tag": self.tag}


@dataclass(frozen=True, slots=True)
class Group:
    """一个偏好轴上的一组成员——「因为喜欢 baby」这一组。"""

    axis: str
    label: str
    tags: list[str]
    item_ids: list[str]

    def as_dict(self) -> dict:
        return {
            "axis": self.axis,
            "label": self.label,
            "tags": list(self.tags),
            "item_ids": list(self.item_ids),
            "count": len(self.item_ids),
        }


def matches_of(entry: Any, preferences: dict[str, list[str]] | None) -> list[TagMatch]:
    """这条上新命中了用户哪几个 tag。逐轴求交，顺序按轴表声明。"""
    preferences = preferences or {}
    found: list[TagMatch] = []
    for axis in AXES:
        chosen = preferences.get(axis.key) or []
        if not chosen:
            continue
        for hit in axis.match(entry, list(chosen)):
            found.append(TagMatch(axis=axis.key, label=axis.label, tag=hit))
    return found


def group_by_axis(entry_tags: list[tuple[str, list[TagMatch]]]) -> list[Group]:
    """按轴分组：同一家店的一次上新会同时命中 shops 和 styles，两边都要出现。

    ``entry_tags`` 是 ``[(条目 id, 命中的 tag 列表)]``，顺序即推荐顺序——
    分组成员沿用这个顺序，不再重排，否则组内顺序和总榜顺序会打架。
    """
    buckets: dict[str, dict] = {}
    order: list[str] = []

    for entry_id, tags in entry_tags:
        for tag in tags:
            bucket = buckets.get(tag.axis)
            if bucket is None:
                bucket = {"label": tag.label, "tags": [], "item_ids": []}
                buckets[tag.axis] = bucket
                order.append(tag.axis)
            if tag.tag not in bucket["tags"]:
                bucket["tags"].append(tag.tag)
            if entry_id not in bucket["item_ids"]:
                bucket["item_ids"].append(entry_id)

    return [
        Group(
            axis=axis,
            label=buckets[axis]["label"],
            tags=buckets[axis]["tags"],
            item_ids=buckets[axis]["item_ids"],
        )
        for axis in order
    ]


def knowledge_query(entry: Any) -> str:
    """拿什么去查知识库。

    用**品类**（JSK / OP / SK）而不是系列名：知识库里讲的是这些东西。
    实测「秋日私语」这类系列名命中 0 条，而「JSK」稳定命中术语条目。
    """
    items = [str(item) for item in (getattr(entry, "items", []) or []) if str(item).strip()]
    return " ".join(items)
