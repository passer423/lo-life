"""离线入库的纯逻辑：页面事实、字段归一化、校验、状态判定、审批检查。

这条链路把"页面"变成"草稿"，但**不联网、不写盘**——抓取、快照、草稿和审批在
`app/services/ingest_pipeline.py`。放在 `domain/` 的理由和 `images.py` 一样：
给定输入一定有输出，不联网也能把每条规则单测钉死。

## 三条纪律

1. **模型只提案，不裁决。** 它给的每个字段都必须带原文引文；引文对不上就丢弃该
   字段。字段级失败不会让整份草稿作废，但状态必须降为 `partial`——绝不把"部分
   成功"伪装成 `ready`。
2. **不猜缺失事实。** 页面只写"10 月下旬"时 `release_date` 留空，不推算具体日期；
   价格吸取不到就留空，不用模型记忆补。
3. **状态决定去向。** `ready / partial / extract_failed / rejected` 不是装饰：
   只有 `ready` 且审批时二次校验通过的草稿，才允许写进 `content/`。
"""

from __future__ import annotations

import hashlib
import html as html_lib
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable
from urllib.parse import urljoin

from app.domain import urls
from app.domain.newarrivals import STYLE_OPTIONS

READY = "ready"
PARTIAL = "partial"
EXTRACT_FAILED = "extract_failed"
REJECTED = "rejected"

REQUIRED = ("id", "brand", "series", "release_date")

# 写进 `content/newarrivals` 的字段白名单。草稿里允许有 `_evidence` 等下划线字段，
# 但它们永远不进正式条目。
ENTRY_FIELDS = (
    "id", "brand", "brand_name", "series", "release_date", "price_range",
    "currency", "items", "styles", "image", "link", "note", "source",
    "sample", "aliases",
)

_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,79}")

_SCRIPT_RE = re.compile(r"<(script|style|noscript)\b[^>]*>.*?</\1>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]{1,200}>")
_META_RE = re.compile(r"<meta\b([^>]*?)>", re.I | re.S)
_ATTR_RE = re.compile(r"""([\w:-]+)\s*=\s*("([^"]*)"|'([^']*)')""", re.I)
_TITLE_RE = re.compile(r"<title\b[^>]*>([\s\S]*?)</title>", re.I)
_LINK_RE = re.compile(r"<link\b([^>]*?)>", re.I | re.S)
_CANONICAL_REL = "canonical"

_DATE_PATTERNS = (
    re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})"),
    re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日"),
)

_QUOTE_WS = re.compile(r"\s+")


@dataclass(slots=True)
class PageFacts:
    """页面里**结构上**能确定的事实。不靠模型，也不猜。"""

    title: str = ""
    image: str = ""
    link: str = ""
    price: str = ""


@dataclass(slots=True)
class Validation:
    status: str
    fields: dict[str, Any]
    evidence: dict[str, dict]
    missing: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


# --------------------------------------------------------------------- 页面事实


def html_to_text(raw: str) -> str:
    """HTML → 纯文本。保留换行，去掉脚本/样式和标签。

    尺寸/系列名都写在正文里，先把标签换成换行比"删标签"更安全：`<p>a</p><p>b</p>`
    删成 `ab` 会把两个事实粘成一个。
    """
    text = _SCRIPT_RE.sub("\n", raw or "")
    text = _TAG_RE.sub("\n", text)
    text = html_lib.unescape(text)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _attrs(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for match in _ATTR_RE.finditer(text or ""):
        name = match.group(1).lower()
        value = match.group(3) if match.group(3) is not None else match.group(4)
        found.setdefault(name, value or "")
    return found


def _meta_content(raw: str, names: Iterable[str]) -> str:
    wanted = {name.lower() for name in names}
    for match in _META_RE.finditer(raw or ""):
        attrs = _attrs(match.group(1))
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        if key in wanted and attrs.get("content"):
            return html_lib.unescape(attrs["content"]).strip()
    return ""


def page_facts(raw: str, page_url: str = "") -> PageFacts:
    """只取页面元信息：标题、主图、规范地址、结构化价格。

    正文里的价格/系列名不在这里猜——那是模型抽取或人工录入的事。元信息抽不到就
    返回空串，草稿会把它列进 `_missing`。
    """
    title = _meta_content(raw, ("og:title", "twitter:title"))
    if not title:
        match = _TITLE_RE.search(raw or "")
        title = re.sub(r"\s+", " ", html_lib.unescape(match.group(1))).strip() if match else ""

    image = _meta_content(raw, ("og:image", "twitter:image"))
    if image:
        image = urljoin(page_url, image) if page_url.startswith(("http://", "https://")) else image

    link = _meta_content(raw, ("og:url",))
    if not link:
        for match in _LINK_RE.finditer(raw or ""):
            attrs = _attrs(match.group(1))
            if attrs.get("rel", "").lower() == _CANONICAL_REL and attrs.get("href"):
                link = attrs["href"].strip()
                break
    link = urljoin(page_url, link) if link and page_url.startswith(("http://", "https://")) else link

    amount = _meta_content(raw, ("product:price:amount", "og:price:amount"))
    return PageFacts(
        title=title,
        image=urls.safe_url(image),
        link=urls.safe_url(link or page_url),
        price=amount.strip(),
    )


# --------------------------------------------------------------------- 归一化


def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_date(value: Any) -> str:
    """只接受**明确写出年月日**的日期；"10 月下旬"返回空串，不补成 1 号。"""
    text = _text(value)
    for pattern in _DATE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        try:
            parsed = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return ""
        if 2000 <= parsed.year <= date.today().year + 2:
            return parsed.isoformat()
        return ""
    return ""


def _normalize_styles(value: Any) -> list[str]:
    raw = value if isinstance(value, (list, tuple)) else [value]
    return [item for item in (_text(part) for part in raw) if item in STYLE_OPTIONS]


def _normalize_items(value: Any) -> list[str]:
    raw = value if isinstance(value, (list, tuple)) else [value]
    out: list[str] = []
    for part in raw:
        item = _text(part).upper()
        if item and item not in out:
            out.append(item)
    return out


def _normalize_aliases(value: Any) -> list[str]:
    raw = value if isinstance(value, (list, tuple)) else [value]
    out: list[str] = []
    for part in raw:
        alias = _text(part)
        if alias and alias not in out:
            out.append(alias)
    return out


def _normalize_currency(value: Any, fallback: str) -> tuple[str, bool]:
    text = _text(value).upper()
    if re.fullmatch(r"[A-Z]{3}", text):
        return text, False
    return fallback, bool(text)


def quote_found(quote: str, source_text: str) -> bool:
    """引文去空白后必须是原文的子串。

    只做空白归一化：模型改写、翻译、补全都会被判失败。这条检查宁可严格——
    草稿被拦下可以人工补，编造的字段进了 `content/` 就很难再发现。
    """
    needle = _QUOTE_WS.sub("", str(quote or ""))
    haystack = _QUOTE_WS.sub("", str(source_text or ""))
    return bool(needle) and needle in haystack


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")


def default_id(brand: str, release_date: str, series: str, today: date | None = None) -> str:
    """`YYYY-MM-brand-slug`。系列名是中文/日文时没有拉丁 slug，退到稳定短哈希，
    保证重复跑得到同一个 id，而不是每跑一次换个名字。"""
    today = today or date.today()
    month = release_date[:7] if re.fullmatch(r"\d{4}-\d{2}-\d{2}", release_date or "") else today.strftime("%Y-%m")
    slug = _slug(series)
    if not slug:
        digest = hashlib.sha1(str(series or "item").encode("utf-8")).hexdigest()[:6]
        slug = f"item-{digest}"
    return f"{month}-{brand}-{slug}"[:80].strip("-")


def safe_id(value: str) -> bool:
    return bool(_ID.fullmatch(str(value or "")))


# --------------------------------------------------------------------- 校验


def validate(
    facts: PageFacts,
    model: dict | None,
    *,
    brand: str,
    brand_name: str = "",
    source_text: str,
    source_url: str = "",
    default_currency: str = "CNY",
    proposed_id: str = "",
    existing_ids: Iterable[str] = (),
    existing_series: Iterable[tuple[str, str]] = (),
    today: date | None = None,
    model_status: str = "ok",
) -> Validation:
    """把结构事实 + 模型提案合成一份可审批的字段集。

    ``model_status`` 三态：``ok`` / ``skipped``（没 key 或主动跳过）/ ``failed``。
    它决定最终状态是 ``partial`` 还是 ``extract_failed``。
    """
    today = today or date.today()
    model = model or {}
    quotes = model.get("quotes") if isinstance(model.get("quotes"), dict) else {}

    fields: dict[str, Any] = {
        "brand": _text(brand).lower(),
        "brand_name": _text(brand_name) or _text(brand),
        "price_range": "",
        "currency": default_currency or "CNY",
        "items": [],
        "styles": [],
        "image": facts.image,
        "link": facts.link,
        "note": "",
        "source": "",
        "sample": False,
        "aliases": [],
    }
    evidence: dict[str, dict] = {}
    missing: list[str] = []
    warnings: list[str] = []
    reasons: list[str] = []

    def take(name: str, normalize, *, required: bool = False, default=None):
        raw = model.get(name)
        if raw is None or raw == "" or raw == []:
            if required:
                missing.append(name)
            return default
        quote = str(quotes.get(name) or "")
        if not quote_found(quote, source_text):
            warnings.append(f"{name}：模型给了值，但引文在原文里找不到，已丢弃")
            if required:
                missing.append(name)
            return default
        try:
            value = normalize(raw)
        except ValueError:
            warnings.append(f"{name}：格式不合法，已丢弃")
            if required:
                missing.append(name)
            return default
        evidence[name] = {"kind": "model", "quote": quote}
        return value

    fields["series"] = take("series", lambda value: _text(value), required=True, default="")
    fields["release_date"] = take("release_date", _date_or_error, required=True, default="")

    # 价格优先用页面元信息：那是页面自己声明的原价，不经过模型转述。
    if facts.price:
        fields["price_range"] = facts.price
        evidence["price_range"] = {"kind": "structural", "from": source_url}
    else:
        fields["price_range"] = take("price_range", lambda value: _text(value), default="")

    currency, invalid = _normalize_currency(
        take("currency", lambda value: _text(value), default=""), default_currency or "CNY"
    )
    if invalid:
        warnings.append(f"currency：{model.get('currency')!r} 不是 ISO 三字母代码，已用店铺默认值")
    fields["currency"] = currency

    fields["items"] = take("items", _normalize_items, default=[])

    fields["styles"] = take("styles", _normalize_styles, default=[])
    raw_styles = model.get("styles")
    if raw_styles and not fields["styles"]:
        warnings.append("styles：没有一个值在固定风格词表里，已丢弃")

    fields["note"] = take("note", lambda value: _text(value), default="")
    fields["aliases"] = take("aliases", _normalize_aliases, default=[])

    if not fields["brand"]:
        missing.append("brand")

    chosen_id = _text(proposed_id) or default_id(fields["brand"], fields["release_date"], fields["series"], today)
    if not safe_id(chosen_id):
        reasons.append("id 只能是小写字母、数字和连字符（最多 80 字符）")
    elif chosen_id in set(existing_ids):
        reasons.append(f"id 已存在：{chosen_id}")
    fields["id"] = chosen_id

    series_key = _series_key(fields["brand"], fields["series"])
    if series_key and series_key in {_series_key(b, s) for b, s in existing_series}:
        warnings.append("同品牌已有同名系列：确认不是重复录入")

    fields["source"] = (
        f"依据：{source_url}（{today.isoformat()} 取）" if source_url else f"依据：本地页面（{today.isoformat()} 取）"
    )

    if reasons:
        status = REJECTED
    elif model_status == "failed":
        status = EXTRACT_FAILED
    elif missing:
        status = PARTIAL
    else:
        status = READY
    return Validation(
        status=status,
        fields=fields,
        evidence=evidence,
        missing=missing,
        warnings=warnings,
        reasons=reasons,
    )


def _date_or_error(value: Any) -> str:
    parsed = normalize_date(value)
    if not parsed:
        raise ValueError("日期不是明确的年月日")
    return parsed


def _series_key(brand: str, series: str) -> tuple[str, str]:
    return (_QUOTE_WS.sub("", str(brand or "").lower()), _QUOTE_WS.sub("", str(series or "").lower()))


# --------------------------------------------------------------------- 审批


def approve_problems(fields: dict) -> list[str]:
    """审批时的二次校验：只看结构和取值，不再要求引文。

    人工补写字段不需要"原文引文"——引文检查发生在抽取阶段，目的是防模型编造，
    不是防录入者。审批要防的是空字段、坏 id、坏日期、写错词表。
    """
    problems: list[str] = []
    for key in REQUIRED:
        if not _text(fields.get(key)):
            problems.append(f"缺少必填字段：{key}")

    if fields.get("id") and not safe_id(fields.get("id")):
        problems.append("id 只能是小写字母、数字和连字符（最多 80 字符）")

    if fields.get("release_date") and not normalize_date(fields.get("release_date")):
        problems.append("release_date 必须是明确的 YYYY-MM-DD")

    if fields.get("styles") and any(item not in STYLE_OPTIONS for item in fields["styles"]):
        problems.append("styles 里有固定词表之外的值")

    currency = _text(fields.get("currency")).upper()
    if currency and not re.fullmatch(r"[A-Z]{3}", currency):
        problems.append("currency 必须是 ISO 三字母代码")

    if not isinstance(fields.get("items", []), list) or not isinstance(fields.get("aliases", []), list):
        problems.append("items / aliases 必须是数组")

    return problems


def entry_payload(fields: dict, *, fallback_source: str = "") -> dict:
    """草稿字段 → 正式条目 JSON。只保留白名单字段，并再过一遍出口校验。"""
    payload = {key: fields.get(key) for key in ENTRY_FIELDS}
    payload["id"] = _text(payload.get("id"))
    payload["brand"] = _text(payload.get("brand")).lower()
    payload["brand_name"] = _text(payload.get("brand_name")) or payload["brand"]
    payload["series"] = _text(payload.get("series"))
    payload["release_date"] = normalize_date(payload.get("release_date"))
    payload["price_range"] = _text(payload.get("price_range"))
    payload["currency"] = _text(payload.get("currency")).upper() or "CNY"
    payload["items"] = _normalize_items(payload.get("items"))
    payload["styles"] = _normalize_styles(payload.get("styles"))
    payload["image"] = urls.safe_url(payload.get("image"))
    payload["link"] = urls.safe_url(payload.get("link"))
    payload["note"] = _text(payload.get("note"))
    payload["source"] = _text(payload.get("source")) or fallback_source
    payload["sample"] = False
    payload["aliases"] = _normalize_aliases(payload.get("aliases"))
    return payload


def diff_fields(existing: dict, proposed: dict) -> list[str]:
    """审批前给人看的差异：只列变化字段，不打印整份 JSON。"""
    lines: list[str] = []
    for key in ENTRY_FIELDS:
        old = existing.get(key)
        new = proposed.get(key)
        if old == new:
            continue
        lines.append(f"  {key}: {old!r} → {new!r}")
    return lines
