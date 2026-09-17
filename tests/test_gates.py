"""闸门测试。

对应规范 `outsourced-employee.md` 的故障预案表——四个闸门各测一遍，
外加"拒绝理由必须能回灌"这条最容易偷懒的要求。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from resident_employee.runtime.gates import (
    AuthorizationGate,
    GateChain,
    LoopBreakerGate,
    ToolSchemaGate,
    ToolScopeGate,
    build_default_chain,
    fingerprint,
)


def run(coro):
    return asyncio.run(coro)


def check(gate, tool, args=None):
    return run(gate.check(tool, args or {}))


# --- 工具面收窄 -------------------------------------------------------------


def test_scope_allows_listed_tool():
    gate = ToolScopeGate(["Read", "Bash"])
    assert check(gate, "Read", {"file_path": "x"}).allowed


def test_scope_denies_unlisted_tool_with_actionable_reason():
    """理由必须说清「可用的有哪几个」——否则模型只能瞎试下一个。"""
    gate = ToolScopeGate(["Read", "Bash"])

    decision = check(gate, "Write", {"file_path": "x"})

    assert not decision.allowed
    assert "Write" in decision.reason
    assert "Read" in decision.reason  # 告诉它有哪些能用
    assert decision.gate == "工具面"


def test_scope_empty_means_no_restriction():
    """不传工具面 = 不限制（给不做收窄的场景用）。"""
    gate = ToolScopeGate(None)
    assert check(gate, "任意工具").allowed


# --- 参数形状 ---------------------------------------------------------------


def test_schema_flags_missing_required_args():
    gate = ToolSchemaGate(required={"Read": ["file_path"]})

    decision = check(gate, "Read", {})

    assert not decision.allowed
    assert "file_path" in decision.reason
    assert "缺必填参数" in decision.reason


@pytest.mark.parametrize("bad", ["", None])
def test_schema_treats_blank_as_missing(bad):
    """空串和 None 都算没填——不然模型可以用空值糊过去。"""
    gate = ToolSchemaGate(required={"Read": ["file_path"]})
    assert not check(gate, "Read", {"file_path": bad}).allowed


def test_schema_flags_forbidden_args():
    gate = ToolSchemaGate(forbidden={"Bash": ["dangerously_skip_permissions"]})

    decision = check(gate, "Bash", {"command": "ls", "dangerously_skip_permissions": True})

    assert not decision.allowed
    assert "dangerously_skip_permissions" in decision.reason


def test_schema_passes_when_shape_ok():
    gate = ToolSchemaGate(required={"Read": ["file_path"]})
    assert check(gate, "Read", {"file_path": "x"}).allowed


# --- 循环检测（官方没有，必须自建）------------------------------------------


def test_loop_breaker_allows_normal_retries():
    """允许正常重试（第一次超时了再试一次），不是一重复就拦。"""
    gate = LoopBreakerGate(max_repeats=3)
    for _ in range(3):
        assert check(gate, "Bash", {"command": "ls"}).allowed


def test_loop_breaker_blocks_after_limit():
    gate = LoopBreakerGate(max_repeats=3)
    for _ in range(3):
        check(gate, "Bash", {"command": "ls"})

    decision = check(gate, "Bash", {"command": "ls"})

    assert not decision.allowed
    assert decision.gate == "循环检测"
    assert "完全相同的参数" in decision.reason


def test_loop_breaker_ignores_arg_order():
    """键序不同但内容相同的参数是**同一个调用**——指纹必须规范化。"""
    gate = LoopBreakerGate(max_repeats=1)
    check(gate, "Bash", {"command": "ls", "timeout": 5})

    decision = check(gate, "Bash", {"timeout": 5, "command": "ls"})

    assert not decision.allowed, "键序不同不该被当成新调用"


def test_loop_breaker_counts_per_arguments():
    """换了参数就是新调用，不该被拦住。"""
    gate = LoopBreakerGate(max_repeats=1)
    check(gate, "Bash", {"command": "ls"})

    assert check(gate, "Bash", {"command": "pwd"}).allowed


def test_loop_breaker_reset():
    gate = LoopBreakerGate(max_repeats=1)
    check(gate, "Bash", {"command": "ls"})
    gate.reset()
    assert check(gate, "Bash", {"command": "ls"}).allowed


def test_loop_breaker_rejects_bad_limit():
    with pytest.raises(ValueError, match="max_repeats"):
        LoopBreakerGate(max_repeats=0)


def test_fingerprint_is_stable():
    assert fingerprint("T", {"b": 1, "a": 2}) == fingerprint("T", {"a": 2, "b": 1})
    assert fingerprint("T", {"a": 1}) != fingerprint("T", {"a": 2})


# --- 授权 -------------------------------------------------------------------


@dataclass
class FakeVerdict:
    allowed: bool
    code: str = ""
    reason: str = ""


def make_auth_gate(verdict: FakeVerdict, seen: list | None = None):
    async def decide(action, args):
        if seen is not None:
            seen.append(action)
        return verdict

    return AuthorizationGate(decide, free={"Read"}, resource="api-key-pool")


def test_auth_gate_lets_free_tools_through():
    """只读免签——不然读个文件都要人类签一次，没法用。"""
    seen: list[str] = []
    gate = make_auth_gate(FakeVerdict(allowed=False), seen)

    assert check(gate, "Read", {"file_path": "x"}).allowed
    assert seen == [], "免签工具不该去问授权"


def test_auth_gate_asks_for_guarded_tools():
    seen: list[str] = []
    gate = make_auth_gate(FakeVerdict(allowed=True), seen)

    assert check(gate, "Bash", {"command": "ls"}).allowed
    assert seen == ["Bash"]


def test_auth_gate_denial_forbids_workarounds():
    """拒绝理由里必须堵住"换个说法再来一次"——那是绕过，不是纠错。"""
    gate = make_auth_gate(FakeVerdict(allowed=False, code="ACTION_NOT_ALLOWED", reason="不在白名单"))

    decision = check(gate, "Bash", {"command": "rm -rf /"})

    assert not decision.allowed
    assert "ACTION_NOT_ALLOWED" in decision.reason
    assert "不要换成别的说法" in decision.reason
    assert "绕过" in decision.reason


# --- 链 ---------------------------------------------------------------------


def test_chain_first_denial_wins():
    chain = GateChain([ToolScopeGate(["Read"]), LoopBreakerGate(1)])

    decision = run(chain("Write", {"file_path": "x"}))

    assert not decision.allowed
    assert decision.gate == "工具面", "先命中的是工具面，不该跑到循环检测"


def test_chain_records_every_decision():
    chain = GateChain([ToolScopeGate(["Read", "Bash"])])
    run(chain("Read", {"a": 1}))
    run(chain("Write", {"a": 1}))

    assert len(chain.decisions) == 2
    assert len(chain.denials()) == 1


def test_chain_reset_clears_history():
    chain = GateChain([ToolScopeGate(["Read"])])
    run(chain("Write", {}))
    chain.reset()
    assert chain.decisions == []


def test_default_chain_skips_auth_when_no_decider():
    chain = build_default_chain(allowed_tools=["Read"])
    assert not any(isinstance(g, AuthorizationGate) for g in chain.gates)


def test_default_chain_includes_auth_when_decider_given():
    async def decide(action, args):
        return FakeVerdict(allowed=True)

    chain = build_default_chain(allowed_tools=["Read"], decide=decide)
    assert any(isinstance(g, AuthorizationGate) for g in chain.gates)


def test_default_chain_order_cheap_first():
    """便宜的判断放前面——工具面是纯内存查表，授权可能落盘。"""
    async def decide(action, args):
        return FakeVerdict(allowed=True)

    chain = build_default_chain(allowed_tools=["Read"], decide=decide)
    names = [g.name for g in chain.gates]
    assert names.index("工具面") < names.index("授权")
    assert names.index("循环检测") < names.index("授权")
