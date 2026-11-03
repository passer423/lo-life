"""知识库入库测试。

关键点：**id 重复必须被拦下来**。上游的 chunk_id 是 ``post_id#序号``，
两条知识在同一 id 下会互相覆盖，而向量库不会报错——这是最难查的一类问题，
所以宁可在入库阶段直接崩掉。
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
for candidate in (ROOT, SCRIPTS):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from app import bridge                      # noqa: E402
from app.config import AppSettings          # noqa: E402
from ingest_kb import ingest, split_front_matter   # noqa: E402

SAMPLE = """id: term-x
title: 什么是 X
category: 术语
tags: 术语,新手
---
X 是一段足够长的正文，用来确认标题会被并进检索文本里，并且长度能超过入库下限。
"""


def write_kb(root: Path, files: dict[str, str]) -> Path:
    kb_dir = root / "content" / "kb"
    kb_dir.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (kb_dir / name).write_text(text, encoding="utf-8")
    return kb_dir


def make_settings(root: Path) -> AppSettings:
    """data 与 content 都指到临时目录——否则会读到真实知识库（见 config.py 注释）。"""
    return AppSettings(data_dir=root / "data", content_dir=root / "content")


def test_split_front_matter():
    meta, body = split_front_matter(SAMPLE)
    assert meta["id"] == "term-x"
    assert meta["title"] == "什么是 X"
    assert meta["tags"] == "术语,新手"
    assert body.startswith("X 是一段足够长的正文")
    assert "---" not in body


def test_split_front_matter_without_header_keeps_whole_text():
    meta, body = split_front_matter("就是一段没有头的正文。")
    assert meta == {}
    assert body == "就是一段没有头的正文。"


def test_ingest_writes_posts_and_merges_title_into_text(workdir):
    write_kb(workdir, {"a.md": SAMPLE})
    target = ingest(make_settings(workdir), verbose=False)

    assert target.exists()
    lines = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 1
    assert lines[0]["id"] == "term-x"
    assert lines[0]["author"] == "知识库"
    # 标题必须进正文，否则「JSK」这类关键词 BM25 打不到
    assert lines[0]["text"].startswith("什么是 X")


def test_duplicate_ids_are_rejected(workdir):
    write_kb(workdir, {"a.md": SAMPLE, "b.md": SAMPLE.replace("什么是 X", "另一个标题")})
    with pytest.raises(SystemExit) as exc:
        ingest(make_settings(workdir), verbose=False)
    assert "id 重复" in str(exc.value)


def test_short_entries_are_skipped(workdir):
    write_kb(workdir, {"short.md": "id: too-short\ntitle: 短\n---\n太短了\n"})
    with pytest.raises(SystemExit) as exc:
        ingest(make_settings(workdir), verbose=False)
    assert "没有可入库" in str(exc.value)


def test_empty_kb_fails_loudly(workdir):
    (workdir / "content" / "kb").mkdir(parents=True, exist_ok=True)
    with pytest.raises(SystemExit) as exc:
        ingest(make_settings(workdir), verbose=False)
    assert "知识库是空的" in str(exc.value)


def test_real_kb_directory_is_valid(workdir):
    """真实知识库必须能通过入库——防止手滑把某条的 id 改重。

    只**读**真实知识库、把副本写进临时目录：测试不往 data/ 里写任何东西。
    """
    real_kb = ROOT / "content" / "kb"
    if not real_kb.exists():
        pytest.skip("真实知识库目录不存在")
    shutil.copytree(real_kb, workdir / "content" / "kb")

    target = ingest(make_settings(workdir), verbose=False)
    lines = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
    assert len(lines) >= 10, f"知识库条目太少（{len(lines)}）"
    ids = [line["id"] for line in lines]
    assert len(ids) == len(set(ids))
    assert all(len(line["text"]) >= 18 for line in lines)


# --------------------------------------------------------------------- 多意图检索


class _FakeChunk:
    def __init__(self, chunk_id: str):
        self.chunk_id = chunk_id
        self.post_id = chunk_id.split("#", 1)[0]
        self.text = f"正文 {chunk_id}"
        self.url = ""
        self.author = "知识库"
        self.created_at = ""
        self.position = 0
        self.total = 1


class _FakeHit:
    def __init__(self, chunk_id: str):
        self.chunk = _FakeChunk(chunk_id)
        self.score = 1.0
        self.source = "hybrid"


class _FakeRetriever:
    def __init__(self, mapping: dict[str, list[str]]):
        self.mapping = mapping
        self.queries: list[tuple[str, int]] = []

    def search(self, query: str, top_k: int = 1) -> list[_FakeHit]:
        self.queries.append((query, top_k))
        return [_FakeHit(chunk_id) for chunk_id in self.mapping.get(query, [])][:top_k]


class _FakeBundle:
    def __init__(self, retriever: _FakeRetriever):
        self.retriever = retriever


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("烫金和蕾丝怎么洗", ["烫金怎么洗", "蕾丝怎么洗"]),
        ("JSK 和 OP 有什么区别", []),
        ("JSK 和 OP 哪个更适合新手", []),
        ("JSK、SK、OP 分别是什么", ["JSK 是什么", "SK 是什么", "OP 是什么"]),
        ("烫金、亮片和蕾丝怎么洗", ["烫金怎么洗", "亮片怎么洗", "蕾丝怎么洗"]),
        ("小个子和大码怎么选裙长", ["小个子怎么选裙长", "大码怎么选裙长"]),
        ("烫金可以机洗和蕾丝可以机洗吗", ["烫金可以机洗", "蕾丝可以机洗吗"]),
        ("和服和和风怎么区分", ["和服怎么区分", "和风怎么区分"]),
        ("蕾丝怎么洗", []),
    ],
)
def test_split_multi_intent(question: str, expected: list[str]):
    assert bridge._split_multi_intent(question) == expected


def test_search_hits_merges_intents_and_deduplicates():
    retriever = _FakeRetriever(
        {
            "烫金和蕾丝怎么洗": ["care-jinshu#0"],
            "烫金怎么洗": ["care-jinshu#0"],
            "蕾丝怎么洗": ["care-lace#0"],
        }
    )
    hits = bridge._search_hits("烫金和蕾丝怎么洗", _FakeBundle(retriever), 1)

    assert [hit.chunk.chunk_id for hit in hits] == ["care-jinshu#0", "care-lace#0"]
    assert retriever.queries == [
        ("烫金和蕾丝怎么洗", 1),
        ("烫金怎么洗", 1),
        ("蕾丝怎么洗", 1),
    ]


def test_search_hits_keeps_comparison_question_whole():
    retriever = _FakeRetriever({"JSK 和 OP 有什么区别": ["term-jsk-op#0"]})
    hits = bridge._search_hits("JSK 和 OP 有什么区别", _FakeBundle(retriever), 1)

    assert [hit.chunk.chunk_id for hit in hits] == ["term-jsk-op#0"]
    assert retriever.queries == [("JSK 和 OP 有什么区别", 1)]


def test_search_hits_without_intents_uses_top_k():
    retriever = _FakeRetriever({"蕾丝怎么洗": ["a#0", "b#0", "c#0"]})
    hits = bridge._search_hits("蕾丝怎么洗", _FakeBundle(retriever), 3)

    assert [hit.chunk.chunk_id for hit in hits] == ["a#0", "b#0", "c#0"]
    assert retriever.queries == [("蕾丝怎么洗", 3)]
