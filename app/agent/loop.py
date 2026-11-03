"""多步循环、护栏，以及**把过程也流出来**。

## 结构

``explore()`` 是一个生成器：每调一次工具就产出一个 ``step`` 事件，最后产出一个
``explored`` 事件（携带证据与收手原因）。``run()`` 和 ``stream()`` 都消费它——
这样流式与非流式**共用同一份循环逻辑**，不会出现"网页上和命令行里行为不一样"。

## 护栏为什么写在代码里，而不是提示词里

提示词是"建议"，模型可以不听；而下面这几件事一旦失控，代价是**错误答案**或**烧钱**：

| 护栏 | 失控后果 |
|---|---|
| 步数硬上限 | 死循环，一个请求打十几次模型 |
| 连续空结果即停 | 反复改写查询"骗"出结果 → 假阳性（上游花整篇 README 修的就是这个） |
| 没证据不许作答 | 模型凭记忆回答"Baby 最近出了三款" |
| 引用越界即降级 | 编造来源，用户点开是空的 |

所以每条都是 ``if ... : break``，都有测试盯着。
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Iterator

from app.agent import trace
from app.agent.planner import PlannerOutputError, build_system_prompt
from app.agent.tools import TOOLS, Context, ToolError
from app.agent.writer import sanitize_history

logger = logging.getLogger(__name__)

MAX_STEPS = 6              # 一次 run 最多几步
MAX_EMPTY = 2              # 连续几次空结果就收手
MAX_INVALID = 2            # 连续几次非法动作就收手
OBSERVATION_LIMIT = 900    # 单条观察最多多少字进上下文

_REFUSAL_MARKERS = ("没有找到", "没找到", "没有相关", "资料中没有", "暂无", "没有匹配", "没有这方面")


@dataclass(slots=True)
class Exploration:
    """一次探索的最终状态。"""

    evidence: list[dict] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    stopped_reason: str = ""
    answer: str = ""           # planner 自己写的答案（脚本化 planner 走这条路）
    usage: dict = field(default_factory=dict)
    duration_ms: int = 0

    def numbered(self) -> list[dict]:
        return [{"n": offset + 1, **item} for offset, item in enumerate(self.evidence)]


def _forced_answer(evidence: list[dict], *, reason: str) -> str:
    if not evidence:
        return (
            "没有检索到能回答这个问题的内容。我不猜——可以换个问法，"
            "或者先把相关的上新/知识库条目补进来。"
        )
    header = {
        "citation_out_of_range": "（模型给的引用编号对不上，下面直接列原文，不做归纳）",
        "no_citation": "（模型的回答没有标注来源，下面直接列原文）",
    }.get(reason, "（已到本轮探索上限，下面是查到的事实）")
    lines = [header, ""]
    for index, item in enumerate(evidence, start=1):
        lines.append(f"[{index}] {item.get('title', '')}：{item.get('text', '')}")
    return "\n".join(lines)


def citation_problems(answer: str, evidence_count: int) -> list[str]:
    """回答里的 ``[n]`` 必须落在 1..证据条数 内；有证据却不标引用也算问题。

    "有证据却没标引用"只在回答**不像拒答**时才判为问题——模型说"没找到"时
    本来就不该有引用，硬要求会把它本来正确的拒答判成错误。
    """
    problems: list[str] = []
    tokens = re.findall(r"\[(\d+)\]", answer or "")
    for token in tokens:
        if not 1 <= int(token) <= evidence_count:
            problems.append(f"引用 [{token}] 越界（只有 {evidence_count} 条证据）")
    if evidence_count and not tokens and not any(mark in (answer or "") for mark in _REFUSAL_MARKERS):
        problems.append("有证据但回答没有标注来源")
    return problems


def explore(
    question: str,
    *,
    ctx: Context,
    planner,
    max_steps: int = MAX_STEPS,
    history: list[dict] | None = None,
) -> Iterator[dict]:
    """产出 ``{"type":"step",...}`` 若干次，最后产出一次 ``{"type":"explored",...}``。"""
    question = (question or "").strip()
    if not question:
        raise ValueError("问题不能为空")

    state = Exploration(usage=getattr(planner, "usage", {}) or {})
    empty_streak = 0
    invalid_streak = 0
    started = time.perf_counter()

    messages: list[dict] = [{"role": "system", "content": build_system_prompt(ctx.body)}]
    messages.extend(sanitize_history(history))
    messages.append({"role": "user", "content": question})

    for index in range(1, max_steps + 1):
        try:
            action = planner.decide(messages)
        except PlannerOutputError as exc:
            invalid_streak += 1
            step = {"step": index, "kind": "invalid", "detail": str(exc)[:160]}
            state.steps.append(step)
            yield {"type": "step", **step}
            messages.append({"role": "user", "content": f"观察：输出不合法（{exc}）。请只输出规定格式的 JSON。"})
            if invalid_streak >= MAX_INVALID:
                state.stopped_reason = "invalid_output"
                break
            continue

        if action["action"] == "final":
            if not state.evidence:
                # 一条证据都没有就想作答 —— 这正是幻觉的入口，直接拦掉
                step = {"step": index, "kind": "final_without_evidence"}
                state.steps.append(step)
                yield {"type": "step", **step}
                state.stopped_reason = "final_without_evidence"
                break
            state.answer = action.get("answer", "")
            step = {"step": index, "kind": "final"}
            state.steps.append(step)
            yield {"type": "step", **step}
            break

        name = action["tool"]
        tool = TOOLS.get(name)
        if tool is None:
            invalid_streak += 1
            step = {"step": index, "kind": "unknown_tool", "tool": name}
            state.steps.append(step)
            yield {"type": "step", **step}
            messages.append({"role": "assistant", "content": json.dumps(action, ensure_ascii=False)})
            messages.append({"role": "user", "content": "观察：没有这个工具。可用工具：" + "、".join(TOOLS)})
            if invalid_streak >= MAX_INVALID:
                state.stopped_reason = "unknown_tool"
                break
            continue

        invalid_streak = 0
        began = time.perf_counter()
        try:
            result = tool.run(action.get("args") or {}, ctx)
            error = ""
        except ToolError as exc:
            # 参数错误回给模型让它自己改，而不是让它看到 500
            result = {"ok": False, "empty": True, "summary": f"参数或调用有问题：{exc}", "evidence": []}
            error = str(exc)
        elapsed_ms = int((time.perf_counter() - began) * 1000)

        fresh = result.get("evidence") or []
        base = len(state.evidence)
        state.evidence.extend(fresh)
        numbered = [{"n": base + offset + 1, "title": item.get("title", "")} for offset, item in enumerate(fresh)]
        is_empty = bool(result.get("empty")) or not fresh

        step = {
            "step": index,
            "kind": "tool",
            "tool": name,
            "args": action.get("args") or {},
            "ok": not error,
            "error": error[:160],
            "empty": is_empty,
            "evidence": len(fresh),
            "ms": elapsed_ms,
            "summary": str(result.get("summary", ""))[:160],
        }
        state.steps.append(step)
        yield {"type": "step", **step}

        observation = {
            "summary": str(result.get("summary", ""))[:OBSERVATION_LIMIT],
            "empty": is_empty,
            "证据": numbered,
        }
        messages.append({"role": "assistant", "content": json.dumps(action, ensure_ascii=False)})
        messages.append({"role": "user", "content": "观察：" + json.dumps(observation, ensure_ascii=False)})

        empty_streak = empty_streak + 1 if is_empty else 0
        if empty_streak >= MAX_EMPTY:
            state.stopped_reason = "empty_results"
            break
    else:
        state.stopped_reason = "max_steps"

    state.usage = getattr(planner, "usage", {}) or {}
    state.duration_ms = int((time.perf_counter() - started) * 1000)
    yield {"type": "explored", "state": state}


def _finish(
    question: str,
    state: Exploration,
    *,
    ctx: Context,
    writer=None,
    history: list[dict] | None = None,
) -> dict:
    """把探索结果收尾成一个完整响应（含引用校验与轨迹）。"""
    answer = state.answer
    if not answer:
        if state.evidence and writer is not None:
            try:
                answer = writer.write(question, state.numbered(), history)
            except Exception as exc:  # noqa: BLE001 - 写作失败不该让整轮白跑
                logger.warning("写作失败，退回原文清单：%s", exc)
                answer = _forced_answer(state.evidence, reason="writer_failed")
        else:
            answer = _forced_answer(state.evidence, reason=state.stopped_reason or "max_steps")

    problems = citation_problems(answer, len(state.evidence))
    if problems:
        reason = "citation_out_of_range" if any("越界" in item for item in problems) else "no_citation"
        answer = _forced_answer(state.evidence, reason=reason)
        state.stopped_reason = ";".join(filter(None, [state.stopped_reason, reason]))

    usage = dict(state.usage)
    if writer is not None and getattr(writer, "usage", None):
        usage["writer"] = writer.usage

    payload = {
        "question": question,
        "answer": answer,
        "refused": not state.evidence,
        "evidence": state.numbered(),
        "steps": state.steps,
        "stopped_reason": state.stopped_reason,
        "usage": usage,
        "duration_ms": state.duration_ms,
        "logged_in": ctx.user is not None,
    }
    trace.append(
        {
            "question": question,
            "answer": answer[:400],
            "refused": payload["refused"],
            "stopped_reason": state.stopped_reason,
            "steps": state.steps,
            "evidence": len(state.evidence),
            "usage": usage,
            "duration_ms": state.duration_ms,
            "logged_in": payload["logged_in"],
            "history_turns": len(history or []),
        },
        ctx.settings,
    )
    return payload


def run(
    question: str,
    *,
    ctx: Context,
    planner,
    max_steps: int = MAX_STEPS,
    history: list[dict] | None = None,
    writer=None,
) -> dict:
    """非流式：跑完一次给完整结果。命令行与普通接口用它。"""
    state: Exploration | None = None
    for event in explore(question, ctx=ctx, planner=planner, max_steps=max_steps, history=history):
        if event["type"] == "explored":
            state = event["state"]
    assert state is not None
    return _finish(question.strip(), state, ctx=ctx, writer=writer, history=history)


def stream(
    question: str,
    *,
    ctx: Context,
    planner,
    writer=None,
    max_steps: int = MAX_STEPS,
    history: list[dict] | None = None,
) -> Iterator[dict]:
    """流式：先吐 step 事件，再吐答案增量，最后吐 final。

    事件类型：``step`` / ``delta`` / ``replace`` / ``final`` / ``error``。
    答案增量**边收边发**，不在服务端攒完再切——那不是流式。
    """
    state: Exploration | None = None
    try:
        for event in explore(question, ctx=ctx, planner=planner, max_steps=max_steps, history=history):
            if event["type"] == "explored":
                state = event["state"]
            else:
                yield event
    except ValueError as exc:
        yield {"type": "error", "detail": str(exc)}
        return

    assert state is not None
    question = question.strip()

    if state.answer:
        # 脚本化 planner（测试）直接给了答案
        yield {"type": "delta", "text": state.answer}
        text = state.answer
    elif state.evidence and writer is not None:
        chunks: list[str] = []
        try:
            for delta in writer.stream(question, state.numbered(), history):
                chunks.append(delta)
                yield {"type": "delta", "text": delta}
        except Exception as exc:  # noqa: BLE001
            logger.warning("流式写作失败：%s", exc)
            fallback = _forced_answer(state.evidence, reason="writer_failed")
            yield {"type": "replace", "text": fallback, "reason": "writer_failed"}
            text = fallback
        else:
            text = "".join(chunks)
    else:
        text = _forced_answer(state.evidence, reason=state.stopped_reason or "max_steps")
        yield {"type": "delta", "text": text}

    problems = citation_problems(text, len(state.evidence))
    if problems:
        reason = "citation_out_of_range" if any("越界" in item for item in problems) else "no_citation"
        fixed = _forced_answer(state.evidence, reason=reason)
        yield {"type": "replace", "text": fixed, "reason": reason, "problems": problems}
        text = fixed
        state.stopped_reason = ";".join(filter(None, [state.stopped_reason, reason]))

    usage = dict(state.usage)
    if writer is not None and getattr(writer, "usage", None):
        usage["writer"] = writer.usage

    payload = {
        "question": question,
        "answer": text,
        "refused": not state.evidence,
        "evidence": state.numbered(),
        "steps": state.steps,
        "stopped_reason": state.stopped_reason,
        "usage": usage,
        "duration_ms": state.duration_ms,
        "logged_in": ctx.user is not None,
    }
    trace.append(
        {
            "question": question,
            "answer": text[:400],
            "refused": payload["refused"],
            "stopped_reason": state.stopped_reason,
            "steps": state.steps,
            "evidence": len(state.evidence),
            "usage": usage,
            "duration_ms": state.duration_ms,
            "logged_in": payload["logged_in"],
            "streamed": True,
            "history_turns": len(history or []),
        },
        ctx.settings,
    )
    yield {"type": "final", **payload}
