"""媒体上传、健康检查、备份导出。

备份这件事在 M0 就必须做——本地优先方案里，衣柜和照片是**唯一不可再生**的东西，
丢一次就再也不会用了。所以导出不是"以后再说"的功能，是第一天就要能按下去的按钮。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
import threading
import zipfile
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from app import __version__, auth, bridge, db
from app.config import settings
from app.domain import images, newarrivals
from app.domain import shops as shop_table
from app.services import imagefetch
from app.services.imagefetch import FetchError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["data"])

MAX_MEDIA_BYTES = 10 * 1024 * 1024
ALLOWED_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


# --------------------------------------------------------------------- 健康


@router.get("/health")
def health() -> dict:
    kb = bridge.index_status(settings)
    entries, problems = newarrivals.load_newarrivals(settings.newarrivals_dir)
    media = list(settings.media_dir.glob("*")) if settings.media_dir.exists() else []
    return {
        "ok": True,
        "version": __version__,
        # 关注与偏好现在是**按用户**的，健康检查不带令牌，所以只报账号总数
        "users": db.count_users(settings.db_path),
        # 升级前遗留的"无归属店家"（旧 follows 表没有 user_id），等第一个注册的账号认领。
        # 暴露出来有两个用处：前端提示"登录后会接手"；冒烟脚本据此拒绝注册临时账号
        # ——否则临时账号会把这些店家认领走，再随注销一起删掉。
        "orphan_shops": db.count_orphan_shops(settings.db_path),
        "newarrivals": len(entries),
        "newarrival_problems": len(problems),
        "media": len([path for path in media if path.is_file()]),
        "llm_enabled": settings.llm_enabled,
        # 暴露 top_k：前端/冒烟脚本要能核对「返回条数不超过配置」这条不变量
        "top_k": settings.top_k,
        "kb": {"ready": kb["ready"], "chunks": kb["chunks"], "built_at": kb["built_at"]},
    }


# --------------------------------------------------------------------- 图片


@router.post("/media", status_code=201)
async def upload_media(file: UploadFile = File(...)) -> dict:
    """衣柜照片上传。

    两道校验刻意放在服务端而不只在前端：``accept="image/*"`` 只是一个提示，
    改个请求头就能传任意文件进来。本地服务也不该把这件事交给前端自觉。
    """
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(status_code=400, detail=f"不支持的图片格式：{suffix or '（无扩展名）'}")
    if not (file.content_type or "").startswith("image/"):
        raise HTTPException(status_code=400, detail=f"不是图片：{file.content_type}")

    settings.media_dir.mkdir(parents=True, exist_ok=True)
    target = settings.media_dir / f"{uuid4().hex}{suffix}"
    total = 0
    try:
        with target.open("wb") as handle:
            while chunk := await file.read(64 * 1024):
                total += len(chunk)
                if total > MAX_MEDIA_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"图片超过 {MAX_MEDIA_BYTES // 1024 // 1024} MB",
                    )
                handle.write(chunk)
    except HTTPException:
        target.unlink(missing_ok=True)
        raise
    except Exception as exc:  # noqa: BLE001
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"保存图片失败：{exc}") from exc

    return {"url": f"/media/{target.name}", "bytes": total}


# --------------------------------------------------------------------- 取图


class ImageFetchIn(BaseModel):
    """为一条上新取图。``page_url`` 留空就用 `content/shops/<brand>.json` 里的 `page_url`。"""

    id: str = Field(..., min_length=1, description="上新条目的 id")
    page_url: str = Field("", max_length=2000, description="商品详情页 / 店铺上新列表页")


_fetch_lock = threading.Lock()


@router.post("/images/fetch")
def fetch_image(payload: ImageFetchIn, request: Request) -> dict:
    """抓一次页面，把这一条上新的 `image`（和淘宝详情页 `link`）写进 JSON。

    ## 为什么允许在服务里联网，但仍然不是"实时抓取"

    这不是页面加载路径。它只在这个端点被**用户点一下**时执行一次，请求之间不共享、
    不预取、不定时跑。页面渲染仍然只读本地 JSON —— 所以某个站挂掉不会让推荐页变慢。

    ## 三个约束

    * **只在本机可用**：拒绝非回环地址。手机/局域网点这个按钮会 403——要取图就在
      跑服务的这台机器上点。理由很直接：这个端点会把任意 URL 抓回来落到磁盘上，
      不该是一个"局域网里谁都能调"的接口。
    * **同一时刻只跑一次**：并发点击会互相踩写回同一个 JSON，也更容易触发风控。
    * **不做重试**：失败就把原因（HTTP 403 / 登录墙 / 配不上）原样返回，让人换输入。
      自动重试会把握手变成撞门。
    """
    if not _is_loopback(request):
        raise HTTPException(
            status_code=403,
            detail="取图只允许在运行服务的本机上操作（非本机请求已拒绝）。",
        )

    entries, _ = newarrivals.load_newarrivals(settings.newarrivals_dir)
    entry = next((item for item in entries if item.id == payload.id), None)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"没有这条上新：{payload.id}")

    page_url = (payload.page_url or "").strip()
    shops, _ = shop_table.load_shops(settings.shops_dir)
    shop = shops.get(entry.brand)
    if not page_url and not (shop is not None and shop.page_url):
        raise HTTPException(
            status_code=400,
            detail=f"「{entry.brand}」还没有登记页面地址：请粘贴商品页地址，"
                   f"或在 content/shops/{entry.brand}.json 里填 page_url。",
        )

    if not _fetch_lock.acquire(blocking=False):
        raise HTTPException(status_code=429, detail="上一次取图还没结束，等它跑完再点。")
    try:
        resolution = imagefetch.fetch_for_entry(entry, page_url, shop=shop)
    except FetchError as exc:
        # 502 而不是 500：失败在"对方的站点"，不在这个服务
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        _fetch_lock.release()

    return {
        "ok": True,
        "id": entry.id,
        "image": resolution.image_url,
        "link": resolution.item_link,
        "page_url": page_url or (shop.page_url if shop else ""),
    }


def _is_loopback(request: Request) -> bool:
    host = (request.client.host if request.client else "") or ""
    return host in {"127.0.0.1", "::1", "localhost", "testclient"}


# --------------------------------------------------------------------- 备份


@router.get("/export")
def export_backup(_user: dict = Depends(auth.require_user)) -> FileResponse:
    """把「不可再生的东西」打成一个 zip：数据库 + 照片 + 知识库源文件。

    **必须登录。** 这个 zip 里是**整库**（含所有账号的密码哈希、身材数据）与全部照片，
    而 README 推荐用 Tailscale 让手机访问——没有这道门，同 tailnet 的任何设备都能
    直接下载全部数据。这是实例级备份，不是"当前账号的导出"。

    数据库用 ``sqlite3.Connection.backup()`` 复制，而不是直接拷 ``app.db``：
    库开着 WAL，直接拷单个文件可能拿到一个缺最新事务的半成品，
    而备份最可怕的情况就是"看起来成功了"。
    """
    settings.ensure_dirs()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    # 用「同目录下的临时文件」而不是临时子目录。两个 Windows 上的实测理由：
    #
    # 1. ``mkstemp`` 会把文件句柄一直开着；不显式 ``os.close`` 就接着 unlink，
    #    Windows 直接抛 WinError 32（"另一个程序正在使用此文件"），整个导出 500。
    #    Linux/macOS 上删一个打开着的文件是允许的，所以这个错只在 Windows 出现。
    # 2. ``mkdtemp`` 建的目录带 0o700 权限，在受限沙箱里连 os.listdir 都会
    #    WinError 5 —— 用 ``Path.mkdir()``（0o777）就没这个问题。
    #
    # 落 SQLite 快照时也不能直接拷 app.db：库开着 WAL，拷单个文件可能拿到缺最新
    # 事务的半成品，而备份最可怕的情况就是"看起来成功了"。
    zip_path = Path(_mkstemp_path(".export-", ".zip"))
    db_snapshot = Path(_mkstemp_path(".export-", ".db"))

    try:
        source = sqlite3.connect(settings.db_path)
        target = sqlite3.connect(db_snapshot)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(db_snapshot, "app.db")
            for folder, prefix in (
                (settings.media_dir, "media/"),
                # content/ 本身在 git 里，但备份要能**独立恢复**，所以一起打包
                (settings.kb_dir, "content/kb/"),
                (settings.size_dir, "content/size/"),
                (settings.care_dir, "content/care/"),
                (settings.newarrivals_dir, "content/newarrivals/"),
            ):
                if not folder.exists():
                    continue
                for path in folder.rglob("*"):
                    if path.is_file():
                        archive.write(path, prefix + path.relative_to(folder).as_posix())
            archive.writestr(
                "MANIFEST.json",
                json.dumps(
                    {
                        "app": "lo-life",
                        "version": __version__,
                        "exported_at": datetime.now().isoformat(timespec="seconds"),
                        "users": db.count_users(settings.db_path),
                        "newarrivals": len(newarrivals.load_newarrivals(settings.newarrivals_dir)[0]),
                        "kb": bridge.index_status(settings),
                        "restore": "把 app.db 与 media/ 放回 data/；content/ 放回项目根（也已经在 git 里）",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
    except Exception as exc:  # noqa: BLE001
        zip_path.unlink(missing_ok=True)
        logger.exception("导出失败")
        raise HTTPException(status_code=500, detail=f"导出失败：{exc}") from exc
    finally:
        db_snapshot.unlink(missing_ok=True)

    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=f"lo-life-backup-{stamp}.zip",
        # 响应发完之后再删：发之前删掉，客户端拿到的是空文件
        background=BackgroundTask(zip_path.unlink, missing_ok=True),
    )


def _mkstemp_path(prefix: str, suffix: str) -> Path:
    """在数据目录里创建一个临时文件名，并**立刻关闭**句柄（见上面的理由 1）。"""
    handle_fd, name = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=str(settings.data_dir))
    os.close(handle_fd)
    return Path(name)
