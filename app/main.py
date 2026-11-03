"""应用入口。

挂载顺序有讲究：``/api/*`` 路由必须先注册，最后才把 ``/`` 挂成静态目录——
静态挂载会吞掉所有未匹配路径，注册晚了 API 就全部 404。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__, db
from app.config import settings
from app.routers import auth as auth_routes
from app.routers import chat, data, feed, kb, profile, tools

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("lo-life")

STATIC_DIR = Path(__file__).parent / "static"

# 挂载静态目录前必须先存在，否则导入期就抛错（uvicorn 启动失败）
settings.ensure_dirs()


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db(settings.db_path)
    logger.info("数据目录：%s", settings.data_dir)
    logger.info("知识库索引：%s", settings.kb_index_dir)
    yield


class UTF8JSONResponse(JSONResponse):
    """显式声明 ``charset=utf-8``。

    JSON 按 RFC 8259 本来就是 UTF-8，浏览器也照这个处理；但**不带 charset 时**
    一些命令行工具（PowerShell 的 Invoke-WebRequest 默认按 ISO-8859-1）会把中文
    解成乱码，于是验证接口的人会以为服务坏了。上游 app.py 里踩过同一个坑，
    这里直接用同样的修法。
    """

    media_type = "application/json; charset=utf-8"


app = FastAPI(
    title="lo-life",
    version=__version__,
    description="Lolita 上新助手：推荐 / 关注 / 知识问答",
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    default_response_class=UTF8JSONResponse,
)

# 1) API 先注册
app.include_router(auth_routes.router)
app.include_router(chat.router)
app.include_router(profile.router)
app.include_router(kb.router)
app.include_router(tools.router)
app.include_router(feed.router)
app.include_router(data.router)

# 2) 静态资源最后挂。`/media` 在 `/` 之前，否则照片会被 index.html 吞掉。
#
# `/media` **不用 StaticFiles**：StaticFiles 在挂载那一刻就把目录固定下来了，
# 而 `data_dir` 是可以在运行期被换掉的（测试就是这么做的：每个用例一份独立库，
# 免得并行跑的两套测试互相删库）。固定目录的后果是"上传成功但访问 404"——
# 文件写进了新目录，StaticFiles 还在看旧目录。每次请求现算路径，就没有这个错位。
@app.get("/media/{name}", include_in_schema=False)
def media_file(name: str) -> FileResponse:
    # 只允许目录下的普通文件名：`..`、子目录、绝对路径一律拒绝
    safe = Path(name).name
    if not safe or safe != name or safe.startswith("."):
        raise HTTPException(status_code=404, detail="没有这个文件")
    target = settings.media_dir / safe
    if not target.is_file():
        raise HTTPException(status_code=404, detail="没有这个文件")
    return FileResponse(target)


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="app")


def main() -> None:
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.bind_host,
        port=settings.bind_port,
        reload=False,
    )


if __name__ == "__main__":
    main()
