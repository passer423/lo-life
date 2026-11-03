"""把模型的输出翻译成"动作"。

## 为什么用 JSON-in-text 而不是原生 tool calling

DeepSeek 与其它 OpenAI 兼容端点对 function calling 的支持程度不一致（有的没有、
有的字段有差异）。JSON 协议在任何端点上都能跑，而且**测试里可以直接注入一个
脚本化 planner**，不必联网、不必有 key——循环和护栏因此可以逐条钉死。

## 提示词里那几条硬规则是怎么来的

都是前几轮踩出来的：

* **空结果是有效结果** —— agent 最本能的错误是"换个说法再查一次，直到搜出东西"，
  这正好复现了上游花整篇 README 修掉的假阳性。
* **两次空必须收手** —— 只靠提示词不够，``loop.py`` 里用计数器强制执行。
* **必须调过工具才能回答** —— 否则模型会拿训练记忆冒充"我们有的款式"。
* **每个事实标证据编号** —— 价格、尺码、发售日期这类信息必须可回溯到工具结果。
"""

from __future__ import annotations

import json
import logging

from app.agent.tools import catalog
from app.config import AppSettings

logger = logging.getLogger(__name__)

PLANNER_MAX_TOKENS = 800
PLANNER_TIMEOUT = 60.0


class PlannerUnavailable(RuntimeError):
    """没有可用模型（未配置 key）。路由层翻译成 503。"""


class PlannerOutputError(RuntimeError):
    """模型返回的东西不是可用的动作。"""


def _profile_block(body: dict | None) -> str:
    """只告诉模型"有没有"，**不告诉具体数值**。

    早先是把「身高 160cm、胸围 86cm…」直接写进提示词的。那等于每一轮问答都把这些
    数字发给模型服务商——而模型根本不需要它们：合身判断在服务端由尺码表算，
    模型只需要知道"能穿 L 码"。数字留在这台机器上，结论才离开。
    """
    if not body:
        return (
            "## 用户身材\n"
            "用户还没填身材参数。要判断合身就用 check_fit，它会说明缺哪几项——"
            "**不要自己编三围，也不要连续追问**。\n"
        )
    return (
        "## 用户身材\n"
        "用户**已经保存过**身材参数（身高/体重/围度，存在本机，不在本对话里）。\n"
        "判断合身请直接调用 check_fit，它会用那些数字对照尺码表。\n"
        "**不要询问、也不要复述这些数字**——你不需要知道它们具体是多少。\n"
    )


def build_system_prompt(body: dict | None = None) -> str:
    tools = json.dumps(catalog(), ensure_ascii=False, indent=1)
    profile = _profile_block(body)
    return f"""你是「lo 上新」的助手。你只能通过下面的工具了解事实，**不能凭记忆回答**。

## 可用工具（含参数的 JSON Schema）
{tools}

{profile}
## 输出协议
只输出一个 JSON 对象，不要任何其它文字。

调用工具：
{{"action":"tool","tool":"<工具名>","args":{{...}}}}

调查结束（**不要在这里写答案**）：
{{"action":"final"}}

## 硬规则
1. **空结果是有效结果**：它意味着我们的内容里没有。不要为了拿到结果而反复改写查询词。
2. 连续两次工具返回空之后，**必须**给 final，并明确说没有找到；但如果空的是商品查询，
   而问题里还有洗护/术语意图，先切到 care_advice 或 search_kb，不要继续改写商品名。
3. 至少调用一次工具之后才能给 final。不许猜，不许用通用常识补。
4. **final 里不要自己写答案**：系统会依据你查到的证据单独写成回答（那样才能流式输出）。
   你的职责是"查清楚 + 判断够了没有"，不是措辞。
5. 如果证据明显答不了这个问题，仍然给 final —— 系统会依据证据的实际情况说明没有。
6. **判断合不合身只能用 check_fit**。不许自己拿尺码表心算，也不许在回答里写
   "你大概穿 M"——推荐码必须来自工具结果。
7. **洗护问题不要被商品查询带偏**：问「怎么洗/能不能机洗/会不会掉色」时，即使提到
   具体商品名，也要从问题里提取面料、工艺、颜色（「棉版/棉布」→ 棉，「红色/酒红」→
   红色/酒红易掉色）并调用 care_advice。商品没收录不等于洗护问题无解；item_profile
   或 search_feed 空了，也要继续用 care_advice 或 search_kb，不能直接拒答。
8. 用户问得口语、含糊（比如"有啥新裙子""这条我能穿吗"）时，按最可能的意图直接查，
   别把问题原样丢回去；只有工具确实缺必需参数（比如没给系列名）时才换工具或收手。
"""


class DeepSeekPlanner:
    """走 OpenAI 兼容协议（默认 DeepSeek）。"""

    def __init__(self, app: AppSettings):
        if not app.llm_enabled:
            raise PlannerUnavailable(
                "助手需要配置模型：在 lo-life/.env 里填 LLM_API_KEY"
                "（DeepSeek 用 https://api.deepseek.com/v1 + deepseek-chat）"
            )
        from openai import OpenAI

        self._client = OpenAI(
            api_key=app.llm_api_key.strip(),
            base_url=app.llm_base_url.strip(),
            timeout=PLANNER_TIMEOUT,
        )
        self._model = app.llm_model.strip()
        self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}

    @property
    def model(self) -> str:
        return self._model

    def decide(self, messages: list[dict]) -> dict:
        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                temperature=0,
                max_tokens=PLANNER_MAX_TOKENS,
                response_format={"type": "json_object"},
            )
        except Exception as exc:  # noqa: BLE001 - 网络/鉴权失败要变成可读信息
            raise PlannerOutputError(f"模型调用失败：{type(exc).__name__}: {exc}") from exc

        self.usage["calls"] += 1
        usage = getattr(response, "usage", None)
        if usage is not None:
            self.usage["prompt_tokens"] += int(getattr(usage, "prompt_tokens", 0) or 0)
            self.usage["completion_tokens"] += int(getattr(usage, "completion_tokens", 0) or 0)

        text = (response.choices[0].message.content or "").strip()
        return parse_action(text)


def parse_action(text: str) -> dict:
    """解析模型输出。单独成函数是为了能单测——不用发请求就能验协议。"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PlannerOutputError(f"模型没有返回合法 JSON：{text[:80]}") from exc
    if not isinstance(data, dict):
        raise PlannerOutputError("模型返回的不是 JSON 对象")

    action = data.get("action")
    if action == "tool":
        name = data.get("tool")
        if not isinstance(name, str) or not name:
            raise PlannerOutputError("action=tool 但缺少 tool 名字")
        args = data.get("args")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise PlannerOutputError("args 必须是对象")
        return {"action": "tool", "tool": name, "args": args}
    if action == "final":
        answer = data.get("answer")
        if answer is None:
            answer = ""
        if not isinstance(answer, str):
            raise PlannerOutputError("final 的 answer 必须是字符串")
        # 允许不带答案：真实模型被要求只输出 {"action":"final"}，
        # 答案交给 writer 单独流式写；带了答案也接受（脚本化 planner 走这条）。
        return {"action": "final", "answer": answer.strip()}
    raise PlannerOutputError(f"未知的 action：{action!r}（只能是 tool 或 final）")
