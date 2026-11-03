"""从 HTML 里抽商品图候选。

## 为什么是"抽候选 + 人确认"，不是"自动定图"

一个商品页里有几十个 `<img>`：站点 logo、导航图标、优惠券、推荐位、模特图、尺码表、
甚至 1×1 的埋点像素。任何"自动选出唯一正确那张"的规则都会错，而且错得没声音——
用户看到的是别人的推荐位，还以为自己买的是那条裙子。

所以这一层的职责只有一个：**把 60 个降到 3 个**。剩下的"选哪张"交给人一眼判断。

## 三条过滤靠什么

1. **扩展名**：`.svg` / `.gif` 基本都是图标和动图，商品主图不会用（错误率极低）。
2. **文件名里的尺寸**：`_430x430.jpg`、`.jpg_800x800.jpg` 这类后缀是电商 CDN 的常态，
   小于 200px 的通常是缩略图或像素，大于 400px 才可能是主图。
3. **文件名里的坏词**：`icon / logo / sprite / qrcode / placeholder …`。

刻意**不看路径**：`/item/`、`/detail/` 这些段在某些站点是页面路由，在里面出现图片
是正常的；用它判断只会误杀。

## 域名策略

`taobao.com` / `tmall.com` 的 robots.txt 是 `Disallow: /*?*`——**带查询参数**的地址
一律不允许抓。这一条是硬的，写在 `policy_violation()` 里，脚本遇到就直接拒绝，
不提供"跳过检查"的开关。无参数的店铺/商品地址不在禁止范围内。
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse

# 懒加载把真实地址藏在任意 data-* 属性里。列的是实际遇到过的几种，
# 淘宝是 data-ks-lazyload，通用站是 data-src / data-original。
_SRC_ATTRS = ("data-ks-lazyload", "data-original", "data-src", "lazy-src", "src")

_IMG_TAG = re.compile(r"<img\b([^>]*)>", re.I)
_ATTR = re.compile(r"""([\w:.-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_DIMS = re.compile(r"(\d{2,4})x(\d{2,4})")
_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")
# BOM 和零宽字符会混进"附近文字"，然后在 Windows 控制台上直接编码失败。
# 它们对匹配没有任何用处，提前清掉。
_INVISIBLE = re.compile(r"[\ufeff\u200b\u200c\u200d\u2060]")

GOOD_EXTS = (".jpg", ".jpeg", ".png", ".webp")
BAD_WORDS = (
    "icon", "logo", "sprite", "avatar", "banner", "qrcode", "qr_",
    "loading", "placeholder", "blank", "spacer", "arrow", "btn",
    "coupon", "star", "soldout", "thumb",
)

# 这些域名的 robots.txt 禁止带查询参数的地址
_QUERY_FORBIDDEN_HOSTS = ("taobao.com", "tmall.com")

# 这些域名的 robots.txt 是 `User-Agent: *` + `Disallow: /`（只对具名搜索引擎 UA 例外），
# 所以**整站都不自动抓**——包括品牌官方账号。要取边走 --from-html（浏览器另存，零请求）。
_ROBOTS_DENY_ALL_HOSTS = ("weibo.com", "weibo.cn")

# 微博图床把尺寸写在**目录段**里（/thumb150/ 头图、/mw690/、/large/ 原图），
# 不在文件名里，所以必须单独看路径，否则头像和商品主图同分。
_SIZE_BIG = ("large", "original", "mw2000", "mw1024")
_SIZE_SMALL = ("thumb150", "thumb", "small", "orj360", "mw360", "square")

# 取图片"前后文"的窗口：商品列表页里，系列名通常就在图片旁边几十个字内。
# 600 字是实测够用又不至于把隔壁商品的名字也卷进来的折中。
CONTEXT_CHARS = 600


@dataclass(frozen=True, slots=True)
class Candidate:
    url: str
    context: str
    score: float


def _plain_text(fragment: str) -> str:
    return _SPACE.sub(" ", _INVISIBLE.sub("", _TAG.sub(" ", fragment or ""))).strip()


def extract_candidates(html: str, base_url: str) -> list[Candidate]:
    """按文档顺序抽出所有候选图，每个带上"附近文字"。

    附近文字是系列名匹配的唯一依据：品牌的新品列表页里没有 `item_id` 这种可靠锚点，
    能用的只有"这张图旁边写的是什么字"。窗口取 ``CONTEXT_CHARS`` 前后各一段。
    """
    seen: set[str] = set()
    out: list[Candidate] = []
    html = html or ""

    for match in _IMG_TAG.finditer(html):
        attrs: dict[str, str] = {}
        for name, double_quoted, single_quoted in _ATTR.findall(match.group(1)):
            key = name.lower()
            if key not in attrs:
                attrs[key] = double_quoted or single_quoted

        raw = ""
        for name in _SRC_ATTRS:
            value = (attrs.get(name) or "").strip()
            if value:
                raw = value
                break
        if not raw and attrs.get("srcset"):
            parts = [part.strip().split()[0] for part in attrs["srcset"].split(",") if part.strip()]
            raw = parts[-1] if parts else ""

        if not raw or raw.startswith("data:"):
            continue
        absolute = urljoin(base_url, raw)
        if absolute in seen:
            continue
        seen.add(absolute)

        start = max(0, match.start() - CONTEXT_CHARS)
        end = min(len(html), match.end() + CONTEXT_CHARS)
        out.append(Candidate(url=absolute, context=_plain_text(html[start:end]), score=score(absolute)))
    return out


def extract_image_urls(html: str, base_url: str) -> list[str]:
    """只要地址（按文档顺序、去重）。给测试和"只想看一眼"的场景用。"""
    return [candidate.url for candidate in extract_candidates(html, base_url)]


def _needles(series: str, aliases: Sequence[str] = ()) -> list[str]:
    """把系列名和别名都归一成"去空格小写"的比对串。

    别名是必要的：同一个系列在中文店和日本官网上是两个名字（`白鳥籠` / `白鸟笼`），
    只认一个名字的话，"从官网取图"永远配不上——而这不是解析器的错。
    """
    raw = [series, *(aliases or ())]
    return [n for n in (re.sub(r"\s+", "", str(item or "")).lower() for item in raw) if n]


def match_series(candidates: list[Candidate], series: str, aliases: Sequence[str] = ()) -> list[Candidate]:
    """挑出"附近文字里出现了系列名（或别名）"的候选，按分数排。

    比对前去掉空格：列表页的标题常带空格或换行（"秋日 私语"），直接 ``in`` 会漏掉。
    **不做模糊匹配、不做同义词**——认错图比没有图更糟，用户会照着别人的裙子下单。
    """
    needles = _needles(series, aliases)
    if not needles:
        return []
    matched = [
        candidate
        for candidate in candidates
        if any(needle in re.sub(r"\s+", "", candidate.context).lower() for needle in needles)
    ]
    matched.sort(key=lambda candidate: -candidate.score)
    return matched


# --------------------------------------------------------------------- 商品链接


@dataclass(frozen=True, slots=True)
class Product:
    """列表页里的一条商品：详情页地址 + 主图 + 旁边文字 + 价格（可能没有）。"""

    item_url: str
    item_id: str
    image_url: str
    context: str
    price: str = ""


_ANCHOR = re.compile(r"""<a\b([^>]*)>([\s\S]{0,4000}?)</a>""", re.I)
_HREF = re.compile(r"""href\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)

# 价格写成「&yen;55,000」或「20,900円」都见过。只认"带货币标记的三位以上数字"，
# 裸数字（商品编号、尺码、年份）一律不当价格。
_PRICE = re.compile(r"(?:&yen;|&amp;yen;|¥|￥)\s*([\d,]{3,})|([\d,]{3,})\s*円")

# 淘宝/天猫的商品详情页只认这两个形状：`.../item.htm?id=数字`。
# 其余（店铺页、活动页、直播间）都不是"这一件商品"。
_ITEM_PATH = "item.htm"


def _price_amounts(html: str) -> list[tuple[int, str]]:
    """页面里所有价格：(位置, 纯数字)。位置用来跟商品配对，不能丢。"""
    out: list[tuple[int, str]] = []
    for match in _PRICE.finditer(html or ""):
        digits = (match.group(1) or match.group(2) or "").replace(",", "").strip()
        if digits.isdigit():
            out.append((match.start(), digits))
    return out


def _host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _is_marketplace(host: str) -> bool:
    return host == "taobao.com" or host.endswith(".taobao.com") \
        or host == "tmall.com" or host.endswith(".tmall.com")


def item_url_of(url: str) -> tuple[str, str]:
    """把任意一个淘宝/天猫地址规范成详情页。返回 ``(地址, item_id)``，认不出就 ``("", "")``。

    只保留 ``id``：`spm`、`scm`、`ali_trackid` 那些是推广追踪参数，**换一次点法就变一次**，
    存进 JSON 里等于存了一个会过期的指纹。存 id 才是稳定的。
    """
    parsed = urlparse(url)
    host = _host_of(url)
    if not _is_marketplace(host):
        return "", ""
    if not parsed.path.lower().rstrip("/").endswith(_ITEM_PATH):
        return "", ""
    values = parse_qs(parsed.query).get("id") or []
    item_id = (values[0] if values else "").strip()
    if not item_id.isdigit():
        return "", ""
    return f"https://{host}/{_ITEM_PATH}?id={item_id}", item_id


def extract_catalog(html: str, base_url: str, *, item_href_pattern: str = "") -> list[Product]:
    """通用商品块抽取：**同一个商品页地址下的所有 ``<a>`` 归成一条 Product**。

    为什么必须按地址归并而不是"一个 ``<a>`` 一条"：官网列表页常见一种拆法——
    **图和标题是两个指向同一商品的 ``<a>``**（AP 官网就是这样：一个有图没字，
    一个有字没图）。一个 ``<a>`` 一条会得到两条残废记录，配对时全都不完整。

    这也是它能同时吃淘宝和品牌官网的原因：判定"这是不是商品页"只靠
    ``item_href_pattern`` 这个特征串，不写死域名。
    """
    if not item_href_pattern:
        return []

    buckets: dict[str, dict] = {}
    order: list[str] = []

    for match in _ANCHOR.finditer(html or ""):
        attrs, inner = match.group(1), match.group(2)
        href_match = _HREF.search(attrs)
        if not href_match:
            continue
        href = (href_match.group(1) or href_match.group(2) or "").strip()
        if item_href_pattern not in href:
            continue

        url = urljoin(base_url, href.replace("&amp;", "&"))
        if url not in buckets:
            buckets[url] = {"image": "", "texts": [], "first_pos": match.start(), "title_pos": 0}
            order.append(url)
        bucket = buckets[url]

        if not bucket["image"]:
            images_inside = extract_candidates(inner, url)
            images_inside.sort(key=lambda candidate: -candidate.score)
            for candidate in images_inside:
                if candidate.score >= -1.0:
                    bucket["image"] = candidate.url
                    break

        text = _plain_text(inner)
        if text:
            if not bucket["title_pos"]:
                bucket["title_pos"] = match.start()
            if text not in bucket["texts"]:
                bucket["texts"].append(text)

    # 价格不在 <a> 里，而是页面上一段独立文本（AP 官网就是"图 + 标题 + 价格"三块）。
    # 所以按**位置区间**配：从这一条的标题开始，到下一个商品的第一个 <a> 之前，
    # 区间内的第一个价格就是它的。这样既不会串到下一件，也不会因为页面里
    # 还有运费/税/推荐位价格而乱抓——只取区间里最靠前的那个。
    prices = _price_amounts(html or "")
    starts = sorted(buckets[url]["first_pos"] for url in order)

    products: list[Product] = []
    for url in order:
        bucket = buckets[url]
        if not bucket["image"]:
            continue
        anchor_pos = bucket["title_pos"] or bucket["first_pos"]
        boundary = next((pos for pos in starts if pos > bucket["first_pos"]), len(html or ""))
        price = next(
            (amount for pos, amount in prices if anchor_pos < pos < boundary),
            "",
        )
        products.append(
            Product(
                item_url=url,
                item_id=_item_signature(url),
                image_url=bucket["image"],
                context=" ".join(bucket["texts"]),
                price=price,
            )
        )
    return products


def _item_signature(url: str) -> str:
    """商品页的稳定标识，用来去重。淘宝取 ``id`` 参数，其余取完整地址。"""
    item_url, item_id = item_url_of(url)
    if item_id:
        return item_id
    return url


def parse_shopify_products(text: str, base_url: str) -> list[Product]:
    """解析 Shopify 的公开商品 JSON（``/products.json``）。

    比抓 HTML 稳得多：标题、主图、商品地址都是结构化的，不用猜 DOM。
    解析不出来就返回空列表，让上层退回 HTML 路径——不要把"这家店不是 Shopify"
    变成一个异常。
    """
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return []
    items = payload.get("products") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []

    root = base_url.rstrip("/")
    if root.endswith(".json"):
        root = root[: -len(".json")]
    if "/products.json" in root:
        root = root.split("/products.json")[0]
    if "/collections/" in root:
        root = root.split("/collections/")[0]

    products: list[Product] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        handle = str(item.get("handle") or "").strip()
        title = str(item.get("title") or "").strip()
        variants = item.get("variants") or [{}]
        amount = ""
        if variants and isinstance(variants[0], dict):
            raw_price = str(variants[0].get("price") or "").strip()
            # Shopify 给的是 "41800.00" 或 "41800"；只留整数部分，"41800.00" 当成
            # 41800 元，而不是 41800 分——这里没有字段能证明币种，别自己乘 100。
            head = raw_price.split(".")[0]
            amount = head if head.isdigit() else ""
        pictures = item.get("images") or []
        source = ""
        if pictures and isinstance(pictures[0], dict):
            source = str(pictures[0].get("src") or "").strip()
        if not handle or not source:
            continue
        products.append(
            Product(
                item_url=f"{root}/products/{handle}",
                item_id=handle,
                image_url=source,
                context=title,
                price=amount,
            )
        )
    return products


def extract_taobao_products(html: str, base_url: str) -> list[Product]:
    products: list[Product] = []
    seen: set[str] = set()

    for attrs, inner in _ANCHOR.findall(html or ""):
        href_match = _HREF.search(attrs)
        if not href_match:
            continue
        href = href_match.group(1) or href_match.group(2) or ""
        item_url, item_id = item_url_of(urljoin(base_url, href.strip()))
        if not item_url or item_id in seen:
            continue

        images_inside = extract_candidates(inner, item_url)
        if not images_inside:
            continue
        images_inside.sort(key=lambda candidate: -candidate.score)
        best = images_inside[0]
        if best.score < -1.0:
            continue

        seen.add(item_id)
        products.append(
            Product(
                item_url=item_url,
                item_id=item_id,
                image_url=best.url,
                # 旁边文字取整段 <a> 的文本；没有就用图片自己的近旁文字
                context=_plain_text(inner) or best.context,
            )
        )
    return products


def match_products(products: list[Product], series: str, aliases: Sequence[str] = ()) -> list[Product]:
    """商品块按系列名（或别名）配对，图分高的排前面。配不上返回空列表。"""
    needles = _needles(series, aliases)
    if not needles:
        return []
    matched = [
        product
        for product in products
        if any(needle in re.sub(r"\s+", "", product.context).lower() for needle in needles)
    ]
    matched.sort(key=lambda product: -score(product.image_url))
    return matched


def search_url_for(brand_name: str, series: str) -> str:
    """淘宝搜索地址。**我们不抓它**，只把它作为"没录到详情页时"的可点入口。

    详情页地址必须来自真实数据（`link` 字段，或列表页里抽到的 ``id``）——拼不出来，
    也不该拼。所以兜底只能是"去搜一下"，并且界面上必须标成「搜」而不是「商品」。
    """
    keyword = " ".join(part for part in (brand_name, series) if part).strip()
    if not keyword:
        return ""
    return "https://s.taobao.com/search?q=" + quote_plus(keyword)


def score(url: str) -> float:
    """越大越像商品主图。低于 -1 的基本可以丢。"""
    parsed = urlparse(url)
    path = PurePosixPath(parsed.path)
    name = path.name.lower()
    suffix = path.suffix.lower()

    if suffix not in GOOD_EXTS:
        return -100.0                      # svg / gif / 无扩展名：图标和动图
    value = 2.0 if suffix in (".jpg", ".jpeg", ".webp") else 1.0
    if any(word in name for word in BAD_WORDS):
        value -= 5.0

    sides = [max(int(width), int(height)) for width, height in _DIMS.findall(name)]
    if sides:
        side = max(sides)
        if side >= 400:
            value += 3.0
        elif side <= 200:
            value -= 3.0

    # 路径里的尺寸段（微博图床）：/thumb150/ 是头像，/large/ 是原图
    lowered = parsed.path.lower()
    if any(token in lowered for token in _SIZE_SMALL):
        value -= 4.0
    elif any(token in lowered for token in _SIZE_BIG):
        value += 3.0

    return value


def pick_candidates(urls: list[str], limit: int = 5, min_score: float = -1.0) -> list[str]:
    """按分数挑前 ``limit`` 个，同分保持文档顺序（用户更可能选靠前的）。"""
    ranked = [(score(url), index, url) for index, url in enumerate(urls)]
    keep = [row for row in ranked if row[0] >= min_score]
    keep.sort(key=lambda row: (-row[0], row[1]))
    return [url for _, _, url in keep[:limit]]


def policy_violation(url: str) -> str:
    """这个地址允许不允许抓。返回空串表示允许，否则是拒绝理由。

    只做**明确的**拒绝，不做"看起来像爬虫就拦"：多拦一次，用户就得手动复制一次，
    然后他会去找一个更粗糙的工具——那才是更糟的结果。
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"只支持 http/https：{parsed.scheme or '（没有协议）'}"
    host = (parsed.hostname or "").lower()
    if not host:
        return "地址里没有主机名"

    if any(host == base or host.endswith("." + base) for base in _ROBOTS_DENY_ALL_HOSTS):
        return (
            "微博的 robots.txt 是 `User-Agent: *` + `Disallow: /`（只对具名搜索引擎 UA 例外），"
            "整站不允许抓取，包括品牌官方账号。\n"
            "  请在浏览器里打开官方账号、把页面另存为 HTML，再用脚本的 --from-html 解析"
            "（那条路完全不联网）。"
        )

    if any(host == base or host.endswith("." + base) for base in _QUERY_FORBIDDEN_HOSTS):
        if parsed.query:
            return (
                "淘宝/天猫的 robots.txt 是 `Disallow: /*?*`：带查询参数的地址一律不抓。"
                "请换成不带参数的店铺页或商品页地址。"
            )
    return ""
