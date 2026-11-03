"""知识问答路由。

薄到只剩参数校验与异常翻译——真正的检索/生成/校验全在 app/bridge.py 里，
上游怎么标定的就怎么跑，这里不插入任何"顺手优化"。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query

from app import bridge
from app.config import settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/kb", tags=["kb"])


@router.get("/status")
def kb_status() -> dict:
    """索引状态。索引没建时返回 ready=false + 一句可执行的话，不让前端猜。"""
    status = bridge.index_status(settings)
    if not status["ready"] and not status["error"]:
        status["error"] = "知识库索引还不存在。先运行：python scripts/build_kb_index.py"
    return status


@router.post("/reload")
def kb_reload() -> dict:
    """重建索引之后立刻换上新索引，不必重启服务。

    与上游 ``/api/reload`` 同样的理由：检索器是启动时装载一次的，
    不主动切换就会一直用旧索引，而症状是「明明建了索引却搜不到新条目」。
    """
    try:
        bundle = bridge.reload_bundle(settings)
    except bridge.UpstreamUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"status": "ok", "chunks": len(bundle.chunks)}


@router.get("/ask")
def kb_ask(
    q: str = Query(..., min_length=1, max_length=300, description="问题"),
    top_k: int | None = Query(None, ge=1, le=20),
) -> dict:
    question = q.strip()
    if not question:
        raise HTTPException(status_code=400, detail="问题不能为空")
    try:
        return bridge.query_kb(question, settings, top_k=top_k)
    except bridge.IndexNotReady as exc:
        # 503 而不是 500：这是"还没准备好"，不是"服务坏了"
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except bridge.UpstreamUnavailable as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - 带上类型，便于现场排查
        logger.exception("知识问答失败")
        raise HTTPException(
            status_code=500, detail=f"知识问答失败：{type(exc).__name__}: {exc}"
        ) from exc
