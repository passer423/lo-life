"""扫描 Baby 官网商品目录，生成**待确认**的上新草稿。

## 为什么不是直接写进 content/newarrivals

官网 `products.json` 是公开结构化数据，但"有这条商品"不等于"它是一条值得展示的
上新"：发售日、系列名、风格、款式都可能需要人看一眼。所以这里分两步：

1. `--write` 只写 `content/_drafts/newarrivals/<id>.json`；
2. 人工确认后 `--approve <id>` 才移进 `content/newarrivals/`。

自动采集只负责"发现 + 抄官方字段"，不负责替人做判断。草稿目录和正式内容分开，
`load_newarrivals()` 不会读到草稿，推荐页也不会出现未确认的条目。

## 用法

    python scripts/scan_baby.py --days 30                 # dry-run
    python scripts/scan_baby.py --days 30 --write         # 写草稿
    python scripts/scan_baby.py --list                    # 看草稿
    python scripts/scan_baby.py --approve <草稿 id>        # 确认一条
    python scripts/scan_baby.py --from-json baby.json      # 429/离线时解析本地 JSON

## 数据口径

* 来源：`content/shops/baby.json` 里的官方 `products.json`；
* 只收 BABY / ALICE and the PIRATES；
* 品类从标题/商品类型推断，风格留空——**不猜**；
* 价格、链接、图片、商品级尺寸来自官方字段，`source` 写清日期；
* 已存在的条目按商品 handle 去重，不会重复起草。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.domain import baby_catalog, newarrivals as na  # noqa: E402
from app.services import imagefetch  # noqa: E402
from app.services.imagefetch import FetchError  # noqa: E402

DEFAULT_CATALOG = "https://store.babyssb.co.jp/products.json?limit=250"


def drafts_dir() -> Path:
    return settings.content_dir / "_drafts" / "newarrivals"


def shop_catalog_url() -> str:
    path = settings.shops_dir / "baby.json"
    if not path.exists():
        return DEFAULT_CATALOG
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return DEFAULT_CATALOG
    return str(payload.get("page_url") or DEFAULT_CATALOG)


def load_payload(from_json: str = "") -> tuple[dict, str]:
    if from_json:
        path = Path(from_json)
        return json.loads(path.read_text(encoding="utf-8")), f"file://{path.resolve()}"
    url = shop_catalog_url()
    return json.loads(imagefetch.fetch_html(url)), url


def existing_handles() -> set[str]:
    entries, _problems = na.load_newarrivals(settings.newarrivals_dir)
    return {
        baby_catalog.handle_from_link(entry.link).lower()
        for entry in entries
        if baby_catalog.handle_from_link(entry.link)
    }


def write_drafts(drafts: list[dict], *, overwrite: bool) -> int:
    folder = drafts_dir()
    folder.mkdir(parents=True, exist_ok=True)
    written = 0
    for draft in drafts:
        path = folder / f"{draft['id']}.json"
        if path.exists() and not overwrite:
            print(f"  跳过 {draft['id']}：草稿已存在（--overwrite 才覆盖）")
            continue
        path.write_text(json.dumps(draft, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written += 1
        print(f"  → {path.relative_to(ROOT)}")
    return written


def list_drafts() -> int:
    folder = drafts_dir()
    if not folder.exists():
        print("还没有草稿。先跑 python scripts/scan_baby.py --write")
        return 0
    paths = sorted(folder.glob("*.json"))
    if not paths:
        print("草稿目录是空的。")
        return 0
    for path in paths:
        try:
            draft = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"  ✗ {path.name}：不是合法 JSON")
            continue
        print(
            f"  {draft.get('id')} | {draft.get('release_date')} | "
            f"{draft.get('price_range') or '未标价'} | {draft.get('series')}"
        )
    print(f"\n共 {len(paths)} 条草稿。确认后：python scripts/scan_baby.py --approve <id>")
    return 0


def approve(draft_id: str) -> int:
    folder = drafts_dir()
    source = folder / f"{draft_id}.json"
    if not source.exists():
        print(f"找不到草稿：{source}")
        return 1

    target = settings.newarrivals_dir / f"{draft_id}.json"
    if target.exists():
        print(f"正式内容里已经有 {target.name}，拒绝覆盖。")
        return 1

    # 用正式的 loader 验一遍：缺字段/日期不合法/风格不在词表都会被它发现。
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copy(source, Path(tmp) / source.name)
        entries, problems = na.load_newarrivals(Path(tmp))

    if problems or not entries:
        print("草稿没有通过校验，未移入正式内容：")
        for problem in problems:
            print(f"  ✗ {problem}")
        return 1

    settings.newarrivals_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))
    print(f"已确认：{target.relative_to(ROOT)}")
    print("推荐页每次请求现读 content/newarrivals，不需要重启。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="扫描 Baby 官网目录，生成待确认的上新草稿")
    parser.add_argument("--days", type=int, default=30, help="只收最近 N 天发布的商品")
    parser.add_argument("--limit", type=int, default=20, help="单轮最多生成几条草稿")
    parser.add_argument("--write", action="store_true", help="把草稿写到 content/_drafts/newarrivals")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已存在的草稿")
    parser.add_argument("--from-json", default="", help="解析本地保存的 products.json（离线/429）")
    parser.add_argument("--list", action="store_true", help="列出已有草稿")
    parser.add_argument("--approve", metavar="ID", default="", help="确认一条草稿，移入正式内容")
    args = parser.parse_args(argv)

    if args.list:
        return list_drafts()
    if args.approve:
        return approve(args.approve)

    try:
        payload, source = load_payload(args.from_json)
    except FetchError as exc:
        # fetch_html 的完整文案是给图片抓取写的（会建议 --from-html）；
        # products.json 是 JSON，正确走法是浏览器另存为 .json 后 --from-json。
        print(f"抓取失败：{str(exc).splitlines()[0]}")
        print("  products.json 是 JSON：请在浏览器打开它、另存为 .json，再用 --from-json 解析。")
        return 1
    except (OSError, json.JSONDecodeError) as exc:
        print(f"读取 products.json 失败：{exc}")
        return 1

    today = date.today()
    drafts, notes, counters = baby_catalog.scan_products(
        payload,
        existing_handles=existing_handles(),
        today=today,
        lookback_days=args.days,
        limit=args.limit,
    )

    print(f"来源：{source}")
    print(
        f"商品 {counters['total']} 条；已存在 {counters['known']}；"
        f"过旧 {counters['old']}；其他品牌 {counters['other_vendor']}；"
        f"无日期 {counters['no_date']}；缺标题/链接 {counters['no_title'] + counters['no_handle']}；"
        f"本轮草稿 {len(drafts)} 条。"
    )
    for note in notes:
        print(f"  ⚠ {note}")

    if not drafts:
        print("没有新的可起草商品。")
        return 0

    for draft in drafts:
        print(
            f"  · {draft['id']} | {draft['release_date']} | "
            f"{draft['price_range'] or '未标价'} | {draft['series']}"
        )
        print(f"      {draft['link']}")

    if not args.write:
        print("\n这是 dry-run：没有写草稿。确认后加 --write。")
        return 0

    written = write_drafts(drafts, overwrite=args.overwrite)
    print(f"\n写入 {written} 条草稿到 {drafts_dir().relative_to(ROOT)}")
    print("看草稿：python scripts/scan_baby.py --list")
    print("确认一条：python scripts/scan_baby.py --approve <id>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
