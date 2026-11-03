"""取图：候选抽取、过滤、系列名配对、域名策略、写回。

这一层最容易出的错不是"抛异常"，而是**悄悄配错图**——用户看到别人的推荐位，
还以为自己买的是那条裙子。所以测试钉的是：

* 图标/像素/占位图必须被过滤掉；
* 系列名没出现在附近文字里时**必须返回空**，不能"挑个最像的"；
* 淘宝/天猫带 `?` 的地址必须被拒绝（robots 的 `Disallow: /*?*`）；
* 写回 JSON 时不能碰其他字段。
"""

from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path

import pytest

from app.domain import images, newarrivals as na, shops as shop_table

ROOT = Path(__file__).resolve().parents[1]

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AAAwAB/AF+7V0dAAAAAElFTkSuQmCC"
)


def load_script():
    """按路径加载脚本模块（scripts/ 不是包，但函数要能单测）。"""
    spec = importlib.util.spec_from_file_location("fetch_images", ROOT / "scripts" / "fetch_images.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HTML = """
<html><body>
  <img src="/static/logo.png">
  <img src="//cdn.example.com/icons/star.svg">
  <img src="https://cdn.example.com/p/2026/ab-430x430.jpg" alt="白鸟笼">
  <div class="desc">AP 白鸟笼 立领无袖 JSK</div>
  <img data-ks-lazyload="https://cdn.example.com/p/2026/cd_800x800.jpg">
  <img srcset="small.jpg 1x, large.jpg 2x">
  <img src="data:image/gif;base64,R0lGOD">
</body></html>
"""


# --------------------------------------------------------------------- 抽取


def test_extract_resolves_relative_and_protocol_relative():
    urls = images.extract_image_urls(HTML, "https://shop.example.com/item/1")
    assert "https://shop.example.com/static/logo.png" in urls
    assert "https://cdn.example.com/icons/star.svg" in urls


def test_extract_reads_lazy_attributes():
    urls = images.extract_image_urls(HTML, "https://shop.example.com/item/1")
    assert "https://cdn.example.com/p/2026/cd_800x800.jpg" in urls


def test_extract_takes_last_srcset_entry():
    urls = images.extract_image_urls(HTML, "https://shop.example.com/item/1")
    assert "https://shop.example.com/item/large.jpg" in urls
    assert "https://shop.example.com/item/small.jpg" not in urls


def test_extract_skips_inline_data_urls():
    urls = images.extract_image_urls(HTML, "https://shop.example.com/item/1")
    assert not any(url.startswith("data:") for url in urls)


def test_extract_dedupes():
    html = '<img src="a.jpg"><img src="a.jpg"><img data-src="a.jpg">'
    assert images.extract_image_urls(html, "https://x.test/") == ["https://x.test/a.jpg"]


# --------------------------------------------------------------------- 过滤


def test_score_prefers_big_jpg_over_icon_and_thumbnail():
    big = images.score("https://cdn.example.com/p/ab-800x800.jpg")
    thumb = images.score("https://cdn.example.com/p/ab-120x120.jpg")
    icon = images.score("https://cdn.example.com/static/logo.png")
    assert big > thumb
    assert big > icon


def test_svg_is_rejected_outright():
    assert images.score("https://x.test/star.svg") < -1


def test_pick_candidates_drops_icons_and_keeps_order_on_ties():
    urls = [
        "https://x.test/logo.png",
        "https://x.test/a.jpg",
        "https://x.test/b.jpg",
        "https://x.test/star.svg",
    ]
    picked = images.pick_candidates(urls)
    assert picked == ["https://x.test/a.jpg", "https://x.test/b.jpg"]


# --------------------------------------------------------------------- 配对


def test_match_series_uses_nearby_text():
    candidates = images.extract_candidates(HTML, "https://shop.example.com/list")
    matched = images.match_series(candidates, "白鸟笼")
    assert matched
    assert matched[0].url == "https://cdn.example.com/p/2026/ab-430x430.jpg"


def test_match_series_ignores_spaces_in_page_title():
    candidates = images.extract_candidates(HTML, "https://shop.example.com/list")
    assert images.match_series(candidates, "白鸟 笼")


def test_match_series_returns_empty_instead_of_guessing():
    candidates = images.extract_candidates(HTML, "https://shop.example.com/list")
    assert images.match_series(candidates, "完全不存在的系列") == []


def test_match_series_requires_a_series_name():
    candidates = images.extract_candidates(HTML, "https://shop.example.com/list")
    assert images.match_series(candidates, "") == []


# --------------------------------------------------------------------- 域名策略


def test_taobao_url_with_query_is_refused():
    reason = images.policy_violation("https://s.taobao.com/search?q=jsk")
    assert "Disallow" in reason


def test_taobao_url_without_query_is_allowed():
    assert images.policy_violation("https://shop123456789.taobao.com/") == ""


def test_tmall_with_query_is_refused():
    assert images.policy_violation("https://list.tmall.com/search_product.htm?q=jsk")


def test_other_hosts_with_query_are_allowed():
    assert images.policy_violation("https://example.com/list?page=2") == ""


@pytest.mark.parametrize(
    "url",
    [
        "https://weibo.com/u/1234567890",
        "https://m.weibo.cn/u/1234567890",
        "https://weibo.com/1234567890/ABCdef",
    ],
)
def test_weibo_is_refused_entirely(url):
    """微博 robots 是 `User-Agent: *` + `Disallow: /`，整站不抓——包括官方账号。

    要取就走 --from-html（浏览器另存），那条路不发任何请求。
    """
    reason = images.policy_violation(url)
    assert "Disallow" in reason
    assert "--from-html" in reason


def test_weibo_image_cdn_is_not_a_page_fetch():
    """图床不在"整站禁抓"名单里：图是用户浏览器已经加载过的，脚本只是下载。"""
    assert images.policy_violation("https://wx1.sinaimg.cn/large/006abc.jpg") == ""


def test_weibo_size_segments_change_the_score():
    """微博把尺寸写在目录里：头像 /thumb150/ 必须低于商品图 /large/。"""
    avatar = images.score("https://wx1.sinaimg.cn/thumb150/006abc.jpg")
    original = images.score("https://wx1.sinaimg.cn/large/006abc.jpg")
    assert original > avatar
    assert avatar < -1          # 直接被过滤掉


def test_non_http_scheme_is_refused():
    assert images.policy_violation("file:///c:/x.html")


# --------------------------------------------------------------------- 店铺表


def write_shop(root: Path, brand: str, **overrides) -> Path:
    payload = {
        "brand": brand,
        "brand_name": brand,
        "shop_url": "",
        "page_url": "",
        "note": "",
        "source": "test",
    }
    payload.update(overrides)
    directory = root / "shops"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{brand}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return directory


def test_shop_loader_skips_underscore_and_reports_broken(workdir):
    directory = write_shop(workdir, "ap", shop_url="https://shop1.example.com")
    (directory / "_template.json").write_text("{ nope", encoding="utf-8")
    (directory / "bad.json").write_text("{ nope", encoding="utf-8")
    shops, problems = shop_table.load_shops(directory)
    assert set(shops) == {"ap"}
    assert len(problems) == 1


def test_shop_loader_drops_dangerous_urls(workdir):
    shops, _ = shop_table.load_shops(write_shop(workdir, "ap", shop_url="javascript:alert(1)"))
    assert shops["ap"].shop_url == ""


def test_link_for_prefers_entry_link():
    shops = {"ap": shop_table.Shop(brand="ap", shop_url="https://shop.example.com")}
    assert shop_table.link_for("ap", "https://item.example.com/1", shops) == (
        "https://item.example.com/1",
        "item",
    )


def test_link_for_falls_back_to_shop():
    shops = {"ap": shop_table.Shop(brand="ap", shop_url="https://shop.example.com")}
    assert shop_table.link_for("ap", "", shops) == ("https://shop.example.com", "shop")


def test_link_for_is_empty_when_nothing_known():
    assert shop_table.link_for("unknown", "", {}) == ("", "")


def test_shop_loader_reads_currency(workdir):
    """币种是店铺级事实：同一家店的报价单位不会逐条变。"""
    shops, _ = shop_table.load_shops(write_shop(workdir, "ap", currency="jpy"))
    assert shops["ap"].currency == "JPY"


def test_entry_currency_defaults_to_cny():
    entry = na.NewArrival(
        id="e", brand="b", brand_name="B", series="S", release_date="2026-10-20"
    )
    assert entry.currency == "CNY"


def test_loader_reads_currency_from_json(workdir):
    directory = workdir / "newarrivals"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "e1.json").write_text(
        json.dumps(
            {
                "id": "e1", "brand": "ap", "series": "X",
                "release_date": "2026-10-20", "price_range": "55000", "currency": "jpy",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    entries, problems = na.load_newarrivals(directory)
    assert problems == []
    assert entries[0].currency == "JPY"          # 统一大写，前端查表不用再转


def test_feed_row_carries_currency():
    row = na.feed([
        na.NewArrival(
            id="e1", brand="ap", brand_name="AP", series="白鸟笼",
            release_date="2026-10-20", price_range="55000", currency="JPY",
        )
    ])[0]
    assert row["currency"] == "JPY"
    assert row["price_range"] == "55000"


def test_feed_marks_link_kind(workdir):
    shops = {"ap": shop_table.Shop(brand="ap", shop_url="https://shop.example.com")}
    entry = na.NewArrival(
        id="e1", brand="ap", brand_name="AP", series="白鸟笼",
        release_date="2026-10-20", items=["JSK"], link="",
    )
    row = na.feed([entry], shops=shops)[0]
    assert row["link"] == "https://shop.example.com"
    assert row["link_kind"] == "shop"


def test_feed_entry_link_wins_over_shop(workdir):
    shops = {"ap": shop_table.Shop(brand="ap", shop_url="https://shop.example.com")}
    entry = na.NewArrival(
        id="e1", brand="ap", brand_name="AP", series="白鸟笼",
        release_date="2026-10-20", items=["JSK"], link="https://item.example.com/9",
    )
    row = na.feed([entry], shops=shops)[0]
    assert row["link"] == "https://item.example.com/9"
    assert row["link_kind"] == "item"


# --------------------------------------------------------------------- 详情页链接


@pytest.mark.parametrize(
    ("raw", "expected_id"),
    [
        ("https://item.taobao.com/item.htm?id=123456789", "123456789"),
        ("https://detail.tmall.com/item.htm?id=998877", "998877"),
        ("//item.taobao.com/item.htm?spm=a1z10&id=42&scm=x", "42"),
    ],
)
def test_item_url_normalizes_and_keeps_id(raw, expected_id):
    url, item_id = images.item_url_of(raw)
    assert item_id == expected_id
    assert url.endswith(f"item.htm?id={expected_id}")


def test_item_url_drops_tracking_params():
    """spm/scm 换一次点法就变一次，存进 JSON 等于存了个会过期的指纹。"""
    url, _ = images.item_url_of("https://item.taobao.com/item.htm?spm=a1z10&id=42&scm=x")
    assert "spm" not in url and "scm" not in url


@pytest.mark.parametrize(
    "raw",
    [
        "https://shop123.taobao.com/",                       # 店铺页，不是某一件商品
        "https://shop123.taobao.com/item.htm",               # 没有 id
        "https://item.taobao.com/item.htm?id=abc",           # id 不是数字
        "https://example.com/item.htm?id=1",                 # 不是淘宝
    ],
)
def test_item_url_rejects_non_item_urls(raw):
    assert images.item_url_of(raw) == ("", "")


LIST_PAGE = """
<div class="grid">
  <a href="//item.taobao.com/item.htm?spm=1&id=111">
    <img src="//img.alicdn.com/a_800x800.jpg"><span>AP 白鸟笼 JSK</span>
  </a>
  <a href="https://detail.tmall.com/item.htm?id=222">
    <img data-src="//img.alicdn.com/b_400x400.jpg">Baby 秋日 私语
  </a>
  <a href="//item.taobao.com/item.htm?id=333">
    <img src="//img.alicdn.com/icon_60x60.png">花与珍珠匣 缎带
  </a>
</div>
"""


def test_extract_products_pairs_link_and_image():
    products = images.extract_taobao_products(LIST_PAGE, "https://shop-x.taobao.com/new")
    assert [p.item_id for p in products] == ["111", "222"]
    assert products[0].image_url == "https://img.alicdn.com/a_800x800.jpg"
    # 只有图标的那一条被丢掉：它的图分低于阈值
    assert "333" not in [p.item_id for p in products]


def test_match_products_pairs_by_series_name():
    products = images.extract_taobao_products(LIST_PAGE, "https://shop-x.taobao.com/new")
    matched = images.match_products(products, "白鸟笼")
    assert len(matched) == 1
    assert matched[0].item_id == "111"


def test_match_products_tolerates_spaces_in_title():
    products = images.extract_taobao_products(LIST_PAGE, "https://shop-x.taobao.com/new")
    assert images.match_products(products, "秋日私语")[0].item_id == "222"


def test_match_products_returns_empty_instead_of_guessing():
    products = images.extract_taobao_products(LIST_PAGE, "https://shop-x.taobao.com/new")
    assert images.match_products(products, "不存在的系列") == []


def test_search_url_is_a_search_not_an_item():
    url = images.search_url_for("AP", "白鸟笼")
    assert url.startswith("https://s.taobao.com/search?q=")
    assert "item.htm" not in url


def test_feed_exposes_search_url_when_no_link():
    entry = na.NewArrival(
        id="e1", brand="ap", brand_name="AP", series="白鸟笼",
        release_date="2026-10-20", items=["JSK"], link="",
    )
    row = na.feed([entry])[0]
    assert row["link"] == ""
    assert row["search_url"].startswith("https://s.taobao.com/search")


# --------------------------------------------------------------------- 写回


def test_write_entry_only_touches_given_fields(workdir):
    """写回 JSON 时只改传进来的字段——手填的 note / sample 不能被动。

    实现在 `app/services/imagefetch.py`：脚本和「取图」按钮共用这一份，
    所以这里测的就是生产路径。
    """
    from app.services import imagefetch

    path = workdir / "e1.json"
    path.write_text(
        json.dumps(
            {
                "id": "e1",
                "brand": "ap",
                "series": "白鸟笼",
                "image": "",
                "link": "",
                "note": "手填的备注",
                "sample": True,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    imagefetch.write_entry(path, image="/media/x.jpg")
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["image"] == "/media/x.jpg"
    assert saved["link"] == ""              # 没传就不动
    assert saved["note"] == "手填的备注"
    assert saved["sample"] is True


def test_download_and_save_roundtrip(workdir):
    """对着本地 HTTP 服务跑一遍真实下载路径（不碰外网）。

    网络与写盘的实现在 `app/services/imagefetch.py`——脚本和接口共用同一份，
    所以这里测的就是生产路径。
    """
    from app.services import imagefetch

    import functools
    import http.server
    import threading

    (workdir / "cover.png").write_bytes(PNG_1X1)

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):        # 测试里不要刷屏
            return

    handler = functools.partial(Quiet, directory=str(workdir))
    try:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    except OSError as exc:                  # 受限环境不允许监听端口
        pytest.skip(f"无法起本地 HTTP 服务：{exc}")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        data, suffix = imagefetch.download_image(f"{base}/cover.png", referer=f"{base}/item")
        assert data == PNG_1X1
        assert suffix == ".png"

        url = imagefetch.save_to_media(data, suffix)
        assert url.startswith("/media/")
        assert (imagefetch.settings.media_dir / Path(url).name).exists()
    finally:
        server.shutdown()
        server.server_close()
