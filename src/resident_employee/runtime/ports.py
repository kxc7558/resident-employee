"""运行时与「引擎」之间的接口。

**为什么要有这层接口**（不是为将来的猜测做准备，是当下就有用）：

1. **可测**。运行时自己的逻辑——闸门、循环检测、成本累计、审计、事件流——
   必须能脱离"真起一个 agent 进程"来测。否则每次跑测试都要花真钱、真起进程、
   还依赖网络。
2. **可换**。驱动的是 `glm-5.3-flash` 这类模型。它扛不住 agent 用法时，
   换引擎不该重写运行时。

所以：**运行时核心不认识 Claude Agent SDK**，只认识这里的 `AgentEngine`。
SDK 的适配器在 `adapters/claude_agent_sdk.py`，是唯一 import 它的地方。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping, Protocol

# --- 事件类型 ---------------------------------------------------------------

KIND_TEXT = "text"
"""模型说的话。"""

KIND_THINKING = "thinking"
"""模型的思考过程。单独一类，好让前台决定折叠还是隐藏。"""

KIND_TOOL_USE = "tool_use"
"""模型要调用工具。**这是「过程可见」的关键信号**——前台靠它显示"正在干什么"。"""

KIND_TOOL_RESULT = "tool_result"
"""工具执行结果回灌。"""

KIND_DONE = "done"
"""一轮结束。"""

KIND_ERROR = "error"
"""出错。"""


@dataclass(frozen=True)
class Event:
    """引擎吐出来的一个事件。

    故意做得**与具体引擎无关**：不管底下是 Claude Agent SDK 还是别的，
    前台看到的都是这几种。
    """

    kind: str
    text: str = ""
    tool: str = ""
    args: Mapping[str, Any] = field(default_factory=dict)
    ok: bool = True
    cost_usd: float = 0.0
    meta: Mapping[str, Any] = field(default_factory=dict)

    def brief(self) -> str:
        """一行摘要，给日志和前台用。"""
        if self.kind == KIND_TEXT:
            return f"说：{self.text[:60]}"
        if self.kind == KIND_THINKING:
            return f"想：{self.text[:60]}"
        if self.kind == KIND_TOOL_USE:
            return f"做：{self.tool}"
        if self.kind == KIND_TOOL_RESULT:
            return f"结果：{'成功' if self.ok else '失败'}"
        if self.kind == KIND_DONE:
            return f"结束（花费 {self.cost_usd:.4f}）"
        return f"{self.kind}：{self.text[:60]}"


# --- 闸门 -------------------------------------------------------------------

DECISION_ALLOW = "allow"
DECISION_DENY = "deny"


@dataclass(frozen=True)
class GateDecision:
    """一次工具调用的放行结论。

    `reason` 在拒绝时**会回灌给模型**（Agent SDK 的 `can_use_tool` 语义），
    所以它必须说清「哪一条不允许」+「改成什么」——
    模型靠这个自我纠正，而不是从一句 denied 里猜。
    """

    allowed: bool
    reason: str = ""
    gate: str = ""
    """哪个闸门下的结论——审计和排障靠它定位。"""

    rewritten: Mapping[str, Any] | None = None
    """允许时可选：把参数改对了再放行。"""

    @classmethod
    def allow(cls, gate: str = "", rewritten: Mapping[str, Any] | None = None) -> "GateDecision":
        return cls(allowed=True, gate=gate, rewritten=rewritten)

    @classmethod
    def deny(cls, reason: str, gate: str = "") -> "GateDecision":
        return cls(allowed=False, reason=reason, gate=gate)

    def brief(self) -> str:
        head = "放行" if self.allowed else "拒绝"
        tail = f"（{self.gate}）" if self.gate else ""
        return f"{head}{tail}" + (f"：{self.reason}" if self.reason else "")


Gate = Callable[[str, Mapping[str, Any]], Awaitable[GateDecision]]
"""一个闸门：拿到「工具名 + 参数」，给出放行或拒绝。

执行前调用——**在动作发生之前**，不是在事后检查日志。
"""


# --- 引擎 -------------------------------------------------------------------


@dataclass(frozen=True)
class TurnRequest:
    """一轮请求。"""

    prompt: str
    session: str = ""
    """会话标识。同一会话的历史由引擎侧按它续接。"""

    resume: str = ""
    """引擎给的续接句柄（Agent SDK 的 session_id）。空 = 新会话。"""

    system_prompt: str = ""

    max_steps: int = 40
    """步数上限。超了就停——**内建**的失控防护（Agent SDK 的 max_turns）。"""

    max_cost_usd: float | None = None
    """花费上限。`None` = 不限。"""

    allowed_tools: tuple[str, ...] = ()
    """工具面。**只发它该有的那几把**——这是"选错工具"的第一道闸。"""

    cwd: str = ""


@dataclass(frozen=True)
class EngineHandles:
    """引擎跑完后回传的句柄，供下一轮续接。"""

    resume: str = ""
    session: str = ""


class AgentEngine(Protocol):
    """引擎接口。**运行时不认识 Claude Agent SDK，只认识这个。**"""

    def run(
        self,
        request: TurnRequest,
        gate: Gate,
    ) -> AsyncIterator[Event]:
        """跑一轮，边跑边吐事件。

        `gate` 必须在**每次工具调用之前**被调用；拒绝时把 reason 回灌给模型，
        让它自我纠正（而不是整轮直接失败）。
        """
        ...
