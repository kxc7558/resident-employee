"""引擎适配器。

运行时核心不认识任何具体引擎，只认识 `..ports.AgentEngine`。
具体引擎的适配器放这里——**每个适配器是本包里唯一 import 那个 SDK 的地方**。

目前只有 `claude_agent_sdk`。它按需导入：没装 SDK 也能用授权/审计部件。
"""

from __future__ import annotations

__all__: list[str] = []
