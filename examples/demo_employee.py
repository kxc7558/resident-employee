"""跑一个常驻员工的一轮对话——**重点看「过程可见」和「越权被拦」**。

    python examples/demo_employee.py --fake          # 假引擎，离线可跑，不花钱
    python examples/demo_employee.py "看看系统健康"   # 真引擎（需要 SDK + 可用后端）

两种模式：

- `--fake` 用一个内置的假引擎，**不联网、不起进程、不花钱**。看的是运行时
  自己的行为：闸门、循环检测、事件流、审计链。
- 不带 `--fake` 走真的 Claude Agent SDK ——**会真起一个 agent 进程、真花 token**。
  所以默认把工具面收成只有 `Read`：演示不至于真动到什么东西。

这跟 `examples/demo.py`（纯本地库演示，永远不花钱）不同，所以分成两个文件。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from resident_employee.authz import AuditChain, Grant, GrantStore, in_hours, now_utc  # noqa: E402
from resident_employee.authz import authorize  # noqa: E402
from resident_employee.runtime import (  # noqa: E402
    KIND_TEXT,
    KIND_THINKING,
    KIND_TOOL_RESULT,
    KIND_TOOL_USE,
    Employee,
    EmployeeRunner,
    Event,
    build_default_chain,
)

EMPLOYEE_FILE = Path(__file__).resolve().parent / "employee.example.json"


def show(event: Event) -> None:
    """过程可见：每一个事件都当场打出来，不等跑完。"""
    marker = {
        KIND_TEXT: "💬",
        KIND_THINKING: "🤔",
        KIND_TOOL_USE: "🔧",
        KIND_TOOL_RESULT: "📄",
    }.get(event.kind, "•")
    if event.kind == KIND_TOOL_USE:
        print(f"  {marker} 调用 {event.tool}  {dict(event.args)}")
    elif event.kind == KIND_TEXT:
        print(f"  {marker} {event.text[:100]}")
    elif event.kind == KIND_THINKING:
        print(f"  {marker} （思考）{event.text[:60]}")
    elif event.kind == KIND_TOOL_RESULT and event.meta.get("denied"):
        # 拒绝理由在这儿——它是回灌给模型的反馈，所以过程视图要显示，但不进回复
        print(f"  ⛔ 被拦（{event.meta.get('gate', '')}）：{event.meta.get('reason', '')[:80]}")
    else:
        print(f"  {marker} {event.brief()}")


class ScriptedEngine:
    """内置假引擎：演一遍「正常读 → 越权写被拦 → 汇报」。

    它**每次都真过一遍闸门**，不是绕过闸门直接吐事件——否则演示的就不是真路径。
    """

    async def run(self, request, gate):
        for tool, args in (
            ("Read", {"file_path": "scripts/key-pool.js"}),
            ("Bash", {"command": "node scripts/key-pool.js status"}),
            ("Write", {"file_path": "config.json", "content": "改点东西"}),
        ):
            decision = await gate(tool, args)
            yield Event(kind=KIND_TOOL_USE, tool=tool, args=args)
            # 拒绝理由走 tool_result 的 meta（真实 SDK 里它就是工具结果），
            # 不走 text —— 走了会被拼进给用户看的回复里
            yield Event(
                kind=KIND_TOOL_RESULT,
                ok=decision.allowed,
                meta={} if decision.allowed else {
                    "denied": True,
                    "gate": decision.gate,
                    "reason": decision.reason,
                },
            )
        yield Event(kind=KIND_TEXT, text="代理池 29 个 Key 全可用，延迟 1.5s。我没动任何配置。")
        yield Event(
            kind="done",
            cost_usd=0.0042,
            meta={"session_id": "demo-session", "num_turns": 3},
        )


async def main() -> int:
    use_fake = "--fake" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    prompt = args[0] if args else "看一下代理池的健康状态"

    employee = Employee.from_file(EMPLOYEE_FILE).validate()
    print(f"员工：{employee.name}")
    print(f"系统提示长度：{len(employee.to_system_prompt())} 字")
    print(f"引擎：{'内置假引擎（离线）' if use_fake else 'Claude Agent SDK（真起进程）'}")
    print("=" * 70)

    tmp = Path(tempfile.mkdtemp(prefix="resident-employee-demo-"))
    audit = AuditChain(tmp / "audit.jsonl")
    store = GrantStore(tmp / "grants.json", audit)

    # --- 演示「授权闸门」：签一张只允许读的授权 ---------------------------------
    record, code = store.issue(
        Grant(
            issuer="张三",
            subject=employee.name,
            actions=("Bash",),          # 只给命令执行，不给 Write
            resources=("api-key-pool",),
            issued_at=now_utc(),
            expires_at=in_hours(1),
        )
    )

    async def decide(action: str, _args) -> object:
        """执行器侧：授权码**握在这里**，模型拿不到。"""
        return authorize(
            store,
            grant_id=record.grant.grant_id,
            action=action,
            resource="api-key-pool",
            code=code,
        )

    tools = ("Read",) if not use_fake else ("Read", "Bash", "Write")
    chain = build_default_chain(allowed_tools=tools, max_repeats=3, decide=decide)

    engine = ScriptedEngine() if use_fake else _real_engine()
    runner = EmployeeRunner(
        employee,
        engine,
        chain=chain,
        audit=audit,
        allowed_tools=tools,
        max_steps=6,
        timeout_s=120,
    )

    print(f"提问：{prompt}")
    print("-" * 70)
    result = await runner.run_turn(prompt, session="demo", on_event=_print_event)

    print("-" * 70)
    print(f"回复：{result.text}")
    print(f"交代：{result.brief()}")
    if result.denials:
        print("\n被拦下的动作：")
        for tool, decision in result.denials:
            print(f"  ✗ {tool}（{decision.gate}）：{decision.reason[:70]}")

    print("\n审计链：")
    ok, note = audit.verify()
    print(f"  {'完整 ✓' if ok else '已断 ✗'} —— {note}")
    for entry in audit:
        print(f"    seq={entry.seq} {entry.event:12} {str(entry.detail.get('tool', '')):8} "
              f"{str(entry.detail.get('reason', ''))[:40]}")
    return 0


async def _print_event(event: Event) -> None:
    show(event)


def _real_engine():
    from resident_employee.runtime.adapters.claude_agent_sdk import ClaudeAgentSDKEngine

    return ClaudeAgentSDKEngine()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
