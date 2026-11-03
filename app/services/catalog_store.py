"""Lolibrary 档案缓存的读取与索引。

档案是第三方数据，放在 `data/lolibrary/catalog.jsonl`（不进 git、可重建）。
这里只负责读与建索引；抓取与写入在 `scripts/import_lolibrary.py`。

按 `(路径, mtime)` 做缓存：档案更新后下一次请求自动换新，不需要重启服务。
"""

from __future__ import annotations

import json
import threading
from typing import Any

from app.config import AppSettings, settings as default_settings
from app.domain import catalog as catalog_domain

_lock = threading.Lock()
_cache: dict[str, Any] = {"path": "", "mtime": 0.0, "index": {}}


def load_records(app: AppSettings | None = None) -> list[dict]:
    app = app or default_settings
    path = app.lolibrary_catalog_path
    if not path.exists():
        return []

    records: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def index(app: AppSettings | None = None) -> dict:
    """返回档案索引；没有档案时返回空 dict。"""
    app = app or default_settings
    path = app.lolibrary_catalog_path
    if not path.exists():
        return {}
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}

    with _lock:
        if _cache["path"] == str(path) and _cache["mtime"] == mtime:
            return dict(_cache["index"])
        records = load_records(app)
        result = catalog_domain.build_index(records)
        result["count"] = len(records)
        _cache.update(path=str(path), mtime=mtime, index=result)
        return dict(result)


def clear_cache() -> None:
    with _lock:
        _cache.update(path="", mtime=0.0, index={})
