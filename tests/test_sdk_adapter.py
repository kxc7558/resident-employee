"""SDK 适配器测试。

**为什么能测**：翻译（SDK 消息 → 运行时事件）和闸门包装（运行时 Gate →
`can_use_tool`）都是纯函数，构造出 SDK 的消息对象就能验——

这两处恰恰是**最容易写错、错了又最难看出来的地方**（字段名记错、
拒绝理由没接上、`interrupt` 设错导致整轮终止）。跑不起 agent 不等于
测不了它。

唯一没法在这里验的是「真把 agent 起起来」——那一步要人工跑
`verify_sdk.py`。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from resident_employee.runtime.adapters.claude_agent_sdk import (
    HAVE_SDK,
    ClaudeAgentSDKEngine,
    relay_env_from_settings,
)
from resident_employee.runtime.gates import GateChain, ToolScopeGate
from resident_employee.runtime.ports import GateDecision

pytestmark = pytest.mark.skipif(not HAVE_SDK, reason="需要 claude-agent-sdk")

sdk = pytest.importorskip("claude_agent_sdk")


def run(coro):
    return asyncio.run(coro)


def make_engine() -> ClaudeAgentSDKEngine:
    return ClaudeAgentSDKEngine(env={})


# --- 翻译：助手消息 ---------------------------------------------------------


def test_text_block_becomes_text_event():
    message = sdk.AssistantMessage(
        content=[sdk.TextBlock(text="一切正常")], model="m"
    )

    events = make_engine()._translate(message)

    assert len(events) == 1
    assert events[0].kind == "text"
    assert events[0].text == "一切正常"


def test_thinking_block_gets_its_own_kind():
    """思考单独一类，好让前台决定折叠还是隐藏。"""
    message = sdk.AssistantMessage(
        content=[sdk.ThinkingBlock(thinking="先看看再动手", signature="sig")], model="m"
    )

    events = make_engine()._translate(message)

    assert events[0].kind == "thinking"
    assert "先看看" in events[0].text


def test_tool_use_block_carries_tool_and_args():
    """**「过程可见」靠的就是这个事件**——前台据此显示"正在干什么"。"""
    message = sdk.AssistantMessage(
        content=[sdk.ToolUseBlock(id="t1", name="Read", input={"file_path": "a.txt"})],
        model="m",
    )

    events = make_engine()._translate(message)

    assert events[0].kind == "tool_use"
    assert events[0].tool == "Read"
    assert events[0].args == {"file_path": "a.txt"}


def test_multiple_blocks_keep_order():
    message = sdk.AssistantMessage(
        content=[
            sdk.TextBlock(text="我先看"),
            sdk.ToolUseBlock(id="t1", name="Read", input={"file_path": "a"}),
            sdk.TextBlock(text="看完了"),
        ],
        model="m",
    )

    kinds = [e.kind for e in make_engine()._translate(message)]

    assert kinds == ["text", "tool_use", "text"]


def test_empty_content_is_safe():
    message = sdk.AssistantMessage(content=[], model="m")
    assert make_engine()._translate(message) == []


# --- 翻译：工具结果与结束 ---------------------------------------------------


def test_tool_result_marks_failure():
    message = sdk.UserMessage(content=[], tool_use_result={"is_error": True})

    events = make_engine()._translate(message)

    assert events[0].kind == "tool_result"
    assert events[0].ok is False


def test_successful_result_is_done_with_cost():
    message = sdk.ResultMessage(
        subtype="success",
        duration_ms=1200,
        duration_api_ms=900,
        is_error=False,
        num_turns=3,
        session_id="sess-42",
        total_cost_usd=0.0123,
        result="干完了",
    )

    events = make_engine()._translate(message)

    assert events[0].kind == "done"
    assert events[0].cost_usd == pytest.approx(0.0123)
    assert events[0].meta["session_id"] == "sess-42", "续接句柄要捎回来"


def test_error_result_becomes_error_event():
    message = sdk.ResultMessage(
        subtype="error_max_turns",
        duration_ms=100,
        duration_api_ms=90,
        is_error=True,
        num_turns=40,
        session_id="sess-1",
        total_cost_usd=0.5,
        result="步数用尽",
    )

    events = make_engine()._translate(message)

    assert events[0].kind == "error"
    assert events[0].ok is False
    assert "步数用尽" in events[0].text


def test_unknown_message_is_ignored_not_crashing():
    assert make_engine()._translate(object()) == []


def test_cost_none_is_zero():
    message = sdk.ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s",
        total_cost_usd=None,
    )
    assert make_engine()._translate(message)[0].cost_usd == 0.0


# --- 闸门包装（Gate → can_use_tool）-----------------------------------------


def test_allowed_decision_maps_to_allow():
    async def gate(tool, args):
        return GateDecision.allow("工具面")

    can_use_tool = ClaudeAgentSDKEngine._wrap_gate(gate)
    result = run(can_use_tool("Read", {"file_path": "a"}, None))

    assert result.behavior == "allow"
    assert result.updated_input is None


def test_rewritten_args_are_passed_through():
    """允许时改写参数——安全件里"改对再放行"比"一律拒绝"更有用。"""
    async def gate(tool, args):
        return GateDecision.allow("参数形状", rewritten={"file_path": "/safe/a"})

    can_use_tool = ClaudeAgentSDKEngine._wrap_gate(gate)
    result = run(can_use_tool("Read", {"file_path": "a"}, None))

    assert result.updated_input == {"file_path": "/safe/a"}


def test_denied_decision_maps_to_deny_with_reason():
    """拒绝理由必须走 `message`——那是回灌给模型的通道。"""
    async def gate(tool, args):
        return GateDecision.deny("Write 不在你的工具面里", "工具面")

    can_use_tool = ClaudeAgentSDKEngine._wrap_gate(gate)
    result = run(can_use_tool("Write", {}, None))

    assert result.behavior == "deny"
    assert result.message == "Write 不在你的工具面里"


def test_denial_does_not_interrupt_the_turn():
    """**不要 `interrupt=True`** —— 那会整轮终止，模型没机会自我纠正。

    拒绝是为了让它换个做法，不是为了把整轮打死。
    """
    async def gate(tool, args):
        return GateDecision.deny("不行")

    can_use_tool = ClaudeAgentSDKEngine._wrap_gate(gate)
    result = run(can_use_tool("Bash", {}, None))

    assert result.interrupt is False


def test_denial_without_reason_still_has_a_message():
    async def gate(tool, args):
        return GateDecision(allowed=False)

    can_use_tool = ClaudeAgentSDKEngine._wrap_gate(gate)
    result = run(can_use_tool("Bash", {}, None))

    assert result.message, "空理由会让模型完全不知道发生了什么"


def test_chain_wires_into_can_use_tool():
    """端到端：真实的一道链 → can_use_tool。"""
    chain = GateChain([ToolScopeGate(["Read"])])
    can_use_tool = ClaudeAgentSDKEngine._wrap_gate(chain)

    assert run(can_use_tool("Read", {"file_path": "a"}, None)).behavior == "allow"
    denied = run(can_use_tool("Write", {"file_path": "a"}, None))
    assert denied.behavior == "deny"
    assert "Write" in denied.message


# --- 后端配置 ---------------------------------------------------------------


def test_relay_env_reads_settings(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:15721", "ANTHROPIC_AUTH_TOKEN": "tok"}}),
        encoding="utf-8",
    )
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)

    env = relay_env_from_settings(settings)

    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:15721"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "tok"


def test_relay_env_survives_missing_file(tmp_path):
    """配置不在也不能崩——回退到当前环境。"""
    env = relay_env_from_settings(tmp_path / "不存在.json")
    assert isinstance(env, dict)


def test_relay_env_survives_broken_json(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text("{不是 json", encoding="utf-8")
    assert isinstance(relay_env_from_settings(settings), dict)


def test_engine_uses_given_env():
    engine = ClaudeAgentSDKEngine(env={"ANTHROPIC_BASE_URL": "http://x"})
    assert engine.env["ANTHROPIC_BASE_URL"] == "http://x"
