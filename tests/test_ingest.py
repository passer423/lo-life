"""离线入库管线的测试：不联网、不需要 key。

覆盖的是"失败时草稿怎么处理"这条主线：

* 没模型 → partial 草稿，结构化字段保留；
* 模型失败 → extract_failed 草稿，raw 快照保留，可重跑；
* 引文对不上 → 该字段丢弃，状态降为 partial；
* 重复 id / 非法 id → rejected 记录，不能审批；
* 审批二次校验：缺字段拒绝、已有条目拒绝、--overwrite 才覆盖；
* 手工补全 partial 草稿后可以审批。

`extractor` 都是注入的假函数，所以用例不依赖网络，也不依赖 LLM_API_KEY。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import AppSettings
from app.domain import ingest as ingest_domain
from app.services import ingest_pipeline
from app.services.ingest_extract import ExtractFailed

PAGE = """<html><head>
<meta property="og:title" content="Dressy Ribbon Poodle ラウンド切替ジャンパースカート">
<meta property="og:image" content="https://example.com/a.jpg">
<meta property="og:url" content="https://example.com/item">
<meta property="product:price:amount" content="63800">
</head><body>
<h1>Dressy Ribbon Poodle ラウンド切替ジャンパースカート</h1>
<p>発売日：2026年9月12日</p>
<p>¥63,800</p>
<p>JSK／甜系</p>
<p>予約販売</p>
</body></html>"""

ENTRY_ID = "2026-09-ap-poodle"


@pytest.fixture()
def app(workdir) -> AppSettings:
    content = workdir / "content"
    (content / "newarrivals").mkdir(parents=True, exist_ok=True)
    shops = content / "shops"
    shops.mkdir(parents=True, exist_ok=True)
    (shops / "ap.json").write_text(
        json.dumps(
            {
                "brand": "ap",
                "brand_name": "AP",
                "shop_url": "https://example.com",
                "currency": "JPY",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return AppSettings(data_dir=workdir / "data", content_dir=content)


def model_ok(_text: str) -> dict:
    return {
        "series": "Dressy Ribbon Poodle ラウンド切替ジャンパースカート",
        "release_date": "2026-09-12",
        "price_range": "63800",
        "items": ["JSK"],
        "styles": ["甜系"],
        "note": "予約販売",
        "aliases": [],
        "quotes": {
            "series": "Dressy Ribbon Poodle ラウンド切替ジャンパースカート",
            "release_date": "2026年9月12日",
            "price_range": "¥63,800",
            "items": "JSK",
            "styles": "甜系",
            "note": "予約販売",
        },
    }


def _draft(result: dict) -> dict:
    return json.loads(Path(result["draft_path"]).read_text(encoding="utf-8"))


def _ingest(app: AppSettings, extractor=None, *, allow_model=True, proposed_id=ENTRY_ID):
    return ingest_pipeline.ingest_from_html(
        PAGE,
        page_url="https://example.com/item",
        brand="ap",
        app=app,
        proposed_id=proposed_id,
        extractor=extractor,
        allow_model=allow_model,
    )


# --------------------------------------------------------------------- 正常路径


def test_ready_draft_and_approve(app):
    result = _ingest(app, model_ok)
    assert result["status"] == ingest_domain.READY

    draft = _draft(result)
    assert draft["fields"]["series"] == "Dressy Ribbon Poodle ラウンド切替ジャンパースカート"
    assert draft["fields"]["release_date"] == "2026-09-12"
    assert draft["fields"]["image"] == "https://example.com/a.jpg"
    assert draft["fields"]["link"] == "https://example.com/item"
    assert draft["fields"]["price_range"] == "63800"
    assert draft["fields"]["currency"] == "JPY"          # 来自店铺表，不靠模型猜
    assert draft["_evidence"]["series"]["kind"] == "model"
    assert draft["_evidence"]["price_range"]["kind"] == "structural"

    approved = ingest_pipeline.approve_draft(result["draft_path"], app=app)
    assert approved["ok"] is True
    entry = json.loads((app.newarrivals_dir / f"{ENTRY_ID}.json").read_text(encoding="utf-8"))
    assert entry["series"] == draft["fields"]["series"]
    assert entry["sample"] is False
    assert entry["source"].startswith("依据：https://example.com/item")


def test_draft_never_writes_content(app):
    result = _ingest(app, model_ok)
    assert not any(app.newarrivals_dir.glob("*.json"))
    assert result["draft_path"].endswith(".json")


# --------------------------------------------------------------------- 模型失败


def test_no_model_keeps_structural_fields_and_is_partial(app):
    result = _ingest(app, allow_model=False)
    assert result["status"] == ingest_domain.PARTIAL
    assert "series" in result["missing"]
    assert "release_date" in result["missing"]

    draft = _draft(result)
    assert draft["fields"]["image"] == "https://example.com/a.jpg"
    assert draft["fields"]["price_range"] == "63800"

    refused = ingest_pipeline.approve_draft(result["draft_path"], app=app)
    assert refused["ok"] is False
    assert any("series" in problem for problem in refused["problems"])


def test_model_failure_writes_extract_failed_draft(app):
    def boom(_text: str) -> dict:
        raise ExtractFailed("模型调用失败：TimeoutError: 超时")

    result = _ingest(app, boom)
    assert result["status"] == ingest_domain.EXTRACT_FAILED
    assert result["errors"]

    draft = _draft(result)
    assert draft["status"] == ingest_domain.EXTRACT_FAILED
    assert draft["_errors"]
    assert result["raw_path"]

    refused = ingest_pipeline.approve_draft(result["draft_path"], app=app)
    assert refused["ok"] is False


def test_bad_quote_drops_field_and_downgrades(app):
    def liar(text: str) -> dict:
        data = model_ok(text)
        data["series"] = "编造的系列名"
        data["quotes"]["series"] = "编造的系列名"
        return data

    result = _ingest(app, liar)
    assert result["status"] == ingest_domain.PARTIAL
    assert "series" in result["missing"]
    assert any("series" in warning and "引文" in warning for warning in result["warnings"])
    assert _draft(result)["fields"]["series"] == ""


def test_retry_reuses_raw_snapshot_and_bumps_attempts(app):
    def boom(_text: str) -> dict:
        raise ExtractFailed("模型调用失败：TimeoutError")

    failed = _ingest(app, boom)
    assert failed["status"] == ingest_domain.EXTRACT_FAILED
    raws_before = list((app.data_dir / "ingest" / "raw").glob("*.html"))
    assert len(raws_before) == 1

    retried = ingest_pipeline.retry_draft(failed["draft_path"], app=app, extractor=model_ok)
    assert retried["status"] == ingest_domain.READY
    assert len(list((app.data_dir / "ingest" / "raw").glob("*.html"))) == 1
    assert _draft(retried)["_attempts"] == 2


# --------------------------------------------------------------------- 拒绝与审批


def test_duplicate_id_is_rejected_without_draft(app):
    existing = {
        "id": ENTRY_ID, "brand": "ap", "brand_name": "AP",
        "series": "已有条目", "release_date": "2026-09-12",
    }
    (app.newarrivals_dir / f"{ENTRY_ID}.json").write_text(
        json.dumps(existing, ensure_ascii=False), encoding="utf-8"
    )
    result = _ingest(app, model_ok)
    assert result["status"] == ingest_domain.REJECTED
    assert result["draft_path"] == ""
    assert result["rejected_path"]
    assert "id 已存在" in result["reasons"][0]


def test_unsafe_id_never_escapes_content_dir(app):
    result = _ingest(app, model_ok, proposed_id="../evil")
    assert result["status"] == ingest_domain.REJECTED
    assert result["draft_path"] == ""
    assert not (app.newarrivals_dir.parent / "evil.json").exists()


def test_manual_completion_makes_partial_approvable(app):
    result = _ingest(app, allow_model=False)
    draft_path = result["draft_path"]
    draft = _draft(result)
    draft["fields"]["series"] = "手工补的系列名"
    draft["fields"]["release_date"] = "2026-09-12"
    Path(draft_path).write_text(
        json.dumps(draft, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    approved = ingest_pipeline.approve_draft(draft_path, app=app)
    assert approved["ok"] is True
    entry = json.loads((app.newarrivals_dir / f"{ENTRY_ID}.json").read_text(encoding="utf-8"))
    assert entry["series"] == "手工补的系列名"


def test_approve_refuses_existing_without_overwrite(app):
    result = _ingest(app, model_ok)
    assert ingest_pipeline.approve_draft(result["draft_path"], app=app)["ok"] is True

    again = ingest_pipeline.approve_draft(result["draft_path"], app=app)
    assert again["ok"] is False
    assert any("--overwrite" in problem for problem in again["problems"])

    draft_path = result["draft_path"]
    draft = _draft(result)
    draft["fields"]["note"] = "改过备注"
    Path(draft_path).write_text(
        json.dumps(draft, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    overwritten = ingest_pipeline.approve_draft(draft_path, app=app, overwrite=True)
    assert overwritten["ok"] is True
    assert any("note" in line for line in overwritten["diff"])
    entry = json.loads((app.newarrivals_dir / f"{ENTRY_ID}.json").read_text(encoding="utf-8"))
    assert entry["note"] == "改过备注"


# --------------------------------------------------------------------- 纯函数


def test_page_facts_prefers_open_graph():
    facts = ingest_domain.page_facts(PAGE, "https://example.com/item")
    assert facts.title.startswith("Dressy Ribbon Poodle")
    assert facts.image == "https://example.com/a.jpg"
    assert facts.link == "https://example.com/item"
    assert facts.price == "63800"


def test_quote_must_match_verbatim():
    assert ingest_domain.quote_found("2026年9月12日", "発売日：2026年9月12日")
    # 空白不敏感：两边的空格都先归一化掉
    assert ingest_domain.quote_found("2026年9月12日", "2026 年 9 月 12 日")
    assert ingest_domain.quote_found("2026 年 9 月 12 日", "2026年9月12日")
    # 改写/补全/翻译都不算
    assert not ingest_domain.quote_found("9月12日发售", "2026年9月12日")
    assert not ingest_domain.quote_found("2026年9月中旬", "2026年9月12日")


def test_vague_date_is_not_guessed():
    assert ingest_domain.normalize_date("2026年9月12日") == "2026-09-12"
    assert ingest_domain.normalize_date("10月下旬") == ""
    assert ingest_domain.normalize_date("2026-13-40") == ""
