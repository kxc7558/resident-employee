"""主循环：把员工、引擎、闸门、限额、审计串起来跑一轮。

它是运行时唯一"知道全貌"的地方，也是**整轮闸门**的落点：

```
闸门（每次工具调用前）        整轮闸门（跑完/撞线才停）
  工具面                        步数   → max_steps
  参数形状                      花费   → max_cost_usd
  循环检测                      时间   → timeout_s
  授权
```

整轮闸门放在这里而不是 `gates.py`，是因为它们管的是"一轮"这个粒度，
不是"一次调用"。**分开之后两边都好测。**

审计：每次工具结论和每轮结束都落一条。允许和拒绝都落——
只记成功的审计查不出"谁试过碰不该碰的"。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

from ..authz.audit import EVENT_ALLOWED, EVENT_DENIED
from .employee import Employee
from .gates import GateChain, build_default_chain
from .ports import (
    KIND_DONE,
    KIND_ERROR,
    KIND_TEXT,
    KIND_TOOL_USE,
    AgentEngine,
    Event,
    GateDecision,
    TurnRequest,
)

EVENT_TURN_END = "turn_ended"
"""运行时自定的事件名：一轮结束（含停因、步数、花费）。"""

# 停因
STOP_DONE = "done"
STOP_MAX_STEPS = "max_steps"
STOP_COST = "cost_limit"
STOP_TIMEOUT = "timeout"
STOP_ERROR = "error"

STOP_LABELS: dict[str, str] = {
    STOP_DONE: "正常结束",
    STOP_MAX_STEPS: "撞到步数上限",
    STOP_COST: "撞到花费上限",
    STOP_TIMEOUT: "超时",
    STOP_ERROR: "出错",
}


@dataclass(frozen=True)
class TurnResult:
    """一轮的完整交代。前台要显示的东西基本都在这儿。"""

    session: str
    text: str
    events: tuple[Event, ...] = field(default_factory=tuple)
    steps: int = 0
    """工具调用次数。"""
    cost_usd: float = 0.0
    stop_reason: str = STOP_DONE
    denials: tuple[tuple[str, GateDecision], ...] = field(default_factory=tuple)
    resume: str = ""
    """引擎给的续接句柄，下一轮带回去就能接着聊。"""

    @property
    def stop_label(self) -> str:
        return STOP_LABELS.get(self.stop_reason, self.stop_reason)

    def brief(self) -> str:
        parts = [f"轮数/步数 {self.steps}", f"花费 {self.cost_usd:.4f}", self.stop_label]
        if self.denials:
            parts.append(f"被拦 {len(self.denials)} 次")
        return " | ".join(parts)


class EmployeeRunner:
    """跑一个员工的运行时。**不认识 Claude Agent SDK**，只认识 `AgentEngine`。"""

    def __init__(
        self,
        employee: Employee,
        engine: AgentEngine,
        *,
        chain: GateChain | None = None,
        audit: Any | None = None,
        allowed_tools: tuple[str, ...] = (),
        max_steps: int = 40,
        max_cost_usd: float | None = None,
        timeout_s: float = 180.0,
        actor: str = "员工",
    ) -> None:
        self.employee = employee.validate(require_all=False)
        self.engine = engine
        self.allowed_tools = tuple(allowed_tools)
        self.chain = chain or build_default_chain(allowed_tools=self.allowed_tools)
        self.audit = audit
        self.max_steps = max_steps
        self.max_cost_usd = max_cost_usd
        self.timeout_s = timeout_s
        self.actor = actor

    # --- 内部 -------------------------------------------------------------

    async def _gated(self, tool: str, args: Mapping[str, Any]) -> GateDecision:
        """过闸门 + 落审计。**放行和拒绝都记。**"""
        decision = await self.chain(tool, args)
        if self.audit is not None:
            self.audit.append(
                EVENT_ALLOWED if decision.allowed else EVENT_DENIED,
                actor=self.actor,
                detail={
                    "tool": tool,
                    "args": dict(args),
                    "gate": decision.gate,
                    "reason": decision.reason,
                    "employee": self.employee.name,
                },
            )
        if not decision.allowed:
            # 把拒绝理由原样回给引擎 → 它会回灌给模型让它自我纠正
            return decision
        return decision

    # --- 跑一轮 -----------------------------------------------------------

    async def run_turn(
        self,
        prompt: str,
        *,
        session: str = "",
        resume: str = "",
        on_event: Callable[[Event], Awaitable[None]] | None = None,
    ) -> TurnResult:
        """跑一轮。`on_event` 用来把过程实时推给前台（**过程可见**）。"""
        self.chain.reset()

        request = TurnRequest(
            prompt=prompt,
            session=session,
            resume=resume,
            system_prompt=self.employee.to_system_prompt(),
            max_steps=self.max_steps,
            max_cost_usd=self.max_cost_usd,
            allowed_tools=self.allowed_tools,
        )

        events: list[Event] = []
        texts: list[str] = []
        steps = 0
        cost = 0.0
        stop_reason = STOP_DONE
        resume_handle = resume

        async def keep(event: Event) -> None:
            nonlocal resume_handle
            events.append(event)
            if event.kind == KIND_TEXT:
                texts.append(event.text)
            # 引擎在事件里捎回的续接句柄——下一轮带回去就能接着聊
            session_id = event.meta.get("session_id") if event.meta else None
            if session_id:
                resume_handle = str(session_id)
            if on_event is not None:
                await on_event(event)

        try:
            async with asyncio.timeout(self.timeout_s):
                async for event in self.engine.run(request, self._gated):
                    if event.kind == KIND_TOOL_USE:
                        steps += 1
                    if event.cost_usd:
                        cost = max(cost, event.cost_usd)
                    await keep(event)

                    if event.kind == KIND_ERROR:
                        stop_reason = STOP_ERROR
                        break
                    if event.kind == KIND_DONE:
                        break
                    if steps > self.max_steps:
                        stop_reason = STOP_MAX_STEPS
                        break
                    if self.max_cost_usd is not None and cost > self.max_cost_usd:
                        stop_reason = STOP_COST
                        break
        except TimeoutError:
            stop_reason = STOP_TIMEOUT
        except Exception as exc:  # 引擎崩了不能把整轮吞掉
            stop_reason = STOP_ERROR
            await keep(Event(kind=KIND_ERROR, text=f"{type(exc).__name__}: {exc}", ok=False))

        # 花费只有轮末才确切知道（Agent SDK 的 total_cost_usd 在 ResultMessage 上），
        # 所以**轮末再核对一次**：超了就如实地报出来，别假装能提前拦住。
        # 真正在轮内拦的是引擎自己的预算闸（我们通过 TurnRequest.max_cost_usd 传下去）。
        if (
            stop_reason == STOP_DONE
            and self.max_cost_usd is not None
            and cost > self.max_cost_usd
        ):
            stop_reason = STOP_COST

        result = TurnResult(
            session=session,
            text="".join(texts).strip(),
            events=tuple(events),
            steps=steps,
            cost_usd=cost,
            stop_reason=stop_reason,
            denials=tuple(self.chain.denials()),
            resume=resume_handle,
        )

        if self.audit is not None:
            self.audit.append(
                EVENT_TURN_END,
                actor=self.actor,
                detail={
                    "session": session,
                    "employee": self.employee.name,
                    "stop_reason": stop_reason,
                    "steps": steps,
                    "cost_usd": cost,
                    "denied": len(result.denials),
                },
            )
        return result
