"""主循环测试（用假引擎，不花钱不起进程）。

验的是**整轮闸门**和**交代是否完整**：步数、花费、停因、被拦记录、审计、过程可见。
这些正是规范里"做完必须验证、交付即留痕"那几条的落点。
"""

from __future__ import annotations

import asyncio

import pytest

from fakes import FakeEngine

from resident_employee.authz import AuditChain
from resident_employee.authz.audit import EVENT_ALLOWED, EVENT_DENIED
from resident_employee.runtime import (
    STOP_COST,
    STOP_DONE,
    STOP_ERROR,
    STOP_MAX_STEPS,
    STOP_TIMEOUT,
    Employee,
    EmployeeRunner,
    Event,
    build_default_chain,
)
from resident_employee.runtime.gates import GateChain, LoopBreakerGate, ToolScopeGate


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def employee() -> Employee:
    return Employee(
        name="key-pool-operator",
        system="代理池在 127.0.0.1:8787。",
        actions="`status` 看健康；`keys` 看 Key 明细。",
        boundaries="只读直接做；删除类要人确认。",
        pitfalls="同账号多 Key 不叠加额度。",
        triage="先 status，再 keys，再 logs。",
        output_format="按【做了什么】【结果】【验证】【影响】四行说。",
    ).validate()


@pytest.fixture
def audit(tmp_path) -> AuditChain:
    return AuditChain(tmp_path / "audit.jsonl")


def make_runner(employee, script, audit=None, **kwargs) -> tuple[EmployeeRunner, FakeEngine]:
    engine = FakeEngine(script)
    chain = kwargs.pop("chain", None) or build_default_chain(
        allowed_tools=kwargs.get("allowed_tools") or ("Read", "Bash")
    )
    runner = EmployeeRunner(employee, engine, chain=chain, audit=audit, **kwargs)
    return runner, engine


# --- 正常一轮 ---------------------------------------------------------------


def test_happy_path_collects_text_and_limits(employee):
    runner, engine = make_runner(
        employee,
        [("text", "看完了，一切正常。"), ("done",)],
    )

    result = run(runner.run_turn("看看系统健康"))

    assert result.stop_reason == STOP_DONE
    assert "一切正常" in result.text
    assert result.steps == 0
    assert result.cost_usd == pytest.approx(0.01)
    assert result.resume == "fake-session"


def test_system_prompt_carries_the_six_sections(employee):
    """员工定义必须真的进到引擎请求里——不然写六段等于没写。"""
    runner, engine = make_runner(employee, [("done",)])

    run(runner.run_turn("随便"))

    prompt = engine.request.system_prompt
    for label in ("① 我管的系统", "② 常用动作", "③ 权限边界", "④ 这个系统的坑", "⑤ 排障顺序", "⑥ 输出要求"):
        assert label in prompt


def test_tool_scope_reaches_the_engine(employee):
    """工具面收窄要传到引擎——让模型根本没有别的工具可挑。"""
    runner, engine = make_runner(employee, [("done",)], allowed_tools=("Read",))

    run(runner.run_turn("随便"))

    assert engine.request.allowed_tools == ("Read",)


def test_steps_count_tool_calls(employee):
    runner, _ = make_runner(
        employee,
        [("tool", "Read", {"file_path": "a"}), ("tool", "Bash", {"command": "ls"}), ("done",)],
    )

    result = run(runner.run_turn("干活"))

    assert result.steps == 2


# --- 闸门真的在拦 -----------------------------------------------------------


def test_denied_tool_is_recorded_in_result(employee):
    runner, _ = make_runner(
        employee,
        [("tool", "Write", {"file_path": "x"}), ("text", "这个我做不了。"), ("done",)],
    )

    result = run(runner.run_turn("写个文件"))

    assert len(result.denials) == 1
    tool, decision = result.denials[0]
    assert tool == "Write"
    assert decision.gate == "工具面"


def test_denial_reason_does_not_leak_into_the_reply(employee):
    """拒绝理由是**回灌给模型的反馈**，不是给用户看的输出。

    曾把拒绝理由当文本事件吐出去，结果它被拼进了回复正文——
    用户会看到一长串本不该出现的内部提示。
    """
    runner, _ = make_runner(employee, [("tool", "Write", {"file_path": "x"}), ("done",)])

    result = run(runner.run_turn("写个文件"))

    assert "不在你的工具面" not in result.text
    assert result.text == ""


def test_denial_reason_travels_in_tool_result_meta(employee):
    """理由要留着（前台和排障要用）——但待在 tool_result 里，不冒充模型说的话。"""
    runner, _ = make_runner(employee, [("tool", "Write", {"file_path": "x"}), ("done",)])

    result = run(runner.run_turn("写个文件"))

    results = [e for e in result.events if e.kind == "tool_result"]
    assert results[0].ok is False
    assert results[0].meta["denied"] is True
    assert "Write" in results[0].meta["reason"]


def test_loop_breaker_stops_repetition(employee):
    script = [("tool", "Read", {"file_path": "same"})] * 5 + [("done",)]
    chain = GateChain([ToolScopeGate(["Read"]), LoopBreakerGate(max_repeats=2)])
    runner, _ = make_runner(employee, script, chain=chain)

    result = run(runner.run_turn("读同一个文件"))

    assert len(result.denials) >= 3
    assert all(d.gate == "循环检测" for _, d in result.denials if d.gate == "循环检测")


def test_chain_resets_between_turns(employee):
    """上一轮的重复计数不能带到下一轮，否则第二问就被误拦。"""
    chain = GateChain([ToolScopeGate(["Read"]), LoopBreakerGate(max_repeats=1)])
    runner, _ = make_runner(employee, [("tool", "Read", {"file_path": "a"}), ("done",)], chain=chain)

    first = run(runner.run_turn("第一问"))
    second = run(runner.run_turn("第二问"))

    assert not first.denials
    assert not second.denials


# --- 整轮闸门 ---------------------------------------------------------------


def test_max_steps_stops_the_turn(employee):
    script = [("tool", "Read", {"file_path": f"f{i}"}) for i in range(6)] + [("done",)]
    runner, _ = make_runner(employee, script, max_steps=2)

    result = run(runner.run_turn("读一堆文件"))

    assert result.stop_reason == STOP_MAX_STEPS
    assert result.stop_label == "撞到步数上限"


def test_cost_limit_is_reported_at_turn_end(employee):
    """花费只有轮末才确切知道（total_cost_usd 在 ResultMessage 上）。

    所以这里验的是**如实上报**：超预算就报 cost_limit，不假装能提前拦住；
    真正在轮内拦的是引擎自己的预算闸——所以还要确认限额**传下去了**。
    """
    engine = FakeEngine([("text", "干完了"), ("done",)], cost=10.0)
    runner = EmployeeRunner(
        employee, engine, chain=build_default_chain(allowed_tools=("Read",)), max_cost_usd=1.0
    )

    result = run(runner.run_turn("贵活"))

    assert result.stop_reason == STOP_COST
    assert engine.request.max_cost_usd == 1.0, "限额必须传给引擎，否则轮内没人拦"


def test_cost_within_limit_does_not_report_cost_stop(employee):
    engine = FakeEngine([("text", "干完了"), ("done",)], cost=0.5)
    runner = EmployeeRunner(
        employee, engine, chain=build_default_chain(allowed_tools=("Read",)), max_cost_usd=1.0
    )

    assert run(runner.run_turn("便宜活")).stop_reason == STOP_DONE


def test_timeout_stops_the_turn(employee):
    runner, _ = make_runner(employee, [("sleep", 5), ("done",)], timeout_s=0.05)

    result = run(runner.run_turn("卡住的活"))

    assert result.stop_reason == STOP_TIMEOUT
    assert result.stop_label == "超时"


def test_engine_crash_is_captured_not_swallowed(employee):
    """引擎崩了不能把整轮吞掉——要变成一条 error 事件和明确的停因。"""

    class BrokenEngine:
        async def run(self, request, gate):
            raise RuntimeError("引擎炸了")
            yield  # pragma: no cover - 让它是异步生成器

    runner = EmployeeRunner(
        employee, BrokenEngine(), chain=build_default_chain(allowed_tools=("Read",))
    )

    result = run(runner.run_turn("随便"))

    assert result.stop_reason == STOP_ERROR
    assert any(e.kind == "error" for e in result.events)
    assert "引擎炸了" in result.text or any("引擎炸了" in e.text for e in result.events)


# --- 过程可见 ---------------------------------------------------------------


def test_on_event_streams_in_order(employee):
    """前台靠这个把"它正在干什么"实时显示出来——不能等跑完才给。"""
    seen: list[Event] = []

    async def collect(event: Event) -> None:
        seen.append(event)

    runner, _ = make_runner(
        employee, [("text", "先看看"), ("tool", "Read", {"file_path": "a"}), ("done",)]
    )

    run(runner.run_turn("干活", on_event=collect))

    kinds = [e.kind for e in seen]
    assert kinds[0] == "text"
    assert "tool_use" in kinds
    assert kinds[-1] == "done"


def test_tool_use_event_carries_args(employee):
    runner, _ = make_runner(employee, [("tool", "Read", {"file_path": "a.txt"}), ("done",)])

    result = run(runner.run_turn("读文件"))

    calls = [e for e in result.events if e.kind == "tool_use"]
    assert calls[0].tool == "Read"
    assert calls[0].args == {"file_path": "a.txt"}


# --- 审计 -------------------------------------------------------------------


def test_audit_records_allowed_and_denied(employee, audit):
    """只记成功的审计查不出"谁试过碰不该碰的"——所以拒绝也要记。"""
    runner, _ = make_runner(
        employee,
        [("tool", "Read", {"file_path": "a"}), ("tool", "Write", {"file_path": "b"}), ("done",)],
        audit=audit,
    )

    run(runner.run_turn("干活"))

    events = [e.event for e in audit]
    assert EVENT_ALLOWED in events
    assert EVENT_DENIED in events


def test_audit_records_turn_end(employee, audit):
    runner, _ = make_runner(employee, [("text", "完事"), ("done",)], audit=audit)

    run(runner.run_turn("干活"))

    last = audit.tail(1)[0]
    assert last.event == "turn_ended"
    assert last.detail["stop_reason"] == STOP_DONE
    assert last.detail["employee"] == "key-pool-operator"


def test_audit_chain_survives_a_full_turn(employee, audit):
    runner, _ = make_runner(
        employee,
        [("tool", "Read", {"file_path": "a"}), ("tool", "Write", {"x": 1}), ("done",)],
        audit=audit,
    )

    run(runner.run_turn("干活"))

    ok, note = audit.verify()
    assert ok, note


def test_result_brief_is_one_line(employee):
    runner, _ = make_runner(employee, [("done",)])
    result = run(runner.run_turn("随便"))

    assert "\n" not in result.brief()
    assert "正常结束" in result.brief()
