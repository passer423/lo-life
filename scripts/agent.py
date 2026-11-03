"""在命令行里跑一次助手，看它走了几步、调了什么、为什么这么答。

为什么要有命令行入口：接前端之前，agent 的行为必须先在终端里看清楚。
多步调用的坏味道（空结果硬重试、绕开工具直接作答）在网页上很难观察，
在终端里一眼就能看到每次工具调用和结果。

用法：
    python scripts/agent.py "700 以内的 OP，哥特风，我穿得上吗？"
    python scripts/agent.py "带亮片和烫金的裙子怎么洗" --trace
    python scripts/agent.py --last-runs 5
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.agent import loop, trace  # noqa: E402
from app.agent.planner import DeepSeekPlanner, PlannerUnavailable  # noqa: E402
from app.agent.tools import Context  # noqa: E402
from app.config import settings  # noqa: E402


def _print_steps(result: dict) -> None:
    print("—" * 60)
    for step in result["steps"]:
        kind = step.get("kind")
        if kind == "tool":
            mark = "空" if step.get("empty") else f"{step.get('evidence')} 条"
            print(
                f"  第 {step['step']} 步  {step.get('tool')}"
                f"  args={json.dumps(step.get('args'), ensure_ascii=False)}"
                f"  → {mark}／{step.get('ms')}ms"
            )
            if step.get("summary"):
                print(f"          {step['summary']}")
            if step.get("error"):
                print(f"          错误：{step['error']}")
        else:
            print(f"  第 {step.get('step')} 步  [{kind}] {step.get('detail', '')}")
    print("—" * 60)
    print(f"  收手原因：{result['stopped_reason'] or '模型自己给完答案'}")
    usage = result.get("usage") or {}
    writing = usage.get("writer") or {}
    # 决策与写作是两次独立调用，只报一个数会让人低估成本
    calls = int(usage.get("calls", 0)) + int(writing.get("calls", 0))
    tokens_in = int(usage.get("prompt_tokens", 0)) + int(writing.get("prompt_tokens", 0))
    tokens_out = int(usage.get("completion_tokens", 0)) + int(writing.get("completion_tokens", 0))
    print(
        f"  模型调用 {calls} 次（决策 {usage.get('calls', 0)} + 写作 {writing.get('calls', 0)}），"
        f"tokens {tokens_in}+{tokens_out}，耗时 {result['duration_ms']}ms"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="跑一次助手")
    parser.add_argument("question", nargs="*", help="要问的问题")
    parser.add_argument("--steps", type=int, default=4, help="最多几步（越大越贵）")
    parser.add_argument("--trace", action="store_true", help="打印每一步")
    parser.add_argument("--stream", action="store_true", help="流式打印答案（模拟前端的体验）")
    parser.add_argument("--last-runs", type=int, default=0, help="只看最近几次轨迹")
    args = parser.parse_args(argv)

    if args.last_runs:
        records = trace.tail(args.last_runs, settings)
        if not records:
            print(f"还没有轨迹（{trace.trace_path(settings)}）")
            return 0
        for record in records:
            tools_used = [step.get("tool") for step in record.get("steps", []) if step.get("kind") == "tool"]
            print(
                f"{record.get('ts')}  {record.get('question')}\n"
                f"    工具={tools_used}  停={record.get('stopped_reason')}  "
                f"拒答={record.get('refused')}  {record.get('duration_ms')}ms"
            )
        return 0

    question = " ".join(args.question).strip()
    if not question:
        parser.error("要给一个问题")

    try:
        planner = DeepSeekPlanner(settings)
        from app.agent.writer import DeepSeekWriter

        writer = DeepSeekWriter(settings)
    except PlannerUnavailable as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2

    if args.stream:
        result = None
        for event in loop.stream(
            question, ctx=Context(settings=settings), planner=planner, writer=writer,
            max_steps=max(1, min(args.steps, loop.MAX_STEPS)),
        ):
            if event["type"] == "delta":
                print(event["text"], end="", flush=True)
            elif event["type"] == "step" and event.get("kind") == "tool":
                print(f"\n· {event['tool']}（{'空' if event.get('empty') else str(event.get('evidence')) + ' 条'}）")
            elif event["type"] == "replace":
                print(f"\n[已替换：{event['reason']}]", flush=True)
            elif event["type"] == "final":
                result = event
        print()
        if result and args.trace:
            _print_steps(result)
        return 0

    result = loop.run(
        question,
        ctx=Context(settings=settings),
        planner=planner,
        writer=writer,
        max_steps=max(1, min(args.steps, loop.MAX_STEPS)),
    )

    if args.trace:
        _print_steps(result)

    print()
    print(result["answer"])
    if result["evidence"]:
        print()
        print("证据：")
        for item in result["evidence"]:
            print(f"  [{item['n']}] {item['title']}｜{item['ref']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
