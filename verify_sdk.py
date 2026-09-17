"""实测：Agent SDK 能否驱动本机中转站。

**两关，看最后一行就够了**（每关打印「通过 / 未通过」）：

| 关 | 测什么 | 为什么重要 |
|---|---|---|
| 一 | 纯文本：SDK 起 CLI、连中转站、拿到回复 | 通路是否成立 |
| 二 | **工具调用**：让它真去 Read 一个文件 | **agent harness 的命门** —— 驱动的是 `glm-5.3-flash`，扛不住工具调用的话，「常驻员工」这条路就得换 |

用法（PowerShell 5.1 不认 `&&`，用 `;`）::

    cd d:/resident-employee; python verify_sdk.py

模型接入从 `~/.claude/settings.json` 读（复用本机既有配置，不写死 key）——
这也正是常驻运行时以后要做的：**换模型后端只改配置，不改代码**。
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
from dataclasses import dataclass, field

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

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# 关二拿这个文件当靶子。**必须是仓库里稳定存在的文件**——
# 曾用过 `_probe_sdk.py`（临时探针），改名时把它删了，导致这一关
# 报「文件不存在」而不是真的去读，命门那一关会给出假阴性。
TARGET_FILE = "pyproject.toml"
EXPECTED_IN_TARGET = "resident-employee"


@dataclass
class Observation:
    """一轮跑下来观察到什么。"""

    texts: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def all_text(self) -> str:
        return "\n".join(self.texts)


def relay_env() -> dict[str, str]:
    """从 ~/.claude/settings.json 取中转站配置，合进当前进程环境。"""
    settings = pathlib.Path.home() / ".claude" / "settings.json"
    if not settings.exists():
        raise SystemExit(f"找不到 {settings}；请手动设置 ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN")
    cfg = json.loads(settings.read_text(encoding="utf-8"))
    env = dict(os.environ)
    for key in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
        value = cfg.get("env", {}).get(key)
        if value:
            env[key] = value
    return env


def observe(msg, seen: Observation) -> None:
    kind = type(msg).__name__
    if isinstance(msg, SystemMessage):
        print(f"  [{kind}] subtype={getattr(msg, 'subtype', '?')}")
    elif isinstance(msg, AssistantMessage):
        for block in msg.content:
            if isinstance(block, TextBlock):
                seen.texts.append(block.text)
                print(f"  [{kind}] 文本: {block.text[:120]!r}")
            elif isinstance(block, ToolUseBlock):
                seen.tools.append(block.name)
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
            detail = getattr(msg, "result", None)
            seen.errors.append(str(detail))
            print(f"      错误: {detail}")
    else:
        print(f"  [{kind}]")


async def run_turn(prompt: str, options: ClaudeAgentOptions) -> Observation:
    seen = Observation()
    try:
        async for msg in query(prompt=prompt, options=options):
            observe(msg, seen)
    except Exception as exc:
        seen.errors.append(f"{type(exc).__name__}: {exc}")
        print(f"  ✗ 异常：{type(exc).__name__}: {str(exc)[:300]}")
    return seen


async def main() -> int:
    env = relay_env()
    print(f"中转站: {env.get('ANTHROPIC_BASE_URL')}（token 长度 {len(env.get('ANTHROPIC_AUTH_TOKEN', ''))}）")

    if not pathlib.Path(TARGET_FILE).exists():
        print(f"✗ 关二的靶子文件不存在：{TARGET_FILE}（请在仓库根目录运行）")
        return 1

    verdicts: list[tuple[str, bool, str]] = []

    # --- 关一：纯文本 ----------------------------------------------------
    print("\n" + "=" * 66)
    print("关一：纯文本（不给工具）")
    print("=" * 66)
    first = await run_turn(
        "Reply with exactly: OK",
        ClaudeAgentOptions(
            max_turns=2,
            allowed_tools=[],
            env=env,
            system_prompt="你是一个测试用的助手，只回答被问的内容，不要多说。",
        ),
    )
    ok1 = "OK" in first.all_text
    verdicts.append(("关一 纯文本通路", ok1, "回复里含 OK" if ok1 else f"没拿到预期回复：{first.all_text[:80]!r}"))

    # --- 关二：工具调用（命门）-------------------------------------------
    print("\n" + "=" * 66)
    print(f"关二：工具调用（只给 Read，让它去读 {TARGET_FILE}）")
    print("=" * 66)
    second = await run_turn(
        f"用 Read 工具读一下项目根目录下的 {TARGET_FILE}，"
        f"然后把其中 name 字段的值原样回复给我，不要多说别的。",
        ClaudeAgentOptions(
            max_turns=4,
            allowed_tools=["Read"],
            permission_mode="bypassPermissions",
            env=env,
            system_prompt="你要完成用户交给的任务，需要看文件就用 Read 工具，不要凭记忆猜。",
        ),
    )
    used_read = "Read" in second.tools
    got_answer = EXPECTED_IN_TARGET in second.all_text
    ok2 = used_read and got_answer
    reason = (
        f"调了 Read 且答出 {EXPECTED_IN_TARGET!r}"
        if ok2
        else f"调了 Read={used_read}，答对={got_answer}"
    )
    verdicts.append(("关二 工具调用", ok2, reason))

    # --- 结论 -------------------------------------------------------------
    print("\n" + "=" * 66)
    print("结论（看这几行）")
    print("=" * 66)
    for name, ok, note in verdicts:
        print(f"  {'✓ 通过' if ok else '✗ 未通过'}  {name:<16} {note}")

    all_ok = all(ok for _, ok, _ in verdicts)
    if all_ok:
        print("\n两关都过 → Agent SDK 可以驱动本机中转站，常驻运行时这条路成立。")
    elif verdicts[0][1] and not verdicts[1][1]:
        print(
            "\n通路成立但工具调用不过 → 模型扛不住 agent 用法。"
            "换一个工具调用能力强的模型（改 settings.json 的模型映射），或在运行时里"
            "把能力都包成确定性工具、少让模型自主决策。"
        )
    else:
        print("\n通路就没通 → 先查中转站是否在跑、token 是否有效。")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(anyio.run(main))
