"""草稿确认流程：草稿只有显式 --approve 才进入正式内容。"""

from __future__ import annotations

import json

import pytest

DRAFT = {
    "id": "2026-11-baby-test-handle",
    "brand": "baby",
    "brand_name": "Baby",
    "series": "测试草稿裙",
    "release_date": "2026-11-01",
    "price_range": "39800",
    "currency": "JPY",
    "items": ["JSK"],
    "styles": [],
    "image": "",
    "link": "https://store.babyssb.co.jp/products/test-handle",
    "note": "草稿",
    "source": "依据：测试 products.json",
    "sample": False,
}


def _setup(workdir):
    from app.config import AppSettings

    content = workdir / "content"
    drafts = content / "_drafts" / "newarrivals"
    drafts.mkdir(parents=True, exist_ok=True)
    (content / "newarrivals").mkdir(parents=True, exist_ok=True)
    return content, drafts, AppSettings(data_dir=workdir / "data", content_dir=content)


def test_approve_moves_valid_draft(workdir, monkeypatch):
    import scripts.scan_baby as scan

    content, drafts, app = _setup(workdir)
    path = drafts / f"{DRAFT['id']}.json"
    path.write_text(json.dumps(DRAFT, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(scan, "settings", app)

    assert scan.approve(DRAFT["id"]) == 0
    assert (content / "newarrivals" / f"{DRAFT['id']}.json").exists()
    assert not path.exists()


def test_approve_rejects_invalid_draft(workdir, monkeypatch):
    import scripts.scan_baby as scan

    content, drafts, app = _setup(workdir)
    bad = {**DRAFT, "id": "bad-draft", "series": ""}
    (drafts / "bad-draft.json").write_text(
        json.dumps(bad, ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setattr(scan, "settings", app)

    assert scan.approve("bad-draft") == 1
    assert not (content / "newarrivals" / "bad-draft.json").exists()
    assert (drafts / "bad-draft.json").exists()          # 不合格的草稿留在原处
