"""取图服务与 `POST /api/images/fetch`。

这一层的契约是三条"不许含糊"：

* 没配地址、id 不存在、被 robots 拒绝时，**各自给不同的状态码和不同的原因**；
* 配不上系列时**不写文件**（宁可留空，不给错图）；
* 成功时 `image` 与 `link` 一起写进去——图有了但点进去是搜索页，是另一种坏体验。
"""

from __future__ import annotations

import base64
import functools
import http.server
import json
import threading
from pathlib import Path

import pytest

from app.config import settings
from app.domain import images
from app.services import imagefetch

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AAAwAB/AF+7V0dAAAAAElFTkSuQmCC"
)


@pytest.fixture()
def site_server(workdir):
    """本地假站点：一个"店铺上新列表页" + 两张图。全程不碰外网。"""
    site = workdir / "site"
    site.mkdir(parents=True, exist_ok=True)
    (site / "bainiao_800x800.png").write_bytes(PNG_1X1)
    (site / "qiuri_800x800.png").write_bytes(PNG_1X1)

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            return

    handler = functools.partial(Quiet, directory=str(site))
    try:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    except OSError as exc:
        pytest.skip(f"无法起本地 HTTP 服务：{exc}")
    threading.Thread(target=server.serve_forever, daemon=True).start()

    base = f"http://127.0.0.1:{server.server_address[1]}"
    page = (
        '<div><a href="//item.taobao.com/item.htm?spm=1&id=111">'
        f'<img src="{base}/bainiao_800x800.png"><span>AP 白鸟笼 JSK</span></a>'
        '<a href="//item.taobao.com/item.htm?id=222">'
        f'<img src="{base}/qiuri_800x800.png"><span>AP 秋日私语</span></a></div>'
    )
    (site / "list.html").write_text(page, encoding="utf-8")

    try:
        yield {"base": base, "page": f"{base}/list.html", "list_html": page}
    finally:
        server.shutdown()
        server.server_close()


# --------------------------------------------------------------------- resolve


def test_resolve_prefers_product_block_with_its_item_link(site_server):
    resolution = imagefetch.resolve(
        site_server["list_html"], site_server["base"], series="白鸟笼", entry_specific=False
    )
    assert resolution.image_url.endswith("bainiao_800x800.png")
    assert resolution.item_link == "https://item.taobao.com/item.htm?id=111"


def test_resolve_returns_nothing_when_series_not_found(site_server):
    resolution = imagefetch.resolve(
        site_server["list_html"], site_server["base"], series="不存在的系列", entry_specific=False
    )
    assert resolution.image_url == ""


def test_resolve_on_item_page_ignores_series(site_server):
    """详情页整页都属于这一件商品，不需要按系列名配对。"""
    single = (
        '<a href="//item.taobao.com/item.htm?id=333">'
        f'<img src="{site_server["base"]}/bainiao_800x800.png"><span>随便写的标题</span></a>'
    )
    resolution = imagefetch.resolve(single, site_server["base"], series="对不上的系列", entry_specific=True)
    assert resolution.image_url.endswith("bainiao_800x800.png")
    assert resolution.item_link.endswith("id=333")


def test_resolve_on_weibo_saved_page_picks_product_image():
    """微博保存页：正文里有系列名，图床是 /large/，头像 /thumb150/ 必须被过滤。

    这条就是"国内品牌走官方微博"的实际路径——**页面是用户自己另存的**，脚本只解析文件，
    一个请求都不发（微博 robots 是 Disallow: /，自动抓不做）。
    """
    html = """
    <div class="card">
      <img src="https://wx1.sinaimg.cn/thumb150/006avatar.jpg">
      <p>花与珍珠匣 缎带与齿轮 系列 本次上新 10/05 全款预定</p>
      <img src="https://wx2.sinaimg.cn/large/006product.jpg">
    </div>
    """
    resolution = imagefetch.resolve(
        html, "https://weibo.com/u/1234567890", series="缎带与齿轮", entry_specific=False
    )
    assert resolution.image_url == "https://wx2.sinaimg.cn/large/006product.jpg"


def test_resolve_matches_by_alias():
    """同一系列在中文条目和日本官网是两个名字，只认一个就永远配不上。"""
    html = (
        '<a href="https://shop.example.com/products/x1">'
        '<img src="https://cdn.example.com/a_800x800.jpg"><span>白鳥籠 ジャンパースカート</span></a>'
    )
    assert imagefetch.resolve(html, "https://shop.example.com/list", series="白鸟笼", entry_specific=False).image_url == ""
    resolution = imagefetch.resolve(
        html,
        "https://shop.example.com/list",
        series="白鸟笼",
        entry_specific=False,
        aliases=["白鳥籠"],
    )
    assert resolution.image_url.endswith("a_800x800.jpg")


def test_extract_catalog_merges_split_anchors():
    """官网常见：图和标题是两个指向同一商品的 <a>。按地址归并才不会得到两条残废记录。"""
    html = (
        '<a href="/Detail.aspx?pid=1"><img src="/img/1_LL.jpg"></a>'
        '<a href="/Detail.aspx?pid=1">Lovely Princess JSK</a>'
    )
    products = images.extract_catalog(html, "https://shop.example.com/list/", item_href_pattern="Detail.aspx")
    assert len(products) == 1
    assert products[0].image_url == "https://shop.example.com/img/1_LL.jpg"
    assert "Lovely Princess" in products[0].context


def test_parse_shopify_products_reads_title_image_and_handle():
    payload = json.dumps(
        {
            "products": [
                {
                    "handle": "p22oj208",
                    "title": "【予約】Dark Night Sweets柄 ジャンパースカートII",
                    "images": [{"src": "https://cdn.shopify.com/x/1.jpg"}],
                }
            ]
        }
    )
    products = images.parse_shopify_products(payload, "https://store.example.com/products.json?limit=250")
    assert products[0].item_url == "https://store.example.com/products/p22oj208"
    assert products[0].image_url == "https://cdn.shopify.com/x/1.jpg"
    assert "Dark Night Sweets" in products[0].context


def test_parse_shopify_products_returns_empty_on_html():
    """不是 Shopify 就退回 HTML 路径，不要把"这家店不是 Shopify"变成异常。"""
    assert images.parse_shopify_products("<html>nope</html>", "https://x.test/") == []


# --------------------------------------------------------------------- 价格


AP_LIKE = """
<div class="item">
  <a href="/Detail.aspx?pid=1"><img src="/img/1_LL.jpg"></a>
  <a href="/Detail.aspx?pid=1">Lovely Princessジャンパースカート</a>
  <span>&yen;55,000</span>
</div>
<div class="item">
  <a href="/Detail.aspx?pid=2"><img src="/img/2_LL.jpg"></a>
  <a href="/Detail.aspx?pid=2">リボンポンポンベレー</a>
  <span>&yen;15,400</span>
</div>
"""


def test_extract_catalog_assigns_price_to_the_right_product():
    """价格不在 <a> 里，是页面上一段独立文本。按位置区间配，才不会张冠李戴。"""
    products = images.extract_catalog(AP_LIKE, "https://shop.example.com/list/", item_href_pattern="Detail.aspx")
    assert [product.price for product in products] == ["55000", "15400"]


def test_extract_catalog_does_not_steal_the_next_price():
    """这一件没写价格，就不能把下一件的价格顶上来。"""
    html = (
        '<a href="/Detail.aspx?pid=1"><img src="/img/1_LL.jpg"></a>'
        '<a href="/Detail.aspx?pid=1">无标价那一件</a>'
        '<a href="/Detail.aspx?pid=2"><img src="/img/2_LL.jpg"></a>'
        '<a href="/Detail.aspx?pid=2">有标价那一件</a>'
        "<span>&yen;9,900</span>"
    )
    products = images.extract_catalog(html, "https://shop.example.com/", item_href_pattern="Detail.aspx")
    assert [product.price for product in products] == ["", "9900"]


def test_bare_numbers_are_not_prices():
    """商品编号、尺码、年份都是裸数字，不能当价格抓。"""
    html = (
        '<a href="/Detail.aspx?pid=1"><img src="/img/1_LL.jpg"></a>'
        '<a href="/Detail.aspx?pid=1">JSK 262J10 尺寸 S M L 2026</a>'
    )
    products = images.extract_catalog(html, "https://shop.example.com/", item_href_pattern="Detail.aspx")
    assert products[0].price == ""


def test_shopify_price_keeps_integer_part_only():
    payload = json.dumps(
        {
            "products": [
                {
                    "handle": "p22oj208",
                    "title": "Dark Night Sweets柄 ジャンパースカートII",
                    "images": [{"src": "https://cdn.shopify.com/1.jpg"}],
                    "variants": [{"price": "41800.00"}],
                }
            ]
        }
    )
    products = images.parse_shopify_products(payload, "https://store.example.com/products.json")
    assert products[0].price == "41800"


def test_resolve_catalog_carries_price():
    resolution = imagefetch.resolve_catalog(
        AP_LIKE,
        "https://shop.example.com/list/",
        series="リボンポンポンベレー",
        catalog="html",
        item_href_pattern="Detail.aspx",
    )
    assert resolution.price == "15400"



def test_ssl_context_keeps_certificate_verification_on():
    """换 CA 包是为了能连上，不是为了让校验失效。

    这台机器的 Anaconda Python 默认 cafile 指向一个不存在的路径，于是所有 https
    都 CERTIFICATE_VERIFY_FAILED——症状长得像"网站挂了"。修法是用 certifi 的 bundle，
    **绝不能**退化成 CERT_NONE。
    """
    import ssl

    context = imagefetch.ssl_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_fetch_html_refuses_taobao_search_url():
    with pytest.raises(imagefetch.FetchError, match="Disallow"):
        imagefetch.fetch_html("https://s.taobao.com/search?q=jsk")


def test_write_entry_leaves_untouched_fields_alone(workdir):
    path = workdir / "e.json"
    path.write_text(
        json.dumps({"id": "e", "image": "", "link": "", "note": "手填"}, ensure_ascii=False),
        encoding="utf-8",
    )
    imagefetch.write_entry(path, image="/media/x.jpg")
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved == {"id": "e", "image": "/media/x.jpg", "link": "", "note": "手填"}


def test_entry_path_finds_file_by_content_id(workdir):
    (workdir / "weird-name.json").write_text(
        json.dumps({"id": "2026-10-ap-bainiao"}), encoding="utf-8"
    )
    assert imagefetch.entry_path(workdir, "2026-10-ap-bainiao").name == "weird-name.json"


# --------------------------------------------------------------------- 接口


@pytest.fixture()
def temp_content(workdir, monkeypatch):
    """把 CONTENT_DIR 指到临时目录：接口会写回 JSON，绝不能碰真实 content/。"""
    content = workdir / "content"
    (content / "newarrivals").mkdir(parents=True, exist_ok=True)
    (content / "shops").mkdir(parents=True, exist_ok=True)
    (content / "newarrivals" / "e1.json").write_text(
        json.dumps(
            {
                "id": "e1",
                "brand": "ap",
                "brand_name": "AP",
                "series": "白鸟笼",
                "release_date": "2026-10-20",
                "items": ["JSK"],
                "image": "",
                "link": "",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "content_dir", content)
    return content


def test_endpoint_rejects_unknown_id(client, temp_content):
    response = client.post("/api/images/fetch", json={"id": "没有这条", "page_url": ""})
    assert response.status_code == 404


def test_endpoint_asks_for_url_when_nothing_registered(client, temp_content):
    response = client.post("/api/images/fetch", json={"id": "e1"})
    assert response.status_code == 400
    assert "page_url" in response.json()["detail"]


def test_endpoint_reports_robots_refusal_as_502(client, temp_content):
    response = client.post(
        "/api/images/fetch",
        json={"id": "e1", "page_url": "https://s.taobao.com/search?q=ap"},
    )
    assert response.status_code == 502
    assert "Disallow" in response.json()["detail"]


def test_endpoint_fetches_image_and_link(client, temp_content, site_server):
    response = client.post("/api/images/fetch", json={"id": "e1", "page_url": site_server["page"]})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["image"].startswith("/media/")
    assert body["link"] == "https://item.taobao.com/item.htm?id=111"

    # 文件真的落盘了，JSON 也真的改了
    assert (settings.media_dir / Path(body["image"]).name).exists()
    saved = json.loads((temp_content / "newarrivals" / "e1.json").read_text(encoding="utf-8"))
    assert saved["image"] == body["image"]
    assert saved["link"] == body["link"]


def test_endpoint_leaves_files_alone_when_series_does_not_match(client, temp_content, site_server):
    """配不上就什么都不写——留空比贴错图好。"""
    (temp_content / "newarrivals" / "e1.json").write_text(
        json.dumps(
            {"id": "e1", "brand": "ap", "brand_name": "AP", "series": "对不上的系列",
             "release_date": "2026-10-20", "items": ["JSK"], "image": "", "link": ""},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    response = client.post("/api/images/fetch", json={"id": "e1", "page_url": site_server["page"]})
    assert response.status_code == 502
    saved = json.loads((temp_content / "newarrivals" / "e1.json").read_text(encoding="utf-8"))
    assert saved["image"] == ""
    assert saved["link"] == ""
