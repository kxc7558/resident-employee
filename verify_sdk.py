"""实测：Agent SDK 能否驱动本机中转站（用完即删）。

两关：
  关一 纯文本 —— SDK 起 CLI、连中转站、拿到回复
  关二 工具调用 —— agent harness 的命门。驱动的是 glm-5.3-flash，
       它扛不住工具调用的话，「常驻员工」这条路就得换。

模型接入从 ~/.claude/settings.json 读（复用本机既有配置，不写死 key）——
这也正是常驻运行时以后要做的：**换模型后端只改配置，不改代码**。
"""

import json
import os
import pathlib

import anyio

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
    UserMessage,
    query,
)


def relay_env() -> dict[str, str]:
    """从 ~/.claude/settings.json 取中转站配置，合进当前进程环境。"""
    cfg = json.loads(
        (pathlib.Path.home() / ".claude" / "settings.json").read_text(encoding="utf-8")
    )
    env = dict(os.environ)
    for key in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
        value = cfg.get("env", {}).get(key)
        if value:
            env[key] = value
    return env


def show(msg) -> None:
    kind = type(msg).__name__
    if isinstance(msg, SystemMessage):
        print(f"  [{kind}] subtype={getattr(msg, 'subtype', '?')}")
    elif isinstance(msg, AssistantMessage):
        for block in msg.content:
            if isinstance(block, TextBlock):
                print(f"  [{kind}] 文本: {block.text[:120]!r}")
            elif isinstance(block, ToolUseBlock):
                print(f"  [{kind}] 工具: {block.name} 入参={block.input}")
            else:
                print(f"  [{kind}] 块: {type(block).__name__}")
    elif isinstance(msg, UserMessage):
        print(f"  [{kind}] 工具结果回灌")
    elif isinstance(msg, ResultMessage):
        print(
            f"  [{kind}] 成功={not msg.is_error} 轮数={msg.num_turns} "
            f"耗时={msg.duration_ms}ms subtype={getattr(msg, 'subtype', None)}"
        )
        if msg.is_error:
            print(f"      错误: {getattr(msg, 'result', None)}")
    else:
        print(f"  [{kind}]")


async def main() -> None:
    env = relay_env()
    print(f"中转站: {env.get('ANTHROPIC_BASE_URL')}（token 长度 {len(env.get('ANTHROPIC_AUTH_TOKEN', ''))}）")

    print()
    print("=" * 66)
    print("关一：纯文本（不给工具）")
    print("=" * 66)
    opts = ClaudeAgentOptions(
        max_turns=2,
        allowed_tools=[],
        env=env,
        system_prompt="你是一个测试用的助手，只回答被问的内容，不要多说。",
    )
    try:
        async for msg in query(prompt="Reply with exactly: OK", options=opts):
            show(msg)
    except Exception as exc:
        print(f"  ✗ 失败：{type(exc).__name__}: {str(exc)[:300]}")
        return

    print()
    print("=" * 66)
    print("关二：工具调用（只给 Read，读一个已知存在的小文件）")
    print("=" * 66)
    opts2 = ClaudeAgentOptions(
        max_turns=4,
        allowed_tools=["Read"],
        permission_mode="bypassPermissions",
        env=env,
        system_prompt="你要完成用户交给的任务，需要看文件就用 Read 工具。",
    )
    try:
        async for msg in query(
            prompt="用 Read 工具读一下 _probe_sdk.py 的前 3 行，然后原样复述出来。",
            options=opts2,
        ):
            show(msg)
    except Exception as exc:
        print(f"  ✗ 失败：{type(exc).__name__}: {str(exc)[:300]}")


if __name__ == "__main__":
    anyio.run(main)
