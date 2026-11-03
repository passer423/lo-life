"""离线入库 CLI：页面 → 草稿 → 人工审批 → `content/newarrivals/<id>.json`。

## 三条路径

```bash
# 1) 浏览器另存为 HTML（零请求，推荐）
python scripts/ingest_newarrivals.py --from-html 页面.html --brand ap --page-url 原始地址

# 2) 现抓一个官方页（域名策略、登录墙检测由 imagefetch 负责）
python scripts/ingest_newarrivals.py --from-page https://… --brand ap

# 3) 重跑抽取 / 审批草稿
python scripts/ingest_newarrivals.py --retry data/ingest/drafts/xxx.json
python scripts/ingest_newarrivals.py --approve data/ingest/drafts/xxx.json
```

## 默认不碰 content/

前两条只写 `data/ingest/`：原文快照、草稿、rejected 记录。只有 `--approve` 会写
`content/newarrivals/`，而且会再校验一次必填字段、日期、词表和重名。

## 退出码

| 码 | 含义 |
|---|---|
| 0 | `ready`（或审批成功） |
| 1 | 参数/网络/文件错误，或审批被拒 |
| 2 | `partial`：缺必填字段或引文校验丢字段，需人工补 |
| 3 | `extract_failed`：模型这一路失败，可 `--retry` |
| 4 | `rejected`：重复 id / 非法 id / 来源问题，不能审批 |
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.domain import ingest as ingest_domain  # noqa: E402
from app.services import ingest_pipeline  # noqa: E402
from app.services.imagefetch import FetchError  # noqa: E402

EXIT_BY_STATUS = {
    ingest_domain.READY: 0,
    ingest_domain.PARTIAL: 2,
    ingest_domain.EXTRACT_FAILED: 3,
    ingest_domain.REJECTED: 4,
}


def read_page(path: Path) -> str:
    """浏览器另存的日文页面可能是 Shift_JIS；按顺序尝试，不静默乱码。"""
    for encoding in ("utf-8", "shift_jis", "cp932"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def show_ingest(result: dict) -> None:
    print(f"[{result['status']}] {result.get('id') or '(未生成 id)'}")
    if result.get("reasons"):
        print("  拒绝原因：" + "；".join(result["reasons"]))
    if result.get("errors"):
        print("  抽取错误：" + "；".join(result["errors"]))
    if result.get("missing"):
        print("  缺失字段：" + "、".join(result["missing"]))
    if result.get("warnings"):
        for warning in result["warnings"]:
            print("  提醒：" + warning)
    if result.get("draft_path"):
        print("  草稿：" + result["draft_path"])
        print("  下一步：补全缺失字段后 --approve；或 --retry 重跑抽取")
    if result.get("rejected_path"):
        print("  记录：" + result["rejected_path"])


def show_approve(result: dict) -> None:
    if result.get("ok"):
        print("[approved] " + result["entry_path"])
        for line in result.get("diff") or []:
            print(line)
        return
    print("[refused] 审批未通过")
    for problem in result.get("problems") or []:
        print("  - " + problem)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(
        description="离线入库：页面 → 草稿 → 人工审批",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--from-html", help="解析浏览器另存的页面（零请求）")
    mode.add_argument("--from-page", help="现抓一个页面（走域名策略与登录墙检测）")
    mode.add_argument("--retry", help="从草稿记录的 raw 快照重跑抽取")
    mode.add_argument("--approve", help="审批草稿并写入 content/newarrivals/")

    parser.add_argument("--brand", help="品牌 id，如 ap / baby（from-html / from-page 必填）")
    parser.add_argument("--id", help="指定条目 id；不填则按发售月 + 品牌 + 系列名生成")
    parser.add_argument("--page-url", default="", help="from-html 时原始页面地址，写进 source")
    parser.add_argument("--no-llm", action="store_true", help="跳过模型抽取，只保留结构化字段")
    parser.add_argument("--overwrite", action="store_true", help="审批时允许覆盖已有条目")
    args = parser.parse_args(argv)

    try:
        if args.from_html:
            if not args.brand:
                parser.error("--from-html 需要 --brand")
            html = read_page(Path(args.from_html))
            result = ingest_pipeline.ingest_from_html(
                html,
                page_url=args.page_url,
                brand=args.brand,
                app=settings,
                proposed_id=args.id or "",
                allow_model=not args.no_llm,
            )
            show_ingest(result)
            return EXIT_BY_STATUS.get(result["status"], 1)

        if args.from_page:
            if not args.brand:
                parser.error("--from-page 需要 --brand")
            result = ingest_pipeline.ingest_from_url(
                args.from_page,
                brand=args.brand,
                app=settings,
                proposed_id=args.id or "",
                allow_model=not args.no_llm,
            )
            show_ingest(result)
            return EXIT_BY_STATUS.get(result["status"], 1)

        if args.retry:
            result = ingest_pipeline.retry_draft(
                args.retry,
                app=settings,
                allow_model=not args.no_llm,
            )
            show_ingest(result)
            return EXIT_BY_STATUS.get(result["status"], 1)

        if args.approve:
            result = ingest_pipeline.approve_draft(
                args.approve,
                app=settings,
                overwrite=args.overwrite,
            )
            show_approve(result)
            return 0 if result.get("ok") else 1

    except (FetchError, ValueError, OSError) as exc:
        print(f"[失败] {exc}", file=sys.stderr)
        return 1

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
