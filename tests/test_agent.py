"""agent 的工具与循环测试。

这一层出错的代价是"给出错误答案"或"烧钱"，所以每条护栏单独钉死：

* 工具只读、参数校验可读、异常不漏到 HTTP 层；
* 循环：步数上限、连续空结果即停、没证据不许给答案、引用越界降级；
* 轨迹写盘。

**不联网、不需要 key**：planner 是脚本化的，模型行为由用例决定。

**feed 工具用自带内容测**，不读 `content/newarrivals` —— 那是会持续变化的真实数据
（已经被并行工作从占位换成官网实拍）。尺码/洗护的严格逻辑分别由 `test_size.py`、
`test_care.py` 覆盖，这里只验"工具能把它包出来"。
"""

from __future__ import annotations

import json

import pytest

from app.agent import loop, trace
from app.agent import tools as agent_tools
from app.agent.planner import PlannerOutputError, parse_action
from app.agent.tools import TOOLS, Context, ToolError
from app.config import AppSettings, settings

FAKE_ENTRIES = [
    {"id": "cheap", "brand": "baby", "brand_name": "Baby", "series": "测试A",
     "release_date": "2026-10-29", "price_range": "100", "items": ["JSK"], "styles": ["甜系"]},
    {"id": "mid", "brand": "ap", "brand_name": "AP", "series": "测试B",
     "release_date": "2026-10-20", "price_range": "500 - 800", "items": ["OP"], "styles": ["古典"]},
    {"id": "pricey", "brand": "ap", "brand_name": "AP", "series": "测试C",
     "release_date": "2026-10-10", "price_range": "2000", "items": ["OP"], "styles": ["蒸汽朋克"]},
]

# 品牌级表的测试夹具。真实 content/size 里的 AP/Baby 是 item_level（官方不给 S/M/L），
# 所以工具测试不能依赖它们，必须自带一份真正的品牌表。
FIT_CHART = {
    "brand": "t",
    "display_name": "T",
    "chart_version": "test",
    "source": "测试官方尺码表",
    "fit": "正码",
    "categories": {
        "JSK": {
            "key": ["bust", "waist"],
            "ease": {"bust": 4, "waist": 2},
            "sizes": {
                "S": {"bust": 80, "waist": 64, "length": 88},
                "M": {"bust": 84, "waist": 68, "length": 92},
                "L": {"bust": 90, "waist": 74, "length": 96},
            },
        }
    },
    "verified": True,
}

FIT_ENTRY = {
    "id": "e1", "brand": "t", "brand_name": "T", "series": "测试区间裙",
    "release_date": "2026-10-20", "items": ["JSK"],
    "size": {
        "kind": "free", "label": "F",
        "bust": [91, 101], "waist": [74, 86], "length": [89, 89],
        "source": "T 官方商品页",
    },
}


class ScriptedPlanner:
    """按脚本返回动作；脚本用完就给 final，避免测试挂死。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}

    def decide(self, messages):
        self.calls += 1
        self.usage["calls"] = self.calls
        if not self.script:
            return {"action": "final", "answer": "（脚本已用完）"}
        return self.script.pop(0)


def tool_action(name, **args):
    return {"action": "tool", "tool": name, "args": args}


def final_action(answer):
    return {"action": "final", "answer": answer}


def ctx() -> Context:
    """真实内容的上下文（尺码表/洗护规则是稳定的）。"""
    return Context(user=None, settings=settings)


@pytest.fixture()
def feed_ctx(workdir) -> Context:
    """自带三条上新内容的上下文，价格/风格都已知。"""
    content = workdir / "content"
    folder = content / "newarrivals"
    folder.mkdir(parents=True, exist_ok=True)
    for entry in FAKE_ENTRIES:
        (folder / f"{entry['id']}.json").write_text(
            json.dumps(entry, ensure_ascii=False), encoding="utf-8"
        )
    app = AppSettings(data_dir=workdir / "data", content_dir=content)
    return Context(user=None, settings=app)


# --------------------------------------------------------------------- 工具表


def test_catalog_is_read_only():
    """P1 不放任何写工具：写操作的破坏力不可逆，必须走"预览 + 确认"。"""
    names = set(TOOLS)
    assert names == {
        "search_feed", "search_kb", "care_advice", "size_recommend", "check_fit", "item_profile",
    }
    for name in names:
        assert not any(word in name for word in ("write", "delete", "create", "update", "set"))


def test_catalog_exposes_json_schema():
    for item in agent_tools.catalog():
        assert item["name"] and item["description"]
        assert item["parameters"]["type"] == "object"
        assert "properties" in item["parameters"]


def test_bad_args_are_readable():
    with pytest.raises(ToolError) as exc:
        TOOLS["search_feed"].run({"price_min": -5}, ctx())
    message = str(exc.value)
    assert "price_min" in message and "不合法" in message


def test_tool_exception_is_wrapped_not_leaked():
    """工具的意外异常必须变成可读失败结果，不能把整个 run 打崩。"""

    def boom(*_args, **_kwargs):
        raise RuntimeError("磁盘炸了")

    tool = agent_tools.Tool(
        name="care_advice", description="x", args_model=agent_tools.CareArgs, handler=boom,
    )
    with pytest.raises(ToolError) as exc:
        tool.run({"materials": ["蕾丝"]}, ctx())
    assert "执行失败" in str(exc.value) and "磁盘炸了" in str(exc.value)


# --------------------------------------------------------------------- 上新工具（自带内容）


def test_search_feed_finds_entry(feed_ctx):
    result = TOOLS["search_feed"].run({"q": "测试B"}, feed_ctx)
    assert result["ok"] is True and result["empty"] is False
    assert [item["ref"] for item in result["evidence"]] == ["mid"]


def test_search_feed_no_match_is_empty_not_error(feed_ctx):
    result = TOOLS["search_feed"].run({"q": "量子计算"}, feed_ctx)
    assert result["ok"] is True
    assert result["empty"] is True
    assert result["evidence"] == []
    # 商品没查到时不能把洗护问题一起判死，观察里要提示切到 care_advice
    assert "care_advice" in result["summary"]


def test_search_feed_price_filter(feed_ctx):
    result = TOOLS["search_feed"].run({"price_min": 600}, feed_ctx)
    refs = set(item["ref"] for item in result["evidence"])
    assert refs == {"mid", "pricey"}        # 100 的被排除


def test_search_feed_style_filter(feed_ctx):
    result = TOOLS["search_feed"].run({"styles": ["蒸汽朋克"]}, feed_ctx)
    assert [item["ref"] for item in result["evidence"]] == ["pricey"]


def test_search_feed_category_filter(feed_ctx):
    result = TOOLS["search_feed"].run({"categories": ["JSK"]}, feed_ctx)
    assert [item["ref"] for item in result["evidence"]] == ["cheap"]


def test_search_feed_on_real_content_does_not_crash():
    """真实内容会变，所以只验"不炸、结构对"。"""
    result = TOOLS["search_feed"].run({"limit": 3}, ctx())
    assert result["ok"] is True
    assert isinstance(result["evidence"], list)
    assert len(result["evidence"]) <= 3


# --------------------------------------------------------------------- 其它工具


def test_search_kb_maps_hits_to_evidence(monkeypatch):
    fake = [{"chunk_id": "kb#1", "score": 0.91, "author": "知识库", "created_at": "",
             "text": "烫金和烫钻最怕高温、搓揉和机洗的离心力。"}]
    monkeypatch.setattr(agent_tools.bridge, "search_kb", lambda *_a, **_k: fake)
    result = TOOLS["search_kb"].run({"question": "烫金能机洗吗"}, ctx())
    assert result["empty"] is False
    assert result["evidence"][0]["ref"] == "kb#1"
    assert "0.910" in result["summary"]


def test_search_kb_empty_says_it_is_a_valid_conclusion(monkeypatch):
    monkeypatch.setattr(agent_tools.bridge, "search_kb", lambda *_a, **_k: [])
    result = TOOLS["search_kb"].run({"question": "量子计算"}, ctx())
    assert result["empty"] is True
    assert "有效结论" in result["summary"]


def test_care_tool_returns_structured_verdict():
    result = TOOLS["care_advice"].run({"materials": ["蕾丝"], "crafts": ["烫金"]}, ctx())
    assert result["ok"] is True and result["empty"] is False
    assert result["summary"]
    assert result["evidence"][0]["text"]


def test_care_tool_requires_selection():
    with pytest.raises(ToolError):
        TOOLS["care_advice"].run({}, ctx())


def test_size_tool_returns_structured_result(fit_ctx):
    result = TOOLS["size_recommend"].run(
        {"brand": "t", "category": "JSK", "bust": 86, "waist": 68}, fit_ctx
    )
    assert result["ok"] is True and result["empty"] is False
    assert result["recommended"] == "L"             # 具体码由 test_size.py 严格覆盖
    assert "T" in result["summary"]
    assert result["verified"] is True
    assert "测试官方尺码表" in result["evidence"][0]["text"]


def test_size_tool_missing_chart_is_a_conclusive_answer():
    result = TOOLS["size_recommend"].run({"brand": "nope", "category": "JSK", "bust": 86}, ctx())
    assert result["ok"] is True
    assert result["empty"] is False                 # 没有表是结论，不是"没查到"
    assert result["status"] == "no_data"
    assert "没有" in result["summary"]
    assert result["evidence"][0]["title"] == "没有品牌级尺码表"


def test_size_tool_missing_measurements_asks_for_body(fit_ctx):
    result = TOOLS["size_recommend"].run({"brand": "t", "category": "JSK"}, fit_ctx)
    assert result["status"] == "need_measurements"
    assert result["empty"] is False
    assert "身材" in result["summary"]


# --------------------------------------------------------------------- 协议解析


@pytest.mark.parametrize("text, expected", [
    ('{"action":"tool","tool":"search_feed","args":{"q":"Baby"}}', "tool"),
    ('{"action":"final","answer":"有两条"}', "final"),
    ('{"action":"tool","tool":"care_advice"}', "tool"),
    # 真实模型的 final 不带答案（答案交给 writer 流式写）
    ('{"action":"final"}', "final"),
    ('{"action":"final","answer":"   "}', "final"),
])
def test_parse_action_accepts_valid(text, expected):
    assert parse_action(text)["action"] == expected


@pytest.mark.parametrize("text, fragment", [
    ("不是 JSON", "合法 JSON"),
    ('{"action":"shrug"}', "未知的 action"),
    ('{"action":"tool"}', "tool 名字"),
    ('{"action":"final","answer":123}', "必须是字符串"),
    ('["数组"]', "JSON 对象"),
])
def test_parse_action_rejects_invalid(text, fragment):
    with pytest.raises(PlannerOutputError) as exc:
        parse_action(text)
    assert fragment in str(exc.value)


# --------------------------------------------------------------------- 循环护栏


def test_happy_path_uses_tool_then_answers(monkeypatch):
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)
    planner = ScriptedPlanner([
        tool_action("care_advice", materials=["蕾丝"], crafts=["烫金"]),
        final_action("[1] 这类装饰不要机洗。"),
    ])
    result = loop.run("烫金能机洗吗？", ctx=ctx(), planner=planner)

    assert result["refused"] is False
    assert result["answer"].startswith("[1]")
    assert [step["kind"] for step in result["steps"]] == ["tool", "final"]
    assert result["evidence"][0]["n"] == 1
    assert planner.calls == 2


def test_two_empty_results_stop_the_loop(monkeypatch):
    """核心护栏：连续两次空就收手，绝不允许"改写查询直到搜出东西"。"""
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)
    planner = ScriptedPlanner([
        tool_action("search_feed", q="量子计算"),
        tool_action("search_feed", q="量子"),
        final_action("我应该没机会被执行"),
    ])
    result = loop.run("有没有量子计算主题的裙子？", ctx=ctx(), planner=planner)

    assert result["stopped_reason"] == "empty_results"
    assert planner.calls == 2          # 第三次根本没请求模型
    assert result["refused"] is True
    assert "不猜" in result["answer"]


def test_final_without_evidence_is_blocked(monkeypatch):
    """一条证据都没有就给答案 —— 这正是幻觉的入口。"""
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)
    planner = ScriptedPlanner([final_action("Baby 最近出了三款新的。")])
    result = loop.run("Baby 出了什么新的？", ctx=ctx(), planner=planner)

    assert result["stopped_reason"] == "final_without_evidence"
    assert result["refused"] is True
    assert "Baby 最近出了三款" not in result["answer"]


def test_unknown_tool_twice_stops(monkeypatch):
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)
    planner = ScriptedPlanner([tool_action("rm_rf"), tool_action("rm_rf"), final_action("x")])
    result = loop.run("删库", ctx=ctx(), planner=planner)

    assert result["stopped_reason"] == "unknown_tool"
    assert planner.calls == 2


def test_max_steps_is_enforced(monkeypatch):
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)
    planner = ScriptedPlanner([tool_action("care_advice", materials=["蕾丝"])] * 5)
    result = loop.run("一直查", ctx=ctx(), planner=planner, max_steps=2)

    assert result["stopped_reason"] == "max_steps"
    assert len([step for step in result["steps"] if step["kind"] == "tool"]) == 2
    assert "探索上限" in result["answer"]      # 有证据就列证据


def test_invalid_output_twice_stops(monkeypatch):
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)

    class Broken:
        usage = {}

        def __init__(self):
            self.calls = 0

        def decide(self, messages):
            self.calls += 1
            raise PlannerOutputError("模型没有返回合法 JSON")

    planner = Broken()
    result = loop.run("q", ctx=ctx(), planner=planner)
    assert result["stopped_reason"] == "invalid_output"
    assert planner.calls == 2


def test_out_of_range_citation_is_downgraded(monkeypatch):
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)
    planner = ScriptedPlanner([
        tool_action("care_advice", materials=["蕾丝"]),
        final_action("有 3 条 [9]，价格 999。"),
    ])
    result = loop.run("蕾丝怎么洗", ctx=ctx(), planner=planner)

    assert "引用编号对不上" in result["answer"]
    assert "citation_out_of_range" in result["stopped_reason"]


def test_missing_citation_is_downgraded(monkeypatch):
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)
    planner = ScriptedPlanner([
        tool_action("care_advice", materials=["蕾丝"]),
        final_action("蕾丝要装洗衣袋。"),
    ])
    result = loop.run("蕾丝怎么洗", ctx=ctx(), planner=planner)

    assert "没有标注来源" in result["answer"]


def test_refusal_with_evidence_is_kept(monkeypatch):
    """模型说"没找到"时本来就没有引用，不该被硬判成错误。"""
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)
    planner = ScriptedPlanner([
        tool_action("care_advice", materials=["蕾丝"]),
        final_action("没有找到关于真丝的规定。"),
    ])
    result = loop.run("真丝怎么洗", ctx=ctx(), planner=planner)

    assert result["answer"] == "没有找到关于真丝的规定。"


# --------------------------------------------------------------------- 轨迹


def test_trace_is_written(workdir, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", workdir)
    planner = ScriptedPlanner([
        tool_action("care_advice", materials=["蕾丝"]),
        final_action("[1] 装洗衣袋。"),
    ])
    loop.run("蕾丝怎么洗", ctx=Context(settings=settings), planner=planner)

    path = trace.trace_path(settings)
    assert path.exists()
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert record["question"] == "蕾丝怎么洗"
    assert record["refused"] is False
    assert [step["tool"] for step in record["steps"] if step["kind"] == "tool"] == ["care_advice"]


def test_trace_failure_does_not_break_the_run(workdir, monkeypatch):
    """轨迹是观测手段，不该成为故障源：写不进去就吞掉，run 照常返回。"""
    blocker = workdir / "blocker"
    blocker.write_text("x", encoding="utf-8")          # 用普通文件挡住目录创建
    monkeypatch.setattr(trace, "trace_path", lambda app=None: blocker / "sub" / "chat_runs.jsonl")
    assert trace.append({"question": "q"}, settings) is None


# --------------------------------------------------------------------- 接口


def test_status_is_disabled_without_key(client):
    payload = client.get("/api/chat/status").json()
    assert payload["enabled"] is False          # 测试环境没有 key
    assert len(payload["tools"]) == 6
    assert "LLM_API_KEY" in payload["hint"]


def test_chat_without_key_is_503(client):
    response = client.post("/api/chat", json={"message": "有 Baby 的新款吗"})
    assert response.status_code == 503
    assert "LLM_API_KEY" in response.json()["detail"]


def test_chat_rejects_empty_message(client):
    assert client.post("/api/chat", json={"message": ""}).status_code == 422


def test_chat_runs_with_injected_planner(client, monkeypatch):
    from app.routers import chat as chat_router

    planner = ScriptedPlanner([
        tool_action("care_advice", materials=["蕾丝"], crafts=["烫金"]),
        final_action("[1] 这类装饰不要机洗。"),
    ])
    monkeypatch.setattr(chat_router, "_make_planner", lambda app=None: planner)

    response = client.post("/api/chat", json={"message": "烫金能机洗吗？", "max_steps": 3})
    assert response.status_code == 200
    body = response.json()
    assert body["refused"] is False
    assert body["answer"].startswith("[1]")
    assert body["evidence"]
    assert body["steps"][0]["tool"] == "care_advice"


def test_chat_reports_model_failure_as_502(client, monkeypatch):
    from app.routers import chat as chat_router

    class Dead:
        usage = {}

        def decide(self, messages):
            raise PlannerOutputError("模型调用失败：APIConnectionError: 连不上")

    monkeypatch.setattr(chat_router, "_make_planner", lambda app=None: Dead())
    response = client.post("/api/chat", json={"message": "有 Baby 的新款吗"})
    assert response.status_code == 502
    assert "模型调用失败" in response.json()["detail"]


def test_runs_requires_login(client):
    """轨迹里是完整的原始问题与回答，匿名不能读。"""
    assert client.get("/api/chat/runs").status_code == 401


def test_runs_endpoint(client):
    token = client.post(
        "/api/auth/register", json={"username": "runs-tester", "password": "password123"}
    ).json()["token"]
    payload = client.get(
        "/api/chat/runs", params={"limit": 5}, headers={"Authorization": "Bearer " + token}
    ).json()
    assert "count" in payload and "runs" in payload


# --------------------------------------------------------------------- 合身（用存下来的身材）


@pytest.fixture()
def user_ctx(data_dir):
    """建一个真账号 + 初始化库：check_fit 要从库里读身材。"""
    from app import auth as auth_mod
    from app import db as db_mod

    db_mod.init_db(settings.db_path)
    user, _token, _claimed = auth_mod.register("agent-fit", "password123")
    return user


@pytest.fixture()
def fit_ctx(user_ctx, workdir):
    """带一份品牌级表和一条商品级尺寸条目的上下文。"""
    content = workdir / "content"
    size_dir = content / "size"
    entries_dir = content / "newarrivals"
    size_dir.mkdir(parents=True, exist_ok=True)
    entries_dir.mkdir(parents=True, exist_ok=True)
    (size_dir / "t.json").write_text(json.dumps(FIT_CHART, ensure_ascii=False), encoding="utf-8")
    (entries_dir / "e1.json").write_text(json.dumps(FIT_ENTRY, ensure_ascii=False), encoding="utf-8")
    app = AppSettings(data_dir=settings.data_dir, content_dir=content)
    return Context(user=user_ctx, settings=app)


def test_check_fit_uses_saved_body(fit_ctx):
    from app import db as db_mod

    db_mod.save_body(settings.db_path, fit_ctx.user["id"], {"bust": 86.0, "waist": 68.0, "height": 160.0})
    result = TOOLS["check_fit"].run({"brand": "t", "category": "JSK"}, fit_ctx)
    assert result["empty"] is False
    assert result["recommended"] == "L"
    assert "L" in result["verdict"]
    assert result["evidence"][0]["text"]
    assert result["verified"] is True


def test_check_fit_prefers_item_level_size(fit_ctx):
    """有条目级实测尺寸时，必须用它，而不是品牌表。"""
    result = TOOLS["check_fit"].run({"entry": "e1", "bust": 84, "waist": 66}, fit_ctx)
    assert result["status"] == "ok"
    assert result["recommended"] == "F"
    assert result["verified"] is True
    assert "官方商品页" in result["evidence"][0]["text"]


def test_check_fit_without_body_asks_to_fill(fit_ctx):
    """没填身材时必须说"还不知道"，不能凭品牌印象给个码。"""
    result = TOOLS["check_fit"].run({"brand": "t", "category": "JSK"}, fit_ctx)
    assert result["status"] == "no_profile"
    assert "身材" in result["summary"]


def test_size_tool_falls_back_to_saved_body(fit_ctx):
    """用户不必每次在对话里报三围。"""
    from app import db as db_mod

    db_mod.save_body(settings.db_path, fit_ctx.user["id"], {"bust": 86.0, "waist": 68.0})
    result = TOOLS["size_recommend"].run({"brand": "t", "category": "JSK"}, fit_ctx)
    assert result["recommended"] == "L"


# --------------------------------------------------------------------- 商品档案（并行取证）


def test_item_profile_merges_parallel_sources(fit_ctx, monkeypatch):
    """一次调用拿到条目 + 合身 + 术语；各路的 ref 不能互相覆盖。"""
    from app import db as db_mod

    db_mod.save_body(settings.db_path, fit_ctx.user["id"], {"bust": 84.0, "waist": 66.0})
    monkeypatch.setattr(
        agent_tools.bridge,
        "search_kb",
        lambda *_a, **_k: [{"chunk_id": "kb#1", "score": 0.8, "text": "JSK 和 OP 的区别。"}],
    )
    result = TOOLS["item_profile"].run({"entry": "e1"}, fit_ctx)

    assert result["status"] == "ok"
    refs = [item["ref"] for item in result["evidence"]]
    assert refs == ["e1", "fit:e1", "kb#1"]
    assert "能穿" in result["summary"]
    assert "相关知识" in result["summary"]


def test_item_profile_without_body_skips_fit(fit_ctx, monkeypatch):
    """没存身材就不做合身判断——档案里不该出现"请先填三围"。"""
    monkeypatch.setattr(agent_tools.bridge, "search_kb", lambda *_a, **_k: [])
    result = TOOLS["item_profile"].run({"entry": "e1"}, fit_ctx)

    assert result["status"] == "ok"
    assert [item["ref"] for item in result["evidence"]] == ["e1"]
    assert "未判断合身" in result["summary"]


def test_item_profile_kb_failure_is_local(fit_ctx, monkeypatch):
    """知识库没建/检索失败只影响这一路，条目本身照样返回。"""

    def boom(*_args, **_kwargs):
        raise RuntimeError("索引没建")

    monkeypatch.setattr(agent_tools.bridge, "search_kb", boom)
    result = TOOLS["item_profile"].run({"entry": "e1"}, fit_ctx)

    assert result["status"] == "ok"
    assert result["evidence"][0]["ref"] == "e1"


def test_item_profile_ambiguous_entry(feed_ctx):
    result = TOOLS["item_profile"].run({"entry": "测试"}, feed_ctx)
    assert result["status"] == "ambiguous"
    assert len(result["evidence"]) == 3
    assert "更具体的系列名" in result["summary"]


def test_item_profile_unknown_entry_is_a_conclusion(feed_ctx):
    result = TOOLS["item_profile"].run({"entry": "量子计算"}, feed_ctx)
    assert result["status"] == "no_data"
    assert result["empty"] is False        # 没有这条是结论，不是"没查到"
    assert "没有匹配的上新条目" in result["evidence"][0]["title"]
    # 商品没收录不等于洗护问题无解：观察里要提示继续查洗护
    assert "care_advice" in result["summary"]


def test_system_prompt_mentions_profile_without_leaking_numbers():
    """只告诉模型"有"，**不给数值**：那些数字不该每轮都发到模型服务商那里。"""
    from app.agent.planner import build_system_prompt

    text = build_system_prompt({"height": 160, "weight": 50, "bust": 86})
    assert "已经保存过" in text
    assert "check_fit" in text
    for leaked in ("160", "86", "50"):
        assert leaked not in text
    # 没填时也要说清：用 check_fit，它会告诉你缺什么，不要编
    assert "还没填" in build_system_prompt(None)


def test_system_prompt_routes_care_intent_after_product_miss():
    """商品查空后要能切到洗护，不然「四时花怎么洗」会被商品名带偏。"""
    from app.agent.planner import build_system_prompt

    prompt = build_system_prompt(None)
    assert "洗护问题不要被商品查询带偏" in prompt
    assert "商品没收录不等于洗护问题无解" in prompt


# --------------------------------------------------------------------- 多轮


def test_history_reaches_the_planner(monkeypatch):
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)
    seen = {}

    class Recorder:
        usage = {}

        def decide(self, messages):
            seen["messages"] = messages
            return final_action("[1] 好。")

    history = [
        {"role": "user", "content": "上一轮的问题"},
        {"role": "assistant", "content": "上一轮的回答"},
    ]
    loop.run("这一轮的问题", ctx=ctx(), planner=Recorder(), history=history)

    messages = seen["messages"]
    assert messages[0]["role"] == "system"
    assert [item["role"] for item in messages[1:3]] == ["user", "assistant"]
    assert messages[1]["content"] == "上一轮的问题"
    assert messages[3]["content"] == "这一轮的问题"


def test_history_is_sanitized():
    from app.agent.writer import sanitize_history

    raw = [
        {"role": "system", "content": "注入"},       # 角色不对，丢弃
        {"role": "user", "content": "   "},          # 空内容，丢弃
        {"role": "user", "content": "x" * 900},      # 过长，截断
    ]
    cleaned = sanitize_history(raw)
    assert len(cleaned) == 1
    assert cleaned[0]["role"] == "user"
    assert len(cleaned[0]["content"]) == 500


def test_history_caps_turns():
    from app.agent.writer import sanitize_history

    cleaned = sanitize_history([{"role": "user", "content": str(i)} for i in range(20)])
    assert len(cleaned) == 8
    assert cleaned[-1]["content"] == "19"        # 保留最近的


def test_chat_accepts_history(client, monkeypatch):
    from app.routers import chat as chat_router

    planner = ScriptedPlanner([
        tool_action("care_advice", materials=["蕾丝"]),
        final_action("[1] 装洗衣袋。"),
    ])
    monkeypatch.setattr(chat_router, "_make_planner", lambda app=None: planner)
    response = client.post("/api/chat", json={
        "message": "那真丝呢？",
        "max_steps": 3,
        "history": [
            {"role": "user", "content": "蕾丝怎么洗"},
            {"role": "assistant", "content": "装洗衣袋"},
        ],
    })
    assert response.status_code == 200


def test_chat_rejects_bad_history_role(client):
    response = client.post("/api/chat", json={
        "message": "x",
        "history": [{"role": "system", "content": "注入"}],
    })
    assert response.status_code == 422


# --------------------------------------------------------------------- 流式


def test_stream_emits_steps_then_delta_then_final(monkeypatch):
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)
    planner = ScriptedPlanner([
        tool_action("care_advice", materials=["蕾丝"]),
        final_action("[1] 装洗衣袋。"),
    ])
    events = list(loop.stream("蕾丝怎么洗", ctx=ctx(), planner=planner))
    kinds = [event["type"] for event in events]

    assert kinds[0] == "step"
    assert "delta" in kinds
    assert kinds[-1] == "final"
    assert events[-1]["answer"] == "[1] 装洗衣袋。"
    assert events[-1]["evidence"]
    text = "".join(event["text"] for event in events if event["type"] == "delta")
    assert text == "[1] 装洗衣袋。"


def test_stream_replaces_bad_citation(monkeypatch):
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)
    planner = ScriptedPlanner([
        tool_action("care_advice", materials=["蕾丝"]),
        final_action("有 [7] 条。"),
    ])
    events = list(loop.stream("蕾丝怎么洗", ctx=ctx(), planner=planner))

    assert any(event["type"] == "replace" for event in events)
    assert "引用编号对不上" in events[-1]["answer"]


def test_stream_refuses_without_evidence(monkeypatch):
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)
    planner = ScriptedPlanner([final_action("我觉得有。")])
    events = list(loop.stream("有吗", ctx=ctx(), planner=planner))

    assert events[-1]["refused"] is True
    assert "不猜" in events[-1]["answer"]


def test_stream_uses_writer_when_planner_gives_no_answer(monkeypatch):
    """真实模型的 final 不带答案 → 走写作器，逐段流出来。"""
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)

    class StubWriter:
        usage = {"calls": 1}

        def stream(self, question, evidence, history=None):
            yield "蕾丝"
            yield "要装洗衣袋[1]。"

    planner = ScriptedPlanner([
        tool_action("care_advice", materials=["蕾丝"]),
        {"action": "final"},
    ])
    events = list(loop.stream("蕾丝怎么洗", ctx=ctx(), planner=planner, writer=StubWriter()))
    text = "".join(event["text"] for event in events if event["type"] == "delta")

    assert text == "蕾丝要装洗衣袋[1]。"
    assert events[-1]["usage"].get("writer") == {"calls": 1}


def test_stream_writer_failure_falls_back(monkeypatch):
    monkeypatch.setattr(trace, "append", lambda *_a, **_k: None)

    class DeadWriter:
        usage = {}

        def stream(self, *_args, **_kwargs):
            raise RuntimeError("上游 502")

    planner = ScriptedPlanner([
        tool_action("care_advice", materials=["蕾丝"]),
        {"action": "final"},
    ])
    events = list(loop.stream("蕾丝怎么洗", ctx=ctx(), planner=planner, writer=DeadWriter()))

    assert any(event["type"] == "replace" and event["reason"] == "writer_failed" for event in events)
    assert events[-1]["answer"]


def test_stream_endpoint_emits_sse(client, monkeypatch):
    from app.routers import chat as chat_router

    planner = ScriptedPlanner([
        tool_action("care_advice", materials=["蕾丝"]),
        final_action("[1] 装洗衣袋。"),
    ])
    monkeypatch.setattr(chat_router, "_make_planner", lambda app=None: planner)
    monkeypatch.setattr(chat_router, "_make_writer", lambda app=None: None)

    response = client.post("/api/chat/stream", json={"message": "蕾丝怎么洗", "max_steps": 3})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    frames = [line for line in response.text.splitlines() if line.startswith("data: ")]
    events = [json.loads(frame[6:]) for frame in frames]
    assert events[0]["type"] == "step"
    assert events[-1]["type"] == "final"
    assert events[-1]["answer"] == "[1] 装洗衣袋。"


def test_stream_endpoint_without_key_is_503(client):
    assert client.post("/api/chat/stream", json={"message": "x"}).status_code == 503
