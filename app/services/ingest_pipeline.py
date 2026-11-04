"""离线入库的 I/O 编排：抓取、快照、草稿、审批。

## 目录约定

```text
data/ingest/
├── raw/       抓到的页面快照（重跑抽取只读这里，不重新抓）
├── drafts/    可审批草稿（含 _evidence / _missing / _errors）
├── rejected/  不能审批的记录（重复 id、来源不合规…）
└── runs.jsonl 每次尝试一行，排查"为什么这条没进 content"
```

`content/` 只有 `approve_draft()` 一个入口能写；其余函数最多写到 `data/`。

## 失败不丢草稿

只要有一个可核实字段（图、链接、价格），就写草稿；模型失败只影响字段覆盖度，
草稿状态会是 `extract_failed` 或 `partial`，审批时会被挡下。`--retry` 从同一份
raw 快照重跑，不重新抓页面。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

from app.config import AppSettings
from app.config import settings as default_settings
from app.domain import ingest as ingest_domain
from app.domain import newarrivals as na
from app.domain import shops as shop_table
from app.services import imagefetch
from app.services.ingest_extract import (
    ExtractFailed,
    ExtractUnavailable,
)
from app.services.ingest_extract import (
    extract as llm_extract,
)

logger = logging.getLogger(__name__)

Extractor = Callable[[str], dict]


def ingest_root(app: AppSettings | None = None) -> Path:
    return Path((app or default_settings).data_dir) / "ingest"


def raw_dir(app: AppSettings | None = None) -> Path:
    return ingest_root(app) / "raw"


def drafts_dir(app: AppSettings | None = None) -> Path:
    return ingest_root(app) / "drafts"


def rejected_dir(app: AppSettings | None = None) -> Path:
    return ingest_root(app) / "rejected"


def _atomic_write_json(path: Path, payload: dict) -> None:
    """先写临时文件再替换：中途断电不会留下半份 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"找不到文件：{path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"不是合法 JSON：{path}（{exc}）") from exc
    if not isinstance(data, dict):
        raise ValueError(f"文件内容不是 JSON 对象：{path}")
    return data


def _file_slug(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return text or datetime.now().strftime("page-%Y%m%d-%H%M%S-%f")


def _save_raw(html: str, *, label: str, app: AppSettings) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    path = raw_dir(app) / f"{stamp}-{_file_slug(label)}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return path


def _existing(app: AppSettings) -> tuple[list[str], list[tuple[str, str]]]:
    entries, problems = na.load_newarrivals(app.newarrivals_dir)
    for problem in problems:
        logger.warning("上新数据有问题：%s", problem)
    return [entry.id for entry in entries], [(entry.brand, entry.series) for entry in entries]


def _shop_info(app: AppSettings, brand: str) -> tuple[str, str]:
    """返回 ``(brand_name, 默认币种)``；没登记就用 brand 和 CNY。"""
    shops, problems = shop_table.load_shops(app.shops_dir)
    for problem in problems:
        logger.warning("店铺表有问题：%s", problem)
    shop = shops.get(brand)
    if shop is None:
        return brand, "CNY"
    return shop.brand_name or brand, shop.currency or "CNY"


def _default_extractor(app: AppSettings, brand: str) -> Extractor:
    brand_name, _currency = _shop_info(app, brand)

    def run(text: str) -> dict:
        return llm_extract(text, app=app, brand=brand, brand_name=brand_name)

    return run


def _extract(
    text: str,
    *,
    app: AppSettings,
    brand: str,
    extractor: Extractor | None,
    allow_model: bool,
) -> tuple[dict, str, list[str]]:
    """返回 ``(模型结果, 状态, 错误列表)``；状态是 ok / skipped / failed。"""
    if not allow_model:
        return {}, "skipped", []
    if extractor is None and not app.llm_enabled:
        return {}, "skipped", []

    runner = extractor or _default_extractor(app, brand)
    try:
        data = runner(text)
    except ExtractUnavailable as exc:
        return {}, "skipped", [str(exc)]
    except ExtractFailed as exc:
        logger.warning("模型抽取失败：%s", exc)
        return {}, "failed", [str(exc)]
    except Exception as exc:  # noqa: BLE001 - 任何未预期异常都降级为失败草稿
        logger.warning("模型抽取异常：%s: %s", type(exc).__name__, exc)
        return {}, "failed", [f"{type(exc).__name__}: {exc}"]
    if not isinstance(data, dict):
        return {}, "failed", ["模型返回的不是 JSON 对象"]
    return data, "ok", []


def _ingest_core(
    html: str,
    *,
    page_url: str,
    brand: str,
    app: AppSettings,
    proposed_id: str = "",
    raw_path: Path | None = None,
    attempts: int = 1,
    mode: str = "html",
    extractor: Extractor | None = None,
    allow_model: bool = True,
    fetched_at: str = "",
    started: float | None = None,
) -> dict:
    started = started or time.perf_counter()
    brand = (brand or "").strip().lower()
    if not brand:
        raise ValueError("必须用 --brand 指定品牌 id")

    facts = ingest_domain.page_facts(html, page_url)
    raw_path = raw_path or _save_raw(html, label=proposed_id or brand, app=app)
    page_text = "\n".join(part for part in (facts.title, ingest_domain.html_to_text(html)) if part)
    fetched_at = fetched_at or datetime.now().isoformat(timespec="seconds")

    brand_name, default_currency = _shop_info(app, brand)
    model, model_status, errors = _extract(
        page_text, app=app, brand=brand, extractor=extractor, allow_model=allow_model
    )

    existing_ids, existing_series = _existing(app)
    validation = ingest_domain.validate(
        facts,
        model,
        brand=brand,
        brand_name=brand_name,
        source_text=page_text,
        source_url=page_url,
        default_currency=default_currency,
        proposed_id=proposed_id,
        existing_ids=existing_ids,
        existing_series=existing_series,
        today=date.today(),
        model_status=model_status,
    )

    source = {"url": page_url, "raw": str(raw_path), "fetched_at": fetched_at}
    base = {
        "status": validation.status,
        "fields": validation.fields,
        "_evidence": validation.evidence,
        "_missing": validation.missing,
        "_warnings": validation.warnings,
        "_errors": errors,
        "_reasons": validation.reasons,
        "_source": source,
        "_attempts": attempts,
    }

    if validation.status == ingest_domain.REJECTED:
        rejected_path = rejected_dir(app) / f"{_file_slug(validation.fields.get('id') or brand)}.json"
        payload = {**base, "_created_at": datetime.now().isoformat(timespec="seconds")}
        _atomic_write_json(rejected_path, payload)
        _log_run(
            app,
            mode=mode,
            status=validation.status,
            fields=validation.fields,
            raw_path=raw_path,
            source=source,
            missing=validation.missing,
            warnings=validation.warnings,
            errors=errors,
            model_status=model_status,
            attempts=attempts,
            started=started,
            rejected_path=rejected_path,
        )
        return {
            "status": validation.status,
            "id": validation.fields.get("id", ""),
            "fields": validation.fields,
            "missing": validation.missing,
            "warnings": validation.warnings,
            "errors": errors,
            "reasons": validation.reasons,
            "draft_path": "",
            "rejected_path": str(rejected_path),
            "raw_path": str(raw_path),
            "source": source,
        }

    draft_path = drafts_dir(app) / f"{validation.fields['id']}.json"
    payload = {**base, "_created_at": datetime.now().isoformat(timespec="seconds")}
    _atomic_write_json(draft_path, payload)
    _log_run(
        app,
        mode=mode,
        status=validation.status,
        fields=validation.fields,
        raw_path=raw_path,
        source=source,
        missing=validation.missing,
        warnings=validation.warnings,
        errors=errors,
        model_status=model_status,
        attempts=attempts,
        started=started,
        draft_path=draft_path,
    )
    return {
        "status": validation.status,
        "id": validation.fields.get("id", ""),
        "fields": validation.fields,
        "missing": validation.missing,
        "warnings": validation.warnings,
        "errors": errors,
        "reasons": validation.reasons,
        "draft_path": str(draft_path),
        "rejected_path": "",
        "raw_path": str(raw_path),
        "source": source,
    }


def _log_run(
    app: AppSettings,
    *,
    mode: str,
    status: str,
    fields: dict,
    raw_path: Path,
    source: dict,
    missing: list[str],
    warnings: list[str],
    errors: list[str],
    model_status: str,
    attempts: int,
    started: float,
    draft_path: Path | None = None,
    rejected_path: Path | None = None,
) -> None:
    """轨迹写不进去不该让入库失败——和 `agent.trace` 同一条纪律。"""
    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "mode": mode,
        "status": status,
        "id": fields.get("id", ""),
        "model_status": model_status,
        "attempts": attempts,
        "missing": missing,
        "warnings": warnings,
        "errors": errors,
        "raw": str(raw_path),
        "source_url": source.get("url", ""),
        "draft": str(draft_path or ""),
        "rejected": str(rejected_path or ""),
        "duration_ms": int((time.perf_counter() - started) * 1000),
    }
    try:
        path = ingest_root(app) / "runs.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:  # noqa: BLE001
        logger.warning("入库轨迹写入失败：%s", exc)


# --------------------------------------------------------------------- 对外入口


def ingest_from_html(
    html: str,
    *,
    page_url: str,
    brand: str,
    app: AppSettings | None = None,
    proposed_id: str = "",
    extractor: Extractor | None = None,
    allow_model: bool = True,
    mode: str = "html",
) -> dict:
    """从本地/已抓到的 HTML 生成草稿。**不写 content/**。"""
    return _ingest_core(
        html,
        page_url=page_url,
        brand=brand,
        app=app or default_settings,
        proposed_id=proposed_id,
        mode=mode,
        extractor=extractor,
        allow_model=allow_model,
    )


def ingest_from_url(
    url: str,
    *,
    brand: str,
    app: AppSettings | None = None,
    proposed_id: str = "",
    extractor: Extractor | None = None,
    allow_model: bool = True,
) -> dict:
    """抓一次页面再生成草稿。域名策略/登录墙检查由 `imagefetch.fetch_html` 负责。"""
    html = imagefetch.fetch_html(url)
    return _ingest_core(
        html,
        page_url=url,
        brand=brand,
        app=app or default_settings,
        proposed_id=proposed_id,
        mode="url",
        extractor=extractor,
        allow_model=allow_model,
    )


def retry_draft(
    path: str | Path,
    *,
    app: AppSettings | None = None,
    extractor: Extractor | None = None,
    allow_model: bool = True,
) -> dict:
    """从草稿里记录的 raw 快照重跑抽取，不重新抓页面。"""
    app = app or default_settings
    draft = _read_json(Path(path))
    source = draft.get("_source")
    fields = draft.get("fields")
    if not isinstance(source, dict) or not isinstance(fields, dict):
        raise ValueError("草稿缺少 _source 或 fields 对象")
    raw = Path(str(source.get("raw") or ""))
    if not raw.is_file():
        raise ValueError(f"原始快照不存在，无法重跑：{raw}")
    html = raw.read_text(encoding="utf-8")
    return _ingest_core(
        html,
        page_url=str(source.get("url") or ""),
        brand=str(fields.get("brand") or ""),
        app=app,
        proposed_id=str(fields.get("id") or ""),
        raw_path=raw,
        attempts=int(draft.get("_attempts") or 1) + 1,
        mode="retry",
        extractor=extractor,
        allow_model=allow_model,
        fetched_at=str(source.get("fetched_at") or ""),
    )


def approve_draft(
    path: str | Path,
    *,
    app: AppSettings | None = None,
    overwrite: bool = False,
) -> dict:
    """审批：二次校验通过后，把草稿写成 `content/newarrivals/<id>.json`。"""
    app = app or default_settings
    draft = _read_json(Path(path))
    status = str(draft.get("status") or "")
    fields = draft.get("fields")
    if not isinstance(fields, dict):
        return {"ok": False, "status": "refused", "problems": ["草稿缺少 fields 对象"]}

    if status == ingest_domain.REJECTED:
        reasons = draft.get("_reasons")
        if not isinstance(reasons, list) or not reasons:
            reasons = ["这条草稿已被标记为 rejected，不能审批"]
        return {"ok": False, "status": "refused", "problems": reasons}

    problems = ingest_domain.approve_problems(fields)
    entry_id = str(fields.get("id") or "").strip()
    target = app.newarrivals_dir / f"{entry_id}.json" if ingest_domain.safe_id(entry_id) else None
    if target is not None and target.exists() and not overwrite:
        problems.append(f"已存在同名条目：{target.name}；确认覆盖请加 --overwrite")
    if problems or target is None:
        return {"ok": False, "status": "refused", "problems": problems or ["id 不合法，无法写入"]}

    existing = _read_json(target) if target.exists() else {}
    proposed = ingest_domain.entry_payload(
        fields,
        fallback_source=str((draft.get("_source") or {}).get("url") or ""),
    )
    diff = ingest_domain.diff_fields(existing, proposed) if existing else []
    _atomic_write_json(target, proposed)

    draft["_approved_at"] = datetime.now().isoformat(timespec="seconds")
    draft["_approved_to"] = str(target)
    _atomic_write_json(Path(path), draft)

    return {
        "ok": True,
        "status": "approved",
        "entry_path": str(target),
        "diff": diff,
        "fields": proposed,
    }
