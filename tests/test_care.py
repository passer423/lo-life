"""洗护规则引擎测试。

三条口径：

* **禁止项一票否决**：只要有一条 `blocks_machine_wash`，结论就是不能机洗；
* **多标签要能叠加**：「蕾丝 + 烫金」必须同时命中两条规则，而不是只有一条生效；
* **没匹配到就说不知道**，不套用最接近的一条——这是和知识问答同一条原则。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.domain import care

ROOT = Path(__file__).resolve().parents[1]


def write_rules(root: Path, rules: list[dict], options: dict | None = None) -> Path:
    directory = root / "care"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "rules.json").write_text(
        json.dumps({"version": "test", "options": options or {}, "rules": rules}, ensure_ascii=False),
        encoding="utf-8",
    )
    return directory


RULE_FORBID = {
    "id": "jinshu",
    "title": "烫金会脱落",
    "severity": "forbid",
    "blocks_machine_wash": True,
    "tags": {"crafts": ["烫金"]},
    "advice": ["不要机洗。", "冷水手洗。"],
    "ref": "care-jinshu",
}
RULE_LACE = {
    "id": "leisi",
    "title": "蕾丝怕勾",
    "severity": "warn",
    "blocks_machine_wash": False,
    "tags": {"materials": ["蕾丝"]},
    "advice": ["装洗衣袋。"],
    "ref": "care-lace",
}
RULE_TIP = {
    "id": "mian",
    "title": "棉耐洗",
    "severity": "tip",
    "blocks_machine_wash": False,
    "tags": {"materials": ["棉"]},
    "advice": ["按常规洗涤。"],
    "ref": "",
}


@pytest.fixture()
def rules(workdir):
    loaded, _ = care.load_rules(write_rules(workdir, [RULE_FORBID, RULE_LACE, RULE_TIP]))
    return loaded


def test_forbid_blocks_machine_wash(rules):
    result = care.advise(rules, crafts=["烫金"])
    assert result["known"] is True
    assert result["machine_washable"] is False
    assert result["severity"] == "forbid"
    assert "不建议机洗" in result["headline"]
    assert result["advice"] == ["不要机洗。", "冷水手洗。"]


def test_warn_does_not_block(rules):
    result = care.advise(rules, materials=["蕾丝"])
    assert result["machine_washable"] is True
    assert result["severity"] == "warn"
    assert "装洗衣袋。" in result["advice"]


def test_tags_stack_across_rule_groups(rules):
    """「蕾丝 + 烫金」两条规则都要命中——这就是每组都要匹配的理由。"""
    result = care.advise(rules, materials=["蕾丝"], crafts=["烫金"])
    matched = {item["id"] for item in result["matched"]}
    assert matched == {"jinshu", "leisi"}
    assert result["machine_washable"] is False      # 一条禁止就够
    assert "装洗衣袋。" in result["advice"]          # 但蕾丝的建议不能被吞掉
    assert result["severity"] == "forbid"           # 最严重的排最前


def test_advice_is_deduplicated(workdir):
    duplicate = dict(RULE_LACE, id="leisi-2", advice=["装洗衣袋。"])
    loaded, _ = care.load_rules(write_rules(workdir, [RULE_LACE, duplicate]))
    result = care.advise(loaded, materials=["蕾丝"])
    assert result["advice"] == ["装洗衣袋。"]


def test_unknown_combination_says_so(rules):
    result = care.advise(rules, materials=["神秘面料"])
    assert result["known"] is False
    assert result["machine_washable"] is None
    assert result["headline"] == "没有匹配到规则"
    assert "洗涤标识" in result["advice"][0]


def test_empty_selection_is_rejected(rules):
    with pytest.raises(ValueError, match="至少要选一项"):
        care.advise(rules, materials=[], crafts=[], notes=[])


def test_refs_are_returned_and_unique(rules):
    result = care.advise(rules, materials=["蕾丝"], crafts=["烫金"])
    assert result["refs"] == ["care-jinshu", "care-lace"]      # 空 ref 被丢掉，顺序稳定


def test_missing_file_is_explicit(workdir):
    with pytest.raises(care.RulesUnavailable, match="找不到洗护规则"):
        care.load_rules(workdir / "nope")


def test_broken_json_is_explicit(workdir):
    directory = workdir / "care"
    directory.mkdir(parents=True)
    (directory / "rules.json").write_text("{ nope", encoding="utf-8")
    with pytest.raises(care.RulesUnavailable, match="不是合法 JSON"):
        care.load_rules(directory)


def test_real_rules_load_and_are_complete():
    """真实规则库必须每条都有 id / 标题 / 建议，且 severity 合法。"""
    loaded, options = care.load_rules(ROOT / "content" / "care")
    assert len(loaded) >= 8, f"规则太少（{len(loaded)}）"
    ids = [rule.id for rule in loaded]
    assert len(ids) == len(set(ids)), "规则 id 有重复"
    for rule in loaded:
        assert rule.id and rule.title and rule.advice
        assert rule.severity in care.SEVERITY_ORDER
    assert options.get("materials") and options.get("crafts")


def test_real_rules_cover_added_materials():
    """新增材质必须能选、能匹配，并指回知识库条目。"""
    loaded, options = care.load_rules(ROOT / "content" / "care")
    for material in ("网纱", "欧根纱", "醋酸", "人造丝", "丝绒", "天鹅绒"):
        assert material in options["materials"]
        result = care.advise(loaded, materials=[material])
        assert result["known"] is True
        assert "care-lace" in result["refs"]

    assert care.advise(loaded, materials=["醋酸"])["machine_washable"] is False
    assert care.advise(loaded, materials=["丝绒"])["machine_washable"] is False


def test_real_rules_cover_color_notes():
    """颜色问题走 notes 标签，不能只写在知识库正文里。"""
    loaded, options = care.load_rules(ROOT / "content" / "care")
    for note in ("红色/酒红易掉色", "白色/米白易泛黄", "彩色/拼色易串色"):
        assert note in options["notes"]
        result = care.advise(loaded, notes=[note])
        assert result["known"] is True
        assert "care-lace" in result["refs"]


def test_note_aliases_map_to_standard_rules():
    """模型传「红色易掉色」这类近义说法时，也要能命中标准标签。"""
    loaded, _ = care.load_rules(ROOT / "content" / "care")
    cases = (
        ("红色易掉色", "dark-fade"),
        ("深色掉色", "dark-fade"),
        ("白色泛黄", "white-yellow"),
        ("拼色串色", "color-bleed"),
    )
    for note, rule_id in cases:
        result = care.advise(loaded, notes=[note])
        assert result["known"] is True
        assert any(item["id"] == rule_id for item in result["matched"])
