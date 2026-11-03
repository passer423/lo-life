"""从品牌官方商品页抓**商品级实测尺寸**，写回 `content/newarrivals/<id>.json`。

## 为什么是商品级，不是品牌级

AP 与 Baby 的官方口径都不是 S/M/L：

* AP 官方指南：全品以 JIS 9 号为基准，商品详情页给**这一件**的实测尺寸；
* Baby 官方指南：服装为单一尺码（日本 9 号 / US 6），商品页给实测区间。

所以尺寸是**商品级的事实**。把它们硬拆成品牌级 `S/M/L` 只能靠编，编出来的码数
会让用户按一个不存在的码下单。这个脚本只写 `newarrivals` 的 `size` 字段，不碰
`content/size/` 的品牌表。

## 默认只看不改

不加 `--write` 时只打印解析结果，不写文件。写回只在条目 `size` 为空时才发生；
已有数据要覆盖必须显式 `--overwrite`。抓错尺寸比没有尺寸更糟。

## 三种入口

1. `--brand ap` / `--brand baby`：逐条抓 `link` 里的官网商品页；
2. `--id <条目 id>`：只处理一条；
3. `--from-html 页面.html`：**零请求**，解析浏览器另存的详情页（淘宝/登录墙场景）；
4. `--from-page URL`：现抓一个指定页面（只配 `--id` 用）。

## 边界

* 只读 GET、固定间隔、不做重试；失败把原因原样打印；
* 只认带 `cm` / `㎝` 的尺寸，裸数字不当尺寸；
* 页面里出现多件商品的 `着丈` 时拒绝写入（可能是列表页，不是详情页）；
* `--from-html` 支持 UTF-8 / Shift_JIS（AP 的日文页）。

用法：

    python scripts/fetch_sizes.py --id 2026-10-ap-bainiao
    python scripts/fetch_sizes.py --brand ap
    python scripts/fetch_sizes.py --brand baby --write
    python scripts/fetch_sizes.py --id 2026-10-ap-bainiao --from-html 白鸟笼.html --write
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import re
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.domain import garment  # noqa: E402
from app.domain import newarrivals as na  # noqa: E402
from app.services import imagefetch  # noqa: E402
from app.services.imagefetch import FetchError  # noqa: E402

_SCRIPT_RE = re.compile(r"<(script|style|noscript)\b[^>]*>.*?</\1>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]{1,200}>")

# 本地另存页面里，尺寸段之后通常是搭配/素材/颜色等板块；切掉它们，避免把
# 关联商品的尺寸也算进来。
_SIZE_BLOCK_END = ("コーディネート例", "コーディネート", "色展開", "素材", "返品", "商品レビュー")


# 官方口径，写进 size.note；不把"品牌 xx 码"当成事实。
_OFFICIAL_NOTE = {
    "ap": "AP 官方以 JIS 9 号为基准；区间来自商品页实测，抽褶/可调结构可能带来弹性。",
    "baby": "Baby 官方服装为单一尺码（日本 9 号）；区间来自商品页实测，抽褶可能带来弹性。",
}


def html_to_text(raw: str) -> str:
    """去脚本/样式与标签，保留换行；尺寸文本本身是纯文本。"""
    text = _SCRIPT_RE.sub("\n", raw or "")
    text = _TAG_RE.sub("\n", text)
    text = html_lib.unescape(text)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def read_saved_html(path: Path) -> str:
    """浏览器另存的 AP 页面可能是 Shift_JIS；按顺序尝试，不静默乱码。"""
    for encoding in ("utf-8", "shift_jis", "cp932"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def size_block(text: str) -> str:
    """只取「サイズ」附近的一段，避免列表页/关联商品混进来。

    先找第一个「サイズ」，截到下一个「コーディネート例 / 素材 / 色展開 …」；
    这一段解析得出尺寸就用它，否则退回整页。**不猜**：整页也解析不出就报失败。
    """
    index = text.find("サイズ")
    if index != -1:
        block = text[index : index + 600]
        for marker in _SIZE_BLOCK_END:
            cut = block.find(marker, 1)
            if cut != -1:
                block = block[:cut]
        if garment.parse_measurements(block):
            return block
    return text


def _shopify_json_url(url: str) -> str:
    """Shopify 商品页 → ``.js`` JSON。只取当前商品，不掺关联推荐。"""
    return url.split("?", 1)[0].rstrip("/") + ".js"


def _entry_file(entry_id: str, directory: Path) -> tuple[Path, dict] | None:
    for path in sorted(directory.glob("*.json")):
        if path.name.startswith("_"):
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if str(raw.get("id")) == entry_id:
            return path, raw
    return None


def build_size(entry: na.NewArrival, page_text: str, *, url: str, today: date, local: bool) -> dict | None:
    source = f"{entry.brand_name} 官网商品 {url}（{today.isoformat()} 取）"
    if local:
        source += "（本地另存页面解析）"
    return garment.size_from_page(
        page_text,
        source=source,
        note=_OFFICIAL_NOTE.get(entry.brand, "区间来自官网商品页实测。"),
    )


def _load_page(entry: na.NewArrival, args: argparse.Namespace) -> tuple[str, str, bool]:
    """返回 ``(页面正文, 来源地址, 是否本地文件)``。"""
    if args.from_html:
        path = Path(args.from_html)
        text = size_block(html_to_text(read_saved_html(path)))
        return text, entry.link or f"file://{path.resolve()}", True

    url = args.from_page or entry.link
    if not url:
        raise FetchError("这条没有 link，无法定位官网商品页")

    # Baby 的商品页会把关联商品的尺寸一起渲染出来；`.js` 只返回当前商品。
    if entry.brand == "baby" and "store.babyssb.co.jp/products/" in url:
        payload = imagefetch.fetch_html(_shopify_json_url(url))
        try:
            description = json.loads(payload).get("description") or ""
        except json.JSONDecodeError as exc:
            raise FetchError(f"Shopify 商品 JSON 解析失败：{exc}") from exc
        return html_to_text(description), url, False

    return size_block(html_to_text(imagefetch.fetch_html(url))), url, False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从官方商品页抓商品级尺寸")
    parser.add_argument("--id", help="只处理这一条上新")
    parser.add_argument("--brand", help="只处理这个品牌的条目")
    parser.add_argument("--from-html", help="解析浏览器另存的详情页（零请求）")
    parser.add_argument("--from-page", help="现抓一个商品详情页（配合 --id）")
    parser.add_argument("--write", action="store_true", help="真的写回 JSON")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已有的 size")
    parser.add_argument("--include-sample", action="store_true", help="也处理 sample=true 的条目")
    parser.add_argument("--delay", type=float, default=2.0, help="条目之间的间隔秒数")
    parser.add_argument("--limit", type=int, default=0, help="最多处理几条（0 = 不限）")
    args = parser.parse_args(argv)

    if args.from_html or args.from_page:
        if not args.id:
            parser.error("--from-html / --from-page 必须配合 --id 使用")
        if args.from_html and args.from_page:
            parser.error("--from-html 与 --from-page 只能选一个")

    entries, problems = na.load_newarrivals(settings.newarrivals_dir)
    for problem in problems:
        print(f"  ⚠ {problem}")

    if args.id:
        entries = [entry for entry in entries if entry.id == args.id]
    if args.brand:
        entries = [entry for entry in entries if entry.brand == args.brand]
    if not args.include_sample:
        entries = [entry for entry in entries if not entry.sample]
    if args.limit > 0:
        entries = entries[: args.limit]

    if not entries:
        print("没有符合条件的条目。")
        return 0

    today = date.today()
    written = parsed = failed = 0

    for index, entry in enumerate(entries, start=1):
        if not entry.link and not (args.from_html or args.from_page):
            print(f"  - {entry.id}：没有 link，跳过（可用 --from-html 指定本地页面）")
            continue

        try:
            page_text, url, local = _load_page(entry, args)
        except (FetchError, OSError) as exc:
            failed += 1
            print(f"  ✗ {entry.id}：{exc}")
            continue

        # 列表页里会出现多个商品的「着丈」；拿第一条会配错衣服，所以直接拒绝。
        if page_text.count("着丈") > 1:
            failed += 1
            print(f"  ✗ {entry.id}：页面里有多件商品的尺寸，可能是列表页；请改用详情页")
            continue

        size = build_size(entry, page_text, url=url, today=today, local=local)
        if size is None:
            failed += 1
            print(f"  ✗ {entry.id}：没解析到带单位的尺寸")
            continue

        parsed += 1
        print(f"  → {entry.id}：{garment.summary(size)}")
        if entry.size:
            print(f"      现有：{garment.summary(entry.size)}")

        if not args.write:
            continue

        found = _entry_file(entry.id, settings.newarrivals_dir)
        if found is None:
            failed += 1
            print(f"  ✗ {entry.id}：找不到对应的 JSON 文件")
            continue
        path, raw = found
        if entry.size and not args.overwrite:
            print(f"      已有 size，未加 --overwrite，跳过写回")
            continue

        raw["size"] = size
        path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written += 1
        print(f"      已写入 {path.name}")

        if args.delay > 0 and index < len(entries):
            time.sleep(args.delay)

    print()
    print(f"解析 {parsed} 条，写回 {written} 条，失败 {failed} 条。")
    if not args.write and parsed:
        print("这是 dry-run：没有修改任何文件。确认无误后加 --write。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
