"""洗护建议：确定性规则匹配，不走模型。

为什么不用 RAG：洗护是「面料 + 工艺 → 建议」的规则问题。检索会给出相似段落，
但用户要的是一个明确的"能不能机洗"。规则匹配的答案稳定、可复现、可审计
（每条建议都能指回是哪条规则、哪个知识库条目）。

一条红线：**没有匹配到规则就说不知道**，给一句"按洗标"，而不是套用最接近的一条。
洗坏一条裙子的代价远大于多查一次洗标。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

SEVERITY_ORDER = {"forbid": 0, "warn": 1, "tip": 2}
SEVERITY_LABEL = {"forbid": "禁止项", "warn": "注意事项", "tip": "建议"}


class RulesUnavailable(RuntimeError):
    """规则文件缺失或损坏。"""


@dataclass(slots=True)
class Rule:
    id: str
    title: str
    severity: str
    advice: list[str]
    tags: dict[str, list[str]]
    blocks_machine_wash: bool
    ref: str

    def matches(self, supplied: dict[str, set[str]]) -> bool:
        """规则里的每个标签组都必须被用户输入命中，才算适用。

        用"每组都要命中"而不是"命中任意"，是为了避免「蕾丝 + 烫金」时只有一条规则生效：
        这样的规则允许把"烫金必禁机洗"和"蕾丝要装洗衣袋"写成两条，各管一件事。
        """
        for group, wanted in self.tags.items():
            if not wanted:
                continue
            have = supplied.get(group) or set()
            if not have & {_norm(item) for item in wanted}:
                return False
        return True


def _norm(value: str) -> str:
    return str(value or "").strip().lower()


# 模型不一定照抄选项标签：问「红色」时它可能传「红色易掉色」，而规则里的标签是
# 「红色/酒红易掉色」。这里只把**明确的近义说法**归一到标准标签；认不出来的值
# 原样保留，仍然走「没有匹配到规则就说不知道」那条红线。
_NOTE_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("红色/酒红易掉色", ("红色", "酒红")),
    ("白色/米白易泛黄", ("白色", "米白", "泛黄")),
    ("彩色/拼色易串色", ("彩色", "拼色", "串色")),
    ("深色易掉色", ("深色", "掉色")),
    ("首次洗", ("首次",)),
    ("只能干洗", ("只能干洗", "需要干洗", "必须干洗")),
    ("有钢圈裙撑", ("钢圈", "裙撑")),
)


def _canonical_notes(values: Iterable[str]) -> set[str]:
    notes: set[str] = set()
    for value in values:
        for part in re.split(r"[、，,；;]+", str(value or "")):
            text = _norm(part)
            if not text:
                continue
            for canonical, keywords in _NOTE_ALIASES:
                if canonical in text or any(keyword in text for keyword in keywords):
                    notes.add(_norm(canonical))
                    break
            else:
                notes.add(text)
    return notes


def load_rules(care_dir: Path) -> tuple[list[Rule], dict[str, list[str]]]:
    """返回 (规则列表, 可选标签)。标签清单给前端渲染成可点的按钮。"""
    path = care_dir / "rules.json"
    if not path.exists():
        raise RulesUnavailable(f"找不到洗护规则：{path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RulesUnavailable(f"洗护规则不是合法 JSON：{path} —— {exc}") from exc

    rules: list[Rule] = []
    for item in raw.get("rules") or []:
        rules.append(
            Rule(
                id=str(item.get("id") or ""),
                title=str(item.get("title") or ""),
                severity=str(item.get("severity") or "tip"),
                advice=[str(line) for line in (item.get("advice") or [])],
                tags={group: [str(v) for v in values] for group, values in (item.get("tags") or {}).items()},
                blocks_machine_wash=bool(item.get("blocks_machine_wash")),
                ref=str(item.get("ref") or ""),
            )
        )
    return rules, {group: [str(v) for v in values] for group, values in (raw.get("options") or {}).items()}


def advise(
    rules: list[Rule],
    *,
    materials: Iterable[str] = (),
    crafts: Iterable[str] = (),
    notes: Iterable[str] = (),
) -> dict[str, Any]:
    """按面料/工艺/其他标签匹配规则，输出结论 + 分条建议 + 来源。"""
    supplied: dict[str, set[str]] = {
        "materials": {_norm(item) for item in materials if item},
        "crafts": {_norm(item) for item in crafts if item},
        "notes": _canonical_notes(notes),
    }
    if not any(supplied.values()):
        raise ValueError("至少要选一项面料、工艺或属性")

    matched = [rule for rule in rules if rule.matches(supplied)]
    if not matched:
        return {
            "known": False,
            "machine_washable": None,
            "severity": "",
            "headline": "没有匹配到规则",
            "advice": ["这类组合还没有收录。请以衣服上的洗涤标识为准，或先把这条材质补进 content/care/rules.json。"],
            "refs": [],
            "matched": [],
        }

    matched.sort(key=lambda rule: (SEVERITY_ORDER.get(rule.severity, 9), rule.id))

    blocked = [rule for rule in matched if rule.blocks_machine_wash]
    machine_washable = not blocked

    if blocked:
        headline = "不建议机洗"
        if len(blocked) == 1:
            headline = f"不建议机洗（{blocked[0].title}）"
    else:
        headline = "可以机洗，但要按下面几条来"
        # 只在没有禁止项时才提机洗细节，避免和"禁止机洗"自相矛盾
        machine_hints = [rule for rule in matched if rule.severity == "tip"]
        if not machine_hints:
            headline = "没有禁止项，按常规轻柔处理即可"

    advice: list[str] = []
    for rule in matched:
        for line in rule.advice:
            if line not in advice:               # 多条规则可能给出同一句
                advice.append(line)

    refs = [rule.ref for rule in matched if rule.ref]
    return {
        "known": True,
        "machine_washable": machine_washable,
        "severity": matched[0].severity,
        "severity_label": SEVERITY_LABEL.get(matched[0].severity, ""),
        "headline": headline,
        "advice": advice,
        "refs": list(dict.fromkeys(refs)),
        "matched": [
            {"id": rule.id, "title": rule.title, "severity": rule.severity, "ref": rule.ref}
            for rule in matched
        ],
    }
