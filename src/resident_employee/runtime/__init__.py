"""常驻运行时：把「专属员工」跑起来。

一个员工 = **常驻进程 + 专属系统提示 + 自己的 CLI（手）+ 跨会话记忆**。

这一层的分工：

| 文件 | 管什么 |
|---|---|
| `ports.py` | 与引擎的接口（运行时**不认识任何具体 SDK**） |
| `employee.py` | 员工定义（六段式）→ 系统提示 |
| `gates.py` | **每次工具调用前**的闸门（工具面/参数/循环/授权） |
| `runner.py` | 主循环 + **整轮**闸门（步数/花费/超时）+ 审计 |
| `adapters/` | 具体引擎（Claude Agent SDK）——唯一 import 它的地方 |

**为什么引擎要搁在接口后面**：运行时自己的逻辑（闸门、循环检测、成本、审计、
事件流）必须能脱离"真起一个 agent 进程"来测。否则每次跑测试都要花真钱、
真起进程、还依赖网络。顺带，换引擎不用重写运行时。

典型用法::

    from resident_employee.runtime import Employee, EmployeeRunner
    from resident_employee.runtime.adapters.claude_agent_sdk import ClaudeAgentSDKEngine

    employee = Employee.from_file("employee.json").validate()
    engine = ClaudeAgentSDKEngine()
    runner = EmployeeRunner(employee, engine, allowed_tools=("Read", "Bash"), max_steps=20)

    result = await runner.run_turn("看一下系统健康状态")
    print(result.text, result.brief())
"""

from .employee import SECTIONS, Employee, EmployeeError
from .gates import (
    DEFAULT_FREE_TOOLS,
    AuthorizationGate,
    BaseGate,
    GateChain,
    LoopBreakerGate,
    ToolSchemaGate,
    ToolScopeGate,
    build_default_chain,
    fingerprint,
)
from .ports import (
    KIND_DONE,
    KIND_ERROR,
    KIND_TEXT,
    KIND_THINKING,
    KIND_TOOL_RESULT,
    KIND_TOOL_USE,
    AgentEngine,
    Event,
    Gate,
    GateDecision,
    TurnRequest,
)
from .runner import (
    STOP_COST,
    STOP_DONE,
    STOP_ERROR,
    STOP_LABELS,
    STOP_MAX_STEPS,
    STOP_TIMEOUT,
    EmployeeRunner,
    TurnResult,
)

__all__ = [
    "DEFAULT_FREE_TOOLS",
    "KIND_DONE",
    "KIND_ERROR",
    "KIND_TEXT",
    "KIND_THINKING",
    "KIND_TOOL_RESULT",
    "KIND_TOOL_USE",
    "SECTIONS",
    "STOP_COST",
    "STOP_DONE",
    "STOP_ERROR",
    "STOP_LABELS",
    "STOP_MAX_STEPS",
    "STOP_TIMEOUT",
    "AgentEngine",
    "AuthorizationGate",
    "BaseGate",
    "Employee",
    "EmployeeError",
    "EmployeeRunner",
    "Event",
    "Gate",
    "GateChain",
    "GateDecision",
    "LoopBreakerGate",
    "ToolSchemaGate",
    "ToolScopeGate",
    "TurnRequest",
    "TurnResult",
    "build_default_chain",
    "fingerprint",
]
