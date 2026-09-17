"""测试用的假引擎。

**这是「引擎搁在接口后面」的直接好处**：运行时自己的逻辑——闸门、循环检测、
成本累计、审计、事件流——全都能脱离"真起一个 agent 进程"来测。
否则每次跑测试都要花真钱、真起进程、还依赖网络。

假引擎按剧本走，并且**每次工具调用都真过一遍闸门**——不是绕过闸门直接吐事件，
否则测的就不是真路径了。
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Mapping, Sequence

from resident_employee.runtime.ports import (
    KIND_DONE,
    KIND_TEXT,
    KIND_TOOL_RESULT,
    KIND_TOOL_USE,
    Event,
    Gate,
    TurnRequest,
)

Step = tuple
"""剧本里的一步：``("text", 文本)`` / ``("tool", 工具名, 参数)`` / ``("done",)`` / ``("sleep", 秒)``。"""


class FakeEngine:
    """按剧本跑的假引擎。"""

    def __init__(
        self,
        script: Sequence[Step],
        *,
        cost: float = 0.01,
        session_id: str = "fake-session",
    ) -> None:
        self.script = list(script)
        self.cost = cost
        self.session_id = session_id
        self.request: TurnRequest | None = None
        self.calls: list[tuple[str, dict[str, Any], bool]] = []
        """闸门收到的每一次调用：``(工具名, 参数, 是否放行)``。"""

    async def run(self, request: TurnRequest, gate: Gate) -> AsyncIterator[Event]:
        self.request = request
        for step in self.script:
            verb = step[0]
            if verb == "text":
                yield Event(kind=KIND_TEXT, text=step[1])
            elif verb == "sleep":
                await asyncio.sleep(step[1])
            elif verb == "tool":
                _, tool, args = step
                decision = await gate(tool, args)
                self.calls.append((tool, dict(args), decision.allowed))
                # 模型确实发起了调用（所以有这个事件）……
                yield Event(kind=KIND_TOOL_USE, tool=tool, args=args)
                # ……但结果取决于闸门：执行了，还是被拒了。
                #
                # **拒绝理由走 tool_result 的 meta，不走 text 事件。**
                # 它是回灌给模型的反馈（真实 SDK 里就是工具结果），
                # 不是模型说的话——当成 text 会被拼进给用户看的回复里。
                yield Event(
                    kind=KIND_TOOL_RESULT,
                    ok=decision.allowed,
                    meta={} if decision.allowed else {
                        "denied": True,
                        "gate": decision.gate,
                        "reason": decision.reason,
                    },
                )
            elif verb == "done":
                yield Event(
                    kind=KIND_DONE,
                    cost_usd=self.cost,
                    meta={"session_id": self.session_id, "num_turns": 1},
                )
