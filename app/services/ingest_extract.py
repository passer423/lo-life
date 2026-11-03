"""可选的模型抽取器：只从页面文本里抽字段，并给出原文引文。

## 为什么单独成模块

它必须联网，所以不能放进 `app/domain/`（那里全是纯函数）。离线管线的其余部分
（归一化、校验、状态、审批）都在 `domain/ingest.py`，不联网也能单测。

## 边界

* **只抽不猜。** 页面没写的字段返回 `null`；日期只写"10 月下旬"也不补成 1 号；
* **每个字段带 `quotes`。** 引文必须逐字出现在正文里，校验层会逐条比对；
* **失败就是失败。** 网络错误、非 JSON、结构不对都抛 `ExtractFailed`，不尝试
  "解析半个 JSON"——半份结果比明确失败更难查；
* **没有 key 是可预期状态。** 抛 `ExtractUnavailable`，调用方降级为结构化字段 +
  人工补录，不把整条管线打崩。
"""

from __future__ import annotations

import json

from app.config import AppSettings
from app.domain.newarrivals import STYLE_OPTIONS

EXTRACT_MAX_TOKENS = 700
EXTRACT_TIMEOUT = 60.0
MAX_INPUT_CHARS = 6000

SYSTEM_PROMPT = """你是上新录入的抽取器。只从页面文本里抽取字段，输出一个 JSON 对象。

字段：
- series：系列名，原文写法
- release_date：YYYY-MM-DD；页面没有明确的年月日就填 null
- price_range：原文里的价格或区间，保持原样（如 "63800" / "295 - 880"）
- currency：ISO 三字母币种（JPY / CNY …）；看不出来填 null
- items：款式数组（JSK / OP / SK / KC …）
- styles：风格数组，只能从下面这份词表里选：{styles}
- note：预订、发货、限定等补充
- aliases：同一系列的别名（原文或标题里出现的写法）
- quotes：每个非空字段对应的原文片段，必须逐字出现在正文里

规则：
1. 只抽页面里明确写出的内容；找不到就 null，**不要推测**。
2. 日期只有"10月下旬"这类没有具体日的，release_date 填 null，不要把日子补成 1 号。
3. quotes 必须逐字摘录，不要改写、不要翻译；没有引文的字段不要填。
4. 只输出 JSON，不要任何其它文字。
"""


class ExtractUnavailable(RuntimeError):
    """没有可用模型（未配置 key）。调用方应降级为人工补录。"""


class ExtractFailed(RuntimeError):
    """模型调用或输出解析失败。调用方把草稿标记为 extract_failed。"""


def extract(
    text: str,
    *,
    app: AppSettings,
    brand: str = "",
    brand_name: str = "",
) -> dict:
    """从纯文本里抽取字段。返回模型给的原始 dict（未经校验）。"""
    if not app.llm_enabled:
        raise ExtractUnavailable(
            "没有配置 LLM_API_KEY，跳过模型抽取；可人工补全草稿后再审批。"
        )

    from openai import OpenAI

    client = OpenAI(
        api_key=app.llm_api_key.strip(),
        base_url=app.llm_base_url.strip(),
        timeout=EXTRACT_TIMEOUT,
    )

    clipped = (text or "").strip()[:MAX_INPUT_CHARS]
    user_parts = [f"品牌：{brand_name or brand or '未知'}", "页面正文：", clipped]
    if len(text or "") > MAX_INPUT_CHARS:
        user_parts.append("\n（正文过长，已截断）")

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT.format(styles=" / ".join(STYLE_OPTIONS))},
        {"role": "user", "content": "\n".join(user_parts)},
    ]

    try:
        response = client.chat.completions.create(
            model=app.llm_model.strip(),
            messages=messages,
            temperature=0,
            max_tokens=EXTRACT_MAX_TOKENS,
            response_format={"type": "json_object"},
        )
    except Exception as exc:  # noqa: BLE001 - 网络/鉴权失败要变成可读结果
        raise ExtractFailed(f"模型调用失败：{type(exc).__name__}: {exc}") from exc

    content = (response.choices[0].message.content or "").strip()
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ExtractFailed(f"模型没有返回合法 JSON：{content[:120]}") from exc
    if not isinstance(data, dict):
        raise ExtractFailed("模型返回的不是 JSON 对象")
    return data
