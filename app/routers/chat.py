"""助手（agent）接口：一次性结果 + SSE 流式。

P1 只有**只读**工具。写操作（录上新、改偏好、重建索引）必须走"预览 + 人工确认"，
不是这一版的范围——写工具的破坏力不可逆，不该为了功能看起来多而提前放进来。

## 为什么流式用 POST + 手写 SSE，而不是 EventSource

``EventSource`` 只能发 GET，问题只能塞进 query string（会进浏览器历史和访问日志），
而且要带 ``Authorization`` 头也没法加。所以用 ``fetch`` 读 ``ReadableStream``，
自己按 ``data:`` 分帧——见 ``static/chat.js``。
"""

from __future__ import annotations

import json
import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app import auth
from app.agent import loop, trace
from app.agent.planner import DeepSeekPlanner, PlannerUnavailable
from app.agent.tools import TOOLS, Context
from app.agent.writer import DeepSeekWriter, sanitize_history
from app.config import settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/chat", tags=["chat"])


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1, max_length=500)


class ChatIn(BaseModel):
    message: str = Field(..., min_length=1, max_length=300)
    # 步数上限由调用方收窄：一次问答的模型调用次数直接等于成本
    max_steps: int = Field(4, ge=1, le=loop.MAX_STEPS)
    # 多轮：客户端带上最近几轮。服务端不存会话——**编号只在单轮内有效**，
    # 所以历史里只留文字，不再引用上一轮的 [n]。
    history: list[ChatTurn] = Field(default_factory=list, max_length=16)


def _make_planner(app=settings):
    """留成函数是为了测试能注入脚本化 planner——不必联网、不必有 key。"""
    return DeepSeekPlanner(app)


def _make_writer(app=settings):
    if not app.llm_enabled:
        raise PlannerUnavailable("写作需要模型：请先配置 LLM_API_KEY")
    return DeepSeekWriter(app)


def _history_of(payload: ChatIn) -> list[dict]:
    return sanitize_history([turn.model_dump() for turn in payload.history])


@router.get("/status")
def chat_status() -> dict:
    """前端靠它决定"能不能聊"：没有模型时应明确禁用，而不是给一个只会说不知道的框。"""
    return {
        "enabled": settings.llm_enabled,
        "model": settings.llm_model if settings.llm_enabled else "",
        "hint": "" if settings.llm_enabled else (
            "需要在 lo-life/.env 里配置 LLM_API_KEY"
            "（DeepSeek：LLM_BASE_URL=https://api.deepseek.com/v1、LLM_MODEL=deepseek-chat）"
        ),
        "tools": [{"name": tool.name, "description": tool.description} for tool in TOOLS.values()],
        "limits": {
            "max_steps": loop.MAX_STEPS,
            "max_empty": loop.MAX_EMPTY,
            "max_invalid": loop.MAX_INVALID,
            "history_turns": 8,
        },
        "stream": True,
    }


@router.post("")
def chat(payload: ChatIn, user: dict | None = Depends(auth.optional_user)) -> dict:
    try:
        planner = _make_planner(settings)
    except PlannerUnavailable as exc:
        # 503 而不是 500：这是"还没配好"，不是"服务坏了"
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    # 写作器拿不到不算致命：planner 自己给了答案就用它的，没给就退回原文清单
    try:
        writer = _make_writer(settings)
    except PlannerUnavailable:
        writer = None

    try:
        result = loop.run(
            payload.message,
            ctx=Context(user=user, settings=settings),
            planner=planner,
            writer=writer,
            max_steps=payload.max_steps,
            history=_history_of(payload),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 模型压根调不通时，别把"强制收手"的兜底文案当成回答给用户
    if result["stopped_reason"] == "invalid_output" and not result["evidence"]:
        detail = next(
            (step.get("detail", "") for step in result["steps"] if step.get("kind") == "invalid"),
            "模型输出不合法",
        )
        raise HTTPException(status_code=502, detail=f"模型调用失败：{detail}")
    return result


@router.post("/stream")
def chat_stream(payload: ChatIn, user: dict | None = Depends(auth.optional_user)):
    """SSE：先推进度（调了哪个工具、几条结果），再推答案增量，最后推 final。"""
    try:
        planner = _make_planner(settings)
    except PlannerUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    try:
        writer = _make_writer(settings)
    except PlannerUnavailable:
        writer = None

    history = _history_of(payload)

    def generate():
        try:
            for event in loop.stream(
                payload.message,
                ctx=Context(user=user, settings=settings),
                planner=planner,
                writer=writer,
                max_steps=payload.max_steps,
                history=history,
            ):
                yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
        except Exception as exc:  # noqa: BLE001 - 流已经开始，只能以事件形式报错
            logger.exception("流式回答失败")
            yield "data: " + json.dumps(
                {"type": "error", "detail": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False
            ) + "\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # 反向代理（Tailscale / nginx）下不加这句会被缓冲成"最后一次性到达"
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/runs")
def chat_runs(limit: int = Query(10, ge=1, le=100)) -> dict:
    """最近几次 run 的轨迹。排查"它为什么没找到"靠这个，而不是靠猜。"""
    records = trace.tail(limit, settings)
    return {"count": len(records), "runs": records}
