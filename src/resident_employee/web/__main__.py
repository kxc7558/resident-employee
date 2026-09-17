"""起前台服务。

```bash
python -m resident_employee.web                          # 起服务
python -m resident_employee.web --trial-engine            # 试用引擎（不装模型也能看页面）
python -m resident_employee.web --check                   # 一条命令查在岗状态
python -m resident_employee.web --write-boot              # 生成开机自启脚本
```

默认**只绑 127.0.0.1**。对话台是**嵌在业务系统内部**的，认证归宿主系统——
要对外请设 `host_token`，而不是改绑 `0.0.0.0`（见 `auth.py`）。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from ..runtime.ports import KIND_DONE, KIND_TEXT, KIND_TOOL_RESULT, KIND_TOOL_USE, Event
from .app import EmployeeApp
from .config import AppConfig
from .heartbeat import HEARTBEAT_FILE, check_health
from .server import DEFAULT_CONFIG_FILE, serve

BAT_DEFAULT = "启动数字员工.bat"
SYSTEMD_DEFAULT = "resident-employee.service"


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
                meta={} if decision.allowed else {
                    "denied": True,
                    "gate": decision.gate,
                    "reason": decision.reason,
                },
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


# --- 一条命令查健康 ---------------------------------------------------------


def report_health(data_dir: Path) -> int:
    """读状态文件判在岗。**不起 agent、不连服务**——一条命令的事不做成两条。"""
    health = check_health(data_dir / HEARTBEAT_FILE)
    print(health.brief())
    if not health.alive:
        print(f"（状态文件：{data_dir / HEARTBEAT_FILE}）")
        return 1
    return 0


# --- 生成自启脚本 -----------------------------------------------------------


def bat_script(*, data_dir: str, port: int, python: str) -> bytes:
    """Windows 启动脚本。

    **GBK(936) + CRLF**——这不是洁癖：.bat 用 UTF-8 存，cmd 会按 GBK 解，
    中文变乱码、甚至报"不是内部或外部命令"（首字节被吃掉）。踩过。
    """
    text = (
        "@echo off\r\n"
        "chcp 936 >nul\r\n"
        "title 数字员工\r\n"
        "cd /d \"%~dp0\"\r\n"
        "echo 正在启动数字员工（这个窗口关掉就停了）\r\n"
        f"\"{python}\" -m resident_employee.web --data \"{data_dir}\" --port {port}\r\n"
        "echo.\r\n"
        "echo 已停止。按任意键关闭。\r\n"
        "pause >nul\r\n"
    )
    return text.encode("gbk", errors="replace")


def systemd_unit(*, data_dir: str, host: str, port: int, python: str, workdir: str) -> str:
    """Linux systemd 单元。"""
    return (
        "[Unit]\n"
        "Description=数字员工（resident-employee 对话台）\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={workdir}\n"
        f"ExecStart={python} -m resident_employee.web --data {data_dir} "
        f"--host {host} --port {port}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def write_boot(args: argparse.Namespace, *, target: str) -> int:
    data_dir = str(Path(args.data).resolve())
    python = sys.executable or "python"
    workdir = str(Path.cwd())

    if target == "windows":
        out = Path(args.out or BAT_DEFAULT)
        out.write_bytes(bat_script(data_dir=data_dir, port=args.port, python=python))
        print(f"已生成：{out}（GBK + CRLF，双击即跑）")
        print("要开机自启：Win+R → shell:startup → 把这个文件（或它的快捷方式）拖进去")
        return 0

    out = Path(args.out or SYSTEMD_DEFAULT)
    out.write_text(
        systemd_unit(
            data_dir=data_dir, host=args.host, port=args.port, python=python, workdir=workdir
        ),
        encoding="utf-8",
    )
    print(f"已生成：{out}")
    print("安装：")
    print(f"  sudo cp {out} /etc/systemd/system/")
    print("  sudo systemctl daemon-reload && sudo systemctl enable --now resident-employee")
    print("查状态：systemctl status resident-employee")
    return 0


# --- 入口 -------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="resident-employee.web", description="数字员工对话台")
    parser.add_argument("--data", default="web-data", help="数据目录（会话/审计/授权/心跳都落这里）")
    parser.add_argument("--host", default="127.0.0.1", help="绑定地址（默认只绑本机）")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--employee", help="员工定义文件（覆盖配置里的）")
    parser.add_argument("--no-engine", action="store_true", help="不起引擎（只看页面/调试用）")
    parser.add_argument("--trial-engine", action="store_true",
                        help="用试用引擎：不装模型也能把页面走一遍（不花钱、不起进程）")
    parser.add_argument("--check", action="store_true", help="查在岗状态后退出（不起服务）")
    parser.add_argument("--write-boot", action="store_true", help="生成开机自启脚本后退出")
    parser.add_argument("--boot-target", choices=("auto", "windows", "systemd"), default="auto")
    parser.add_argument("--out", help="--write-boot 的输出路径")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.check:
        return report_health(Path(args.data))

    if args.write_boot:
        target = args.boot_target
        if target == "auto":
            target = "windows" if os.name == "nt" else "systemd"
        return write_boot(args, target=target)

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
