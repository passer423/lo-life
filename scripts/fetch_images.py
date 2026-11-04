"""按品牌/条目取图：把上新条目里的 `image` 从空串变成真图，顺便写上淘宝详情页链接。

## 三种入口，风险从低到高

1. ``--from-html 页面.html`` —— **零请求**。你在浏览器里（哪怕是登录状态、哪怕是
   淘宝的店铺页）把页面"另存为"到本地，脚本只解析这个文件。风控、登录墙、防盗链
   全都不存在，因为发请求的是你的浏览器，而且是你本人操作。
2. ``--brand ap`` —— 用 `content/shops/ap.json` 里登记的 `page_url` 抓一次。
   同一个品牌的所有上新共用这一页，靠"图片旁边的文字里有系列名"来配对。
3. ``--from-page URL`` —— 现抓一个指定页面，给"--id + 商品详情页"这种精确场景用。

## 默认只看不改

不加 ``--write`` 时**不下载、不改文件**，只把候选打印出来（带分数和附近文字）。
抓错图比没有图更糟，用户会照着别人的裙子下单。

## 会被拒绝的地址

`taobao.com` / `tmall.com` 的 robots.txt 是 ``Disallow: /*?*``——带查询参数的地址
一律不抓。脚本遇到直接退出并说明原因，**没有跳过开关**。

## 用法

    python scripts/fetch_images.py --id 2026-10-ap-bainiao --from-page https://...
    python scripts/fetch_images.py --id 2026-10-ap-bainiao --from-html 白鸟笼.html
    python scripts/fetch_images.py --brand ap
    python scripts/fetch_images.py --brand ap --write
    python scripts/fetch_images.py --id 2026-10-ap-bainiao --pick 3 --write

网络与写盘的实现在 `app/services/imagefetch.py`：界面上的「取图」按钮用的是同一份，
两条路不会各自长出一套筛选规则。
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.domain import images  # noqa: E402
from app.domain import newarrivals as na  # noqa: E402
from app.domain import shops as shop_table  # noqa: E402
from app.services import imagefetch  # noqa: E402
from app.services.imagefetch import FetchError  # noqa: E402


def show_products(products: list[images.Product], chosen: images.Product | None, limit: int = 12) -> None:
    for index, product in enumerate(products[:limit], start=1):
        mark = "→" if product is chosen else " "
        print(f" {mark}{index}. {product.item_url}")
        print(f"      {product.image_url}")
        if product.context:
            print(f"      近旁：…{product.context[:50]}…")
    if len(products) > limit:
        print(f"  … 还有 {len(products) - limit} 个未显示（--pick 仍可指定）")


def show_candidates(candidates: list[images.Candidate], limit: int = 12) -> list[images.Candidate]:
    """打印图片候选。**编号与 `--pick` 用的是同一个列表**（可用、文档顺序），
    否则用户看到"第 2 个"却选中了另一个，这种错没人能自己发现。"""
    usable = [c for c in candidates if c.score >= -1.0]
    rejected = len(candidates) - len(usable)

    for index, candidate in enumerate(usable[:limit], start=1):
        text = candidate.context[:60].replace("\n", " ")
        print(f"  {index}. [{candidate.score:+.0f}] {candidate.url}")
        if text:
            print(f"       近旁：…{text}…")
    if len(usable) > limit:
        print(f"  … 还有 {len(usable) - limit} 个可用候选未显示（--pick 仍可指定它们）")
    if rejected:
        print(f"  已过滤 {rejected} 个（图标 / 占位图 / svg / data: 内联图）")
    return usable


def _resolve(html: str, page_url: str, entry, args: argparse.Namespace, shops) -> imagefetch.Resolution:
    """选解析器：给了具体页面就按页面走，用登记地址就按品牌官网的商品目录走。

    这两条路的关键差别不是"抓哪"，而是**"哪条 <a> 算商品页"**：
    淘宝是 `item.htm`，AP 官网是 `ProductDetail.aspx`，BABY 是 Shopify 的
    `products.json`。写死一个就会把另一家的图全判成噪声。
    """
    aliases = entry.aliases
    if args.from_page or args.from_html:
        return imagefetch.resolve(
            html,
            page_url,
            series=entry.series,
            entry_specific=entry_specific_of(args),
            aliases=aliases,
        )

    shop = shops.get(entry.brand)
    return imagefetch.resolve_catalog(
        html,
        page_url,
        series=entry.series,
        catalog=shop.catalog if shop else "html",
        item_href_pattern=shop.item_href_pattern if shop else "",
        aliases=aliases,
    )


def entry_specific_of(args: argparse.Namespace) -> bool:
    return bool(args.id and (args.from_page or args.from_html))


def select_entries(entries: list[na.NewArrival], args: argparse.Namespace) -> list[na.NewArrival]:
    if args.id:
        picked = [entry for entry in entries if entry.id == args.id]
        if not picked:
            raise SystemExit(f"没有这个 id：{args.id}\n可用的：{', '.join(e.id for e in entries)}")
        return picked
    if args.brand:
        picked = [entry for entry in entries if entry.brand == args.brand]
        if not picked:
            raise SystemExit(f"没有这个品牌的条目：{args.brand}")
        if not args.include_filled:
            picked = [entry for entry in picked if not entry.image]
        return picked
    if args.all:
        return [entry for entry in entries if args.include_filled or not entry.image]
    raise SystemExit("要指定范围：--id <条目 id> / --brand <品牌 id> / --all")


def source_url_for(entry: na.NewArrival, args: argparse.Namespace, shops: dict[str, shop_table.Shop]) -> str:
    if args.from_page:
        return args.from_page
    shop = shops.get(entry.brand)
    if shop is not None and shop.page_url:
        return shop.page_url
    raise SystemExit(
        f"「{entry.brand}」没有登记页面地址。\n"
        f"  要么加 content/shops/{entry.brand}.json 里的 page_url，\n"
        f"  要么用 --from-page / --from-html 指定。"
    )


def main() -> int:
    # Windows 控制台默认是 GBK（cp936）：页面的"附近文字"里只要混进一个 BOM 或表情，
    # print 就会 UnicodeEncodeError 把整个脚本崩掉。这里保留原编码（中文照常显示），
    # 只把编不出来的字符换成 ?，绝不让"打印失败"变成一个失败原因。
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(errors="replace")

    parser = argparse.ArgumentParser(
        description="按品牌/条目取图，默认只打印候选（dry-run）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--id", help="只处理这一条上新")
    parser.add_argument("--brand", help="处理这个品牌下所有没图的条目")
    parser.add_argument("--all", action="store_true", help="处理所有没图的条目")
    parser.add_argument("--from-page", help="现抓这个页面")
    parser.add_argument("--from-html", help="解析本地保存的页面（零请求）")
    parser.add_argument("--pick", type=int, help="取候选里的第 N 个（1 起），默认取最优")
    parser.add_argument("--write", action="store_true", help="真的下载并写回 JSON")
    parser.add_argument("--link", help="顺便把 link 也写上（原始商品地址）")
    parser.add_argument("--include-filled", action="store_true", help="连已经有图/有链接的也处理")
    args = parser.parse_args()

    entries, problems = na.load_newarrivals(settings.newarrivals_dir)
    for problem in problems:
        print(f"[跳过] {problem}", file=sys.stderr)

    shops, shop_problems = shop_table.load_shops(settings.shops_dir)
    for problem in shop_problems:
        print(f"[店铺表] {problem}", file=sys.stderr)

    targets = select_entries(entries, args)
    if not targets:
        print("没有需要处理的条目（是否都已有图？加 --include-filled 可强制处理）。")
        return 0
    if args.from_html and len(targets) > 1:
        # 一个页面 = 一个商品。拿同一份 HTML 往多条上套，只会把同一张图贴到所有条目上。
        raise SystemExit("--from-html 一次只能配一条（一个页面 = 一个商品），请用 --id")

    pages: dict[str, str] = {}
    failures = 0

    for entry in targets:
        try:
            if args.from_html:
                html = Path(args.from_html).read_text(encoding="utf-8", errors="replace")
                page_url = args.from_page or f"file:///{Path(args.from_html).resolve()}"
            else:
                page_url = source_url_for(entry, args, shops)
                if page_url not in pages:
                    pages[page_url] = imagefetch.fetch_html(page_url)
                html = pages[page_url]
        except (FetchError, OSError, SystemExit) as exc:
            print(f"[失败] {entry.id}：{exc}", file=sys.stderr)
            failures += 1
            continue

        resolution = _resolve(html, page_url, entry, args, shops)

        print(f"\n{entry.id} · {entry.brand_name} {entry.series}  ← {page_url}")

        if resolution.products:
            show_products(resolution.products, _product_of(resolution))
        else:
            show_candidates(resolution.candidates)

        image_url, item_link = resolution.image_url, resolution.item_link

        if args.pick:
            # --pick 的编号池必须和上面打印出来的列表是同一个，否则用户选"第 2 个"
            # 会拿到另一个——这种错没有任何提示，只能靠人肉发现。
            if resolution.products:
                pool = [(product.image_url, product.item_url) for product in resolution.products]
            else:
                pool = [(candidate.url, "") for candidate in resolution.candidates if candidate.score >= -1.0]
            if not 1 <= args.pick <= len(pool):
                print(f"[失败] {entry.id}：--pick {args.pick} 超出范围（{len(pool)} 个）", file=sys.stderr)
                failures += 1
                continue
            image_url, picked_link = pool[args.pick - 1]
            item_link = picked_link

        if not image_url:
            print("  → 没配上图（页面里找不到含系列名的图/商品）。可用 --pick N 手动指定。", file=sys.stderr)
            failures += 1
            continue

        if item_link:
            print(f"  → 详情页：{item_link}")
        print(f"  → 选中图：{image_url}")

        if not args.write:
            print("  → dry-run：没下载、没改文件。加 --write 生效。")
            continue

        try:
            data, suffix = imagefetch.download_image(image_url, referer=page_url)
            stored = imagefetch.save_to_media(data, suffix)
        except FetchError as exc:
            print(f"[失败] {entry.id}：{exc}", file=sys.stderr)
            failures += 1
            continue

        # 链接优先级：命令行显式给的 > 页面里抽到的详情页 > 不动原来的
        final_link = args.link or item_link or None
        imagefetch.write_entry(
            imagefetch.entry_path(settings.newarrivals_dir, entry.id),
            image=stored,
            link=final_link,
        )
        print(f"  → 已写入 {entry.id}.json：image={stored}"
              + (f"，link={final_link}" if final_link else ""))

    if failures:
        print(f"\n{failures} 条没处理成功。", file=sys.stderr)
        return 1
    if args.write:
        print("\n完成。刷新页面即可看到（列表每次请求现读 JSON，不用重启）。")
    return 0


def _product_of(resolution: imagefetch.Resolution) -> images.Product | None:
    """默认选中的那个商品块（用于在列表里打 → 标记）。"""
    for product in resolution.products:
        if product.image_url == resolution.image_url:
            return product
    return None


if __name__ == "__main__":
    raise SystemExit(main())
