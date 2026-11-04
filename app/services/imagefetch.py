"""取图服务：抓页面 → 抽候选 → 下图片 → 落 `data/media/`。

## 为什么在 `app/services/` 而不在 `app/domain/`

`domain/` 里全是**纯函数**：给定输入一定有输出，不发请求、不写文件、可单测。
这里必须联网、必须写磁盘。混进 `domain/` 之后，"这个模块要不要联网"就变成
得读代码才知道的事——而这条信息恰恰是审核时最该一眼看见的。

## 谁调用它

* `scripts/fetch_images.py`（批量、离线）
* `POST /api/images/fetch`（界面上点一下，单条）

两条路用的是**同一份**逻辑。复制一份到路由里，下次改筛选规则时必然只改一处，
然后两边给出不同的图——这种分歧最难查。

## 只在被触发时联网

服务启动、页面加载、搜索、推荐排序，全都不碰这里。所以页面永远不会因为某个站
超时而变慢。
"""

from __future__ import annotations

import json
import ssl
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from app.config import settings
from app.domain import images

MAX_BYTES = 10 * 1024 * 1024
TIMEOUT = 20

# 有些站会按 UA 直接返回 403 或一张"请用手机打开"的图，所以必须带一个正常的浏览器 UA。
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

SUFFIX_BY_TYPE = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

LOGIN_HINTS = ("请登录", "亲，请登录", "login.taobao.com", "身份验证", "滑动验证", "安全验证")


class FetchError(RuntimeError):
    """取图失败。消息里必须带"下一步该做什么"，否则用户只会反复重试。"""


def ssl_context() -> ssl.SSLContext:
    """给 HTTPS 用的 SSL 上下文。**必须校验证书**，只是要给它一个可信的 CA 包。

    为什么不能直接用默认值：这台机器上的 Python（Anaconda 版）里，
    ``ssl.get_default_verify_paths()`` 指向 ``C:\\Program Files\\Common Files\\ssl\\cert.pem``，
    那个路径并不存在。于是任何 https 请求都会 ``CERTIFICATE_VERIFY_FAILED``，
    而症状长得像"连不上"/"站点挂了"——真正的原因跟网络无关。

    也**不会**退化成 ``CERT_NONE``：关掉证书校验等于把抓取变成"谁劫持都认"，
    为了省一个 bundle 而拆掉 TLS 没有道理。
    """
    try:
        import certifi  # noqa: PLC0415 - 可选依赖

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:                                       # noqa: BLE001 - 没装就退回系统默认
        return ssl.create_default_context()


_SSL = ssl_context()


@dataclass(slots=True)
class Resolution:
    """一次解析的结果：选中的图、详情页链接、价格，以及全部候选（给调用方打印/选择）。"""

    image_url: str = ""
    item_link: str = ""
    price: str = ""
    products: list[images.Product] = field(default_factory=list)
    candidates: list[images.Candidate] = field(default_factory=list)


# --------------------------------------------------------------------- 网络


def fetch_html(url: str) -> str:
    """抓一个页面。先过域名策略，再抓，最后检查是不是撞上了登录墙/风控页。"""
    reason = images.policy_violation(url)
    if reason:
        raise FetchError(reason)

    request = Request(url, headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    try:
        with urlopen(request, timeout=TIMEOUT, context=_SSL) as response:      # noqa: S310 - 本地工具
            charset = response.headers.get_content_charset() or "utf-8"
            html = response.read().decode(charset, errors="replace")
    except HTTPError as exc:
        raise FetchError(
            f"HTTP {exc.code}：{url}\n"
            "  403/418 通常是被风控拦了。这种情况请在浏览器里打开页面、另存为 HTML，"
            "然后用脚本的 --from-html 解析（零请求）。"
        ) from exc
    except URLError as exc:
        raise FetchError(f"连不上：{exc.reason}（{url}）") from exc

    if any(hint in html for hint in LOGIN_HINTS) and "<img" not in html[:2000]:
        raise FetchError(
            f"这个页面看起来是登录墙或风控页：{url}\n"
            "  请在浏览器里打开、另存为 HTML，再用 --from-html。"
        )
    return html


def download_image(url: str, *, referer: str = "") -> tuple[bytes, str]:
    """下载一张图，返回 ``(字节, 后缀)``。

    带 ``Referer`` 是必要的：电商图片 CDN 普遍校验来源，不带会拿到 403 或一张占位图。
    这不是"绕过防护"，只是把浏览器本来就会发的那个头补上——下载的是你自己正在看的
    那张商品图。
    """
    request = Request(
        url,
        headers={
            "User-Agent": UA,
            "Referer": referer or url,
            "Accept": "image/avif,image/webp,image/png,image/*,*/*;q=0.8",
        },
    )
    try:
        with urlopen(request, timeout=TIMEOUT, context=_SSL) as response:      # noqa: S310
            content_type = (response.headers.get_content_type() or "").lower()
            if not content_type.startswith("image/"):
                raise FetchError(f"返回的不是图片（{content_type}）：{url}")
            data = response.read(MAX_BYTES + 1)
    except HTTPError as exc:
        raise FetchError(f"图片 HTTP {exc.code}：{url}") from exc
    except URLError as exc:
        raise FetchError(f"图片连不上：{exc.reason}（{url}）") from exc

    if len(data) > MAX_BYTES:
        raise FetchError(f"图片超过 {MAX_BYTES // 1024 // 1024} MB：{url}")
    suffix = SUFFIX_BY_TYPE.get(content_type) or Path(url.split("?")[0]).suffix.lower() or ".jpg"
    return data, suffix


def save_to_media(data: bytes, suffix: str) -> str:
    """落到 `data/media/`，返回可直接填进 JSON 的站内地址。"""
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    name = f"{uuid4().hex}{suffix}"
    (settings.media_dir / name).write_bytes(data)
    return f"/media/{name}"


# --------------------------------------------------------------------- 选图


def resolve(
    html: str,
    page_url: str,
    *,
    series: str,
    entry_specific: bool,
    aliases: tuple[str, ...] | list[str] = (),
) -> Resolution:
    """从页面里挑出"这一条上新"的图和详情页链接。

    两档，优先级不可反：

    1. **商品块**：列表页里每张商品图都包在指向详情页的 ``<a>`` 里。顺着这层结构抽，
       图和详情页链接一次拿齐，不用猜。
    2. **纯图片候选**：页面里没有商品链接结构时（品牌官网常见），退回按"近旁文字里
       有没有系列名"配对。

    ``entry_specific`` 为真表示这个页面就是"这一件商品"的详情页，此时整页的图都属于
    它，不需要按系列名配对。
    """
    resolution = Resolution(
        candidates=images.extract_candidates(html, page_url),
        products=images.extract_taobao_products(html, page_url),
    )

    if resolution.products:
        picked: images.Product | None = None
        if entry_specific and len(resolution.products) == 1:
            picked = resolution.products[0]
        else:
            matched = images.match_products(resolution.products, series, aliases)
            picked = matched[0] if matched else None
        if picked is not None:
            resolution.image_url = picked.image_url
            resolution.item_link = picked.item_url
            resolution.price = picked.price
            return resolution

    usable = [c for c in resolution.candidates if c.score >= -1.0]
    if entry_specific:
        usable.sort(key=lambda candidate: -candidate.score)
        if usable:
            resolution.image_url = usable[0].url
        return resolution

    matched = images.match_series(usable, series, aliases)
    if matched:
        resolution.image_url = matched[0].url
    return resolution


def resolve_catalog(
    text: str,
    page_url: str,
    *,
    series: str,
    catalog: str = "html",
    item_href_pattern: str = "",
    aliases: tuple[str, ...] | list[str] = (),
) -> Resolution:
    """从**品牌官网的商品目录**里配对。

    两套解析器按 ``catalog`` 选：

    * ``shopify`` —— 官方公开的商品 JSON（``/products.json``），标题、主图、商品地址
      都是结构化的，最可靠；
    * ``html`` —— 普通页面，按 ``item_href_pattern`` 认出哪条 ``<a>`` 是商品页。

    JSON 解析不出来（不是 Shopify、或接口返回了 HTML）自动退回 HTML 路径，
    而不是抛错——"这家店不是 Shopify"不是故障。
    """
    products: list[images.Product] = []
    if catalog == "shopify":
        products = images.parse_shopify_products(text, page_url)
    if not products:
        products = images.extract_catalog(text, page_url, item_href_pattern=item_href_pattern)

    resolution = Resolution(products=products)
    matched = images.match_products(products, series, aliases)
    if matched:
        resolution.image_url = matched[0].image_url
        resolution.item_link = matched[0].item_url
        resolution.price = matched[0].price
    return resolution


# --------------------------------------------------------------------- 写回


def write_entry(
    path: Path,
    *,
    image: str | None = None,
    link: str | None = None,
    fields: dict[str, object] | None = None,
) -> dict:
    """只改传进来的字段，其余原样保留（含键顺序）。

    ``None`` 表示"别动这个字段"。字符串（含空串）表示"改成这个值"——两者语义不同，
    所以 image/link 的签名必须是 ``str | None`` 而不是 ``str = ""``。
    ``fields`` 用于"换成官网真实商品"这类整体改写：series / items / price_range /
    aliases 一起换掉，否则卡片上的名字和图说的不是同一件东西。
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    if image is not None:
        raw["image"] = image
    if link is not None:
        raw["link"] = link
    for key, value in (fields or {}).items():
        raw[key] = value
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return raw


def entry_path(newarrivals_dir: Path, entry_id: str) -> Path:
    """按内容找条目文件。文件名不一定等于 id（id 是全项目唯一的，文件名只是方便看）。"""
    direct = newarrivals_dir / f"{entry_id}.json"
    if direct.exists():
        return direct
    for path in sorted(newarrivals_dir.glob("*.json")):
        if path.name.startswith("_"):
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if str(raw.get("id") or "") == entry_id:
            return path
    return direct


def fetch_for_entry(entry, page_url: str = "", *, shop=None) -> Resolution:
    """一条龙：抓页面 → 选图 → 下载 → 落 media → 写回 JSON。

    两种来源，优先用 ``page_url``（用户当场粘贴的那一条）：

    * ``page_url`` 为空且品牌在 `content/shops` 里登记过 → 用登记的地址和解析器
      （品牌官网的商品目录，见 :func:`resolve_catalog`）；
    * 否则按 ``page_url`` 当成"某一页"，淘宝列表页走商品块、品牌官网走目录。

    失败一律抛 :class:`FetchError`，消息里要包含**页面上实际有哪些商品**——
    光说"没配上"等于让人猜。
    """
    aliases = getattr(entry, "aliases", ()) or ()

    if page_url:
        html = fetch_html(page_url)
        resolution = resolve(
            html,
            page_url,
            series=entry.series,
            entry_specific=bool(images.item_url_of(page_url)[0]),
            aliases=aliases,
        )
    elif shop is not None and shop.page_url:
        page_url = shop.page_url
        text = fetch_html(page_url)
        resolution = resolve_catalog(
            text,
            page_url,
            series=entry.series,
            catalog=shop.catalog,
            item_href_pattern=shop.item_href_pattern,
            aliases=aliases,
        )
    else:
        raise FetchError(
            "没有可用的页面地址：请粘贴这条商品的地址，"
            "或在 content/shops/<brand>.json 里填 page_url。"
        )

    if not resolution.image_url:
        titles = [product.context[:40] for product in resolution.products[:6] if product.context]
        hint = "\n  页面上的商品是：" + "；".join(titles) if titles else ""
        raise FetchError(
            "页面里没找到对得上「"
            + entry.series
            + "」的图和详情页。"
            + hint
            + "\n  可以在条目 JSON 里加 aliases（官网上的另一种写法），或换成这条商品的详情页地址。"
        )

    data, suffix = download_image(resolution.image_url, referer=page_url)
    stored = save_to_media(data, suffix)

    # 价格只在**条目还没写**的时候补：手录的价格（可能是国内代购价、定金价）
    # 不该被官网原价悄悄顶掉。想覆盖就自己改 JSON。
    # 币种跟着价格一起写——只写数字不写币种，就是把 55000 日元冒充成人民币。
    fields: dict[str, object] = {}
    if resolution.price and not str(getattr(entry, "price_range", "") or "").strip():
        fields["price_range"] = resolution.price
        if shop is not None and shop.currency:
            fields["currency"] = shop.currency

    write_entry(
        entry_path(settings.newarrivals_dir, entry.id),
        image=stored,
        link=resolution.item_link or None,
        fields=fields or None,
    )
    resolution.image_url = stored
    return resolution
