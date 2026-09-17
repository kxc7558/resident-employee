"""Claude Agent SDK 的适配器。

**这是运行时里唯一 import `claude_agent_sdk` 的地方。** 换引擎只改这个文件。

映射关系：

| 运行时 | Claude Agent SDK |
|---|---|
| `Gate`（每次调用前） | `can_use_tool`（拒绝时 `message` 回灌给模型） |
| `TurnRequest.max_steps` | `max_turns` |
| `TurnRequest.max_cost_usd` | `max_budget_usd` |
| `Event` | `AssistantMessage` / `UserMessage` / `ResultMessage` 翻译而来 |

字段名都是**实测确认过的**（`claude-agent-sdk` 0.2.154），不是照着记忆写的：

- `PermissionResultAllow(behavior, updated_input, updated_permissions)`
- `PermissionResultDeny(behavior, message, interrupt)`
- `TextBlock(text)` / `ToolUseBlock(id, name, input)` / `ThinkingBlock(thinking, signature)`
- `ResultMessage.session_id / total_cost_usd / is_error / num_turns / result`
"""

from __future__ import annotations

import json
import os
import pathlib
from typing import Any, AsyncIterator, Mapping

from ..ports import (
    KIND_DONE,
    KIND_ERROR,
    KIND_TEXT,
    KIND_THINKING,
    KIND_TOOL_RESULT,
    KIND_TOOL_USE,
    Event,
    Gate,
    TurnRequest,
)

try:  # pragma: no cover - 分支取决于环境
    from claude_agent_sdk import (  # type: ignore[import-not-found]
        AssistantMessage,
        ClaudeAgentOptions,
        PermissionResultAllow,
        PermissionResultDeny,
        ResultMessage,
        TextBlock,
        ThinkingBlock,
        ToolUseBlock,
        UserMessage,
        query,
    )

    HAVE_SDK = True
except ImportError:  # pragma: no cover
    HAVE_SDK = False

SDK_HINT = (
    "常驻运行时需要 Claude Agent SDK：pip install 'resident-employee[engine]'"
    "（即 claude-agent-sdk）。只用授权/审计部件不需要它。"
)


def relay_env_from_settings(settings_path: str | pathlib.Path | None = None) -> dict[str, str]:
    """从 `~/.claude/settings.json` 读模型后端配置，合进当前进程环境。

    **复用本机既有配置，不写死 key**——这正是运行时该做的：
    换模型后端只改配置，不改代码。
    """
    path = pathlib.Path(settings_path) if settings_path else pathlib.Path.home() / ".claude" / "settings.json"
    env = dict(os.environ)
    if not path.exists():
        return env
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return env
    for key in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
        value = cfg.get("env", {}).get(key)
        if value:
            env[key] = value
    return env


class ClaudeAgentSDKEngine:
    """用 Claude Agent SDK 当引擎。"""

    def __init__(
        self,
        *,
        env: Mapping[str, str] | None = None,
        model: str | None = None,
        permission_mode: str | None = None,
        cwd: str | None = None,
    ) -> None:
        if not HAVE_SDK:  # pragma: no cover - 取决于环境
            raise ImportError(SDK_HINT)
        self.env = dict(env) if env is not None else relay_env_from_settings()
        self.model = model
        self.permission_mode = permission_mode
        self.cwd = cwd

    # --- 闸门 -------------------------------------------------------------

    @staticmethod
    def _wrap_gate(gate: Gate):
        """把运行时的 `Gate` 包成 SDK 的 `can_use_tool`。

        拒绝时用 `PermissionResultDeny.message` 把理由**回灌给模型**，
        不要 `interrupt=True`——那会整轮终止，模型没机会自我纠正。
        """

        async def can_use_tool(tool_name: str, tool_input: dict[str, Any], _context: Any):
            decision = await gate(tool_name, tool_input or {})
            if decision.allowed:
                return PermissionResultAllow(
                    updated_input=dict(decision.rewritten) if decision.rewritten else None
                )
            return PermissionResultDeny(message=decision.reason or "该动作未获授权")

        return can_use_tool

    # --- 跑一轮 -----------------------------------------------------------

    async def run(self, request: TurnRequest, gate: Gate) -> AsyncIterator[Event]:
        options = ClaudeAgentOptions(
            system_prompt=request.system_prompt or None,
            allowed_tools=list(request.allowed_tools),
            max_turns=request.max_steps,
            max_budget_usd=request.max_cost_usd,
            can_use_tool=self._wrap_gate(gate),
            env=dict(self.env),
            cwd=request.cwd or self.cwd or None,
        )
        if self.model:
            options.model = self.model
        if self.permission_mode:
            options.permission_mode = self.permission_mode
        if request.resume:
            options.resume = request.resume

        async for message in query(prompt=request.prompt, options=options):
            for event in self._translate(message):
                yield event

    # --- 翻译 -------------------------------------------------------------

    def _translate(self, message: Any) -> list[Event]:
        if isinstance(message, AssistantMessage):
            return self._from_assistant(message)
        if isinstance(message, UserMessage):
            return self._from_user(message)
        if isinstance(message, ResultMessage):
            return self._from_result(message)
        return []

    @staticmethod
    def _from_assistant(message: Any) -> list[Event]:
        out: list[Event] = []
        for block in message.content or []:
            if isinstance(block, TextBlock):
                out.append(Event(kind=KIND_TEXT, text=block.text))
            elif isinstance(block, ThinkingBlock):
                out.append(
                    Event(kind=KIND_THINKING, text=block.thinking, meta={"thinking": True})
                )
            elif isinstance(block, ToolUseBlock):
                # **「过程可见」靠的就是这个事件**——前台据此显示"正在干什么"
                out.append(
                    Event(kind=KIND_TOOL_USE, tool=block.name, args=dict(block.input or {}))
                )
        return out

    @staticmethod
    def _from_user(message: Any) -> list[Event]:
        ok = True
        detail = message.tool_use_result
        if isinstance(detail, Mapping) and detail.get("is_error"):
            ok = False
        return [Event(kind=KIND_TOOL_RESULT, ok=ok, meta={"result": detail} if detail else {})]

    @staticmethod
    def _from_result(message: Any) -> list[Event]:
        cost = float(message.total_cost_usd or 0.0)
        meta = {
            "session_id": getattr(message, "session_id", "") or "",
            "num_turns": getattr(message, "num_turns", 0),
            "subtype": getattr(message, "subtype", ""),
            "duration_ms": getattr(message, "duration_ms", 0),
        }
        if message.is_error:
            return [
                Event(
                    kind=KIND_ERROR,
                    text=str(getattr(message, "result", "") or "执行出错"),
                    ok=False,
                    cost_usd=cost,
                    meta=meta,
                )
            ]
        return [
            Event(
                kind=KIND_DONE,
                text=str(getattr(message, "result", "") or ""),
                cost_usd=cost,
                meta=meta,
            )
        ]
