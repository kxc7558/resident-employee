"""起前台服务：``python -m resident_employee.web --data data --port 8765``。

默认**只绑 127.0.0.1**。这是本机工具，不是给公网访问的——
要对外必须先加认证，别改绑 0.0.0.0 了事。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from .server import DEFAULT_CONFIG_FILE, AppConfig, EmployeeApp, serve
from ..runtime.ports import KIND_DONE, KIND_TEXT, KIND_TOOL_RESULT, KIND_TOOL_USE, Event


class TrialEngine:
    """试用引擎：不装模型也能把页面走一遍。

    它的价值是**让前台可被看见**——不用 API key、不花钱、不起进程，
    就能看到事件流、过程可见、被拦、确认执行这一整套。

    它**每次调用都真过闸门**，不是直接吐事件——否则试用时看到的就不是真路径。
    """

    SCRIPT = (
        ("Read", {"file_path": "scripts/example.js"}),
        ("Bash", {"command": "node scripts/example.js status"}),
        ("Write", {"file_path": "config.json", "content": "改点东西"}),
    )

    async def run(self, request, gate):  # noqa: ANN001
        for tool, args in self.SCRIPT:
            decision = await gate(tool, args)
            yield Event(kind=KIND_TOOL_USE, tool=tool, args=args)
            yield Event(
                kind=KIND_TOOL_RESULT,
                ok=decision.allowed,
                meta={} if decision.allowed else {"denied": True, "gate": decision.gate, "reason": decision.reason},
            )
        yield Event(
            kind=KIND_TEXT,
            text=(
                "（试用引擎）我看到你的系统了。\n\n"
                "读文件和执行命令都没问题；但**改配置这个动作我没有授权**，"
                "所以没动。要我做的话，请点下面的「确认执行」。"
            ),
        )
        yield Event(kind=KIND_DONE, cost_usd=0.0, meta={"session_id": request.session or "trial"})


def build_app(args: argparse.Namespace) -> EmployeeApp:
    data_dir = Path(args.data)
    config = AppConfig.load(data_dir / DEFAULT_CONFIG_FILE)
    if args.employee:
        config.employee_file = args.employee

    engine_factory = None
    if args.trial_engine:
        engine_factory = TrialEngine
    elif not args.no_engine:
        try:
            from ..runtime.adapters.claude_agent_sdk import ClaudeAgentSDKEngine

            engine_factory = ClaudeAgentSDKEngine
        except ImportError as exc:
            print(f"注意：没装引擎（{exc}）\n"
                  f"     页面能打开，但发消息会报错。装法：pip install 'resident-employee[engine]'\n"
                  f"     或加 --trial-engine 用试用引擎把页面走一遍。")

    return EmployeeApp(data_dir=data_dir, config=config, engine_factory=engine_factory)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="resident-employee.web", description="数字员工网页前台")
    parser.add_argument("--data", default="web-data", help="数据目录（会话/审计/授权都落这里）")
    parser.add_argument("--host", default="127.0.0.1", help="绑定地址（默认只绑本机）")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--employee", help="员工定义文件（覆盖配置里的）")
    parser.add_argument("--no-engine", action="store_true", help="不起引擎（只看页面/调试用）")
    parser.add_argument("--trial-engine", action="store_true",
                        help="用试用引擎：不装模型也能把页面走一遍（不花钱、不起进程）")
    args = parser.parse_args(argv)

    app = build_app(args)
    if app.config.employee_file and not Path(app.config.employee_file).exists():
        default_employee = Path("examples/employee.example.json")
        if default_employee.exists():
            app.config.employee_file = str(default_employee)
            print(f"提示：配置里的员工定义不存在，已改用 {default_employee}")

    serve(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
