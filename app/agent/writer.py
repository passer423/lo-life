"""把"决策"和"写作"拆开，顺便让答案可以真流式。

## 为什么要拆

原来 planner 在 ``{"action":"final","answer":"..."}`` 里把答案一起写在 JSON 里。
这有两个问题：

1. **没法流式**——用户要看到的是答案一个个字出现，而不是一个 JSON 字符串。
2. **贵**——planner 的 system prompt 里塞着四个工具的完整 JSON Schema（约 3.4k tokens），
   让它写答案等于每次都替这些 schema 付一次钱。

拆开之后：planner 只说"我调查完了"，写作单独一次调用，只带证据（约 0.6k tokens）。
写作的提示词也能写得更死——没有工具可用，它只能照着证据写。

## 兼容

planner 的 final 动作如果**自己带了答案**，就直接用（测试里的脚本化 planner 走这条路，
不必联网）。真实模型被要求不带答案，于是走流式写作。
"""

from __future__ import annotations

import logging
from typing import Iterator

from app.config import AppSettings

logger = logging.getLogger(__name__)

WRITER_MAX_TOKENS = 600
WRITER_TIMEOUT = 60.0

# 多轮：最多带几轮历史、每条多长。上下文是有限资源，历史无限增长就是给自己制造噪声。
MAX_HISTORY_TURNS = 8
MAX_HISTORY_CHARS = 500

SYNTH_SYSTEM = """你是「lo 上新」的助手，负责把调查结果写成给用户看的回答。

硬规则：
1. 只使用【证据】里的内容。不许补充常识、不许推测、不许"顺便提醒"。
2. 每个事实后面标证据编号，如 [1][2]；编号对应【证据】里的序号。
3. 证据里没有的信息就直说没有，不要绕圈子。
4. 价格、尺码、日期必须与证据原文**完全一致**，不要换算、不要合并不同币种。
5. 简体中文，先给结论再给细节；不要复述问题、不要开场白、不要超过 6 句。
"""


def build_synth_messages(
    question: str,
    evidence: list[dict],
    history: list[dict] | None = None,
) -> list[dict]:
    blocks = [
        f"[{item.get('n')}] {item.get('title', '')}：{item.get('text', '')}" for item in evidence
    ]
    evidence_text = "\n".join(blocks) if blocks else "（没有任何证据）"
    messages: list[dict] = [{"role": "system", "content": SYNTH_SYSTEM}]
    for turn in (history or [])[-MAX_HISTORY_TURNS:]:
        messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append(
        {
            "role": "user",
            "content": f"【证据】\n{evidence_text}\n\n【问题】\n{question}",
        }
    )
    return messages


def sanitize_history(raw) -> list[dict]:
    """只留最近若干轮，截断过长内容，丢掉角色不对的项。

    客户端传什么都可能——历史是**不可信输入**，而且直接进上下文（成本与安全都在这里）。
    """
    if not raw:
        return []
    cleaned: list[dict] = []
    for turn in raw:
        if not isinstance(turn, dict):
            continue
        role = turn.get("role")
        content = str(turn.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        cleaned.append({"role": role, "content": content[:MAX_HISTORY_CHARS]})
    return cleaned[-MAX_HISTORY_TURNS:]


class DeepSeekWriter:
    """答案写作。``write`` 一次性返回，``stream`` 逐段产出。"""

    def __init__(self, app: AppSettings):
        from openai import OpenAI

        self._client = OpenAI(
            api_key=app.llm_api_key.strip(),
            base_url=app.llm_base_url.strip(),
            timeout=WRITER_TIMEOUT,
        )
        self._model = app.llm_model.strip()
        self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}

    def _record(self, usage) -> None:
        self.usage["calls"] += 1
        if usage is not None:
            self.usage["prompt_tokens"] += int(getattr(usage, "prompt_tokens", 0) or 0)
            self.usage["completion_tokens"] += int(getattr(usage, "completion_tokens", 0) or 0)

    def write(self, question: str, evidence: list[dict], history: list[dict] | None = None) -> str:
        response = self._client.chat.completions.create(
            model=self._model,
            messages=build_synth_messages(question, evidence, history),
            temperature=0.2,
            max_tokens=WRITER_MAX_TOKENS,
        )
        self._record(getattr(response, "usage", None))
        return (response.choices[0].message.content or "").strip()

    def stream(
        self, question: str, evidence: list[dict], history: list[dict] | None = None
    ) -> Iterator[str]:
        """逐段产出。**调用方负责拼接**——这里不缓存整段，否则就不是流式了。"""
        stream = self._client.chat.completions.create(
            model=self._model,
            messages=build_synth_messages(question, evidence, history),
            temperature=0.2,
            max_tokens=WRITER_MAX_TOKENS,
            stream=True,
            stream_options={"include_usage": True},
        )
        for chunk in stream:
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                self._record(usage)
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            delta = getattr(choices[0].delta, "content", None)
            if delta:
                yield delta
