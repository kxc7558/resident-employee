"""前台的应用对象：员工、会话、审计、授权、配置，都在这里。

**引擎是注入的**（`engine_factory`）——测试时换成假引擎，整套前台行为都能验，
不起进程、不花钱、不依赖网络。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ..authz import AuditChain, GrantStore
from ..runtime import Employee, EmployeeRunner, build_default_chain
from .approvals import ApprovalBook, blocked_hard, confirmable_actions
from .config import AppConfig
from .heartbeat import HEARTBEAT_FILE, Heartbeat, check_health
from .sessions import ROLE_EMPLOYEE, Session, SessionStore


# --- 应用 -------------------------------------------------------------------


@dataclass
class EmployeeApp:
    """前台的中央对象：员工、会话、审计、授权、配置，都在这里。"""

    data_dir: Path
    config: AppConfig = field(default_factory=AppConfig)
    engine_factory: Any = None
    """``() -> AgentEngine``。注入是为了**测试时换成假引擎**——不起进程、不花钱。"""

    def __post_init__(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.audit = AuditChain(self.data_dir / "audit.jsonl")
        self.grants = GrantStore(self.data_dir / "grants.json", self.audit)
        self.sessions = SessionStore(self.data_dir / "sessions.json")
        self.approvals = ApprovalBook(
            self.grants, self.audit, resource=self.config.resource
        )
        self.heartbeat = Heartbeat(self.data_dir / HEARTBEAT_FILE)
        self._lock = threading.Lock()

    # --- 员工与运行器 -----------------------------------------------------

    def employee(self) -> Employee:
        path = Path(self.config.employee_file)
        if not path.exists():
            raise FileNotFoundError(
                f"员工定义不在：{path}。请在配置页里指到 examples/employee.example.json，"
                f"或写一份自己的六段式定义。"
            )
        return Employee.from_file(path).validate(require_all=False)

    def build_runner(self) -> EmployeeRunner:
        if self.engine_factory is None:  # pragma: no cover - 生产路径
            raise RuntimeError("没有装引擎。请 pip install 'resident-employee[engine]'")
        employee = self.employee()
        tools = self.config.allowed_tools

        chain = build_default_chain(
            allowed_tools=tools,
            max_repeats=3,
            decide=self._decide,
        )
        return EmployeeRunner(
            employee,
            self.engine_factory(),
            chain=chain,
            audit=self.audit,
            allowed_tools=tools,
            max_steps=self.config.max_steps,
            timeout_s=self.config.timeout_s,
            actor=employee.name,
        )

    async def _decide(self, action: str, _args: Mapping[str, Any]):
        """闸门的授权判定——**授权码在服务端，模型拿不到**。"""
        return self.approvals.decide(action, self.config.resource)

    # --- 状态 -------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        chain_ok, chain_note = self.audit.verify()
        health = check_health(self.data_dir / HEARTBEAT_FILE)
        try:
            employee_name = self.employee().name
        except FileNotFoundError:
            employee_name = ""
        return {
            "employee": employee_name,
            "engine": "已装" if self.engine_factory else "未装",
            "sessions": len(self.sessions.list()),
            "audit_entries": len(self.audit),
            "chain_ok": chain_ok,
            "chain_note": chain_note,
            "alive": health.alive,
            "uptime_s": round(health.uptime_s, 1),
            "config": self.config.to_public_dict(),
        }


def step_from_dict(raw: Mapping[str, Any]):
    from .sessions import Step

    return Step.from_dict(raw)


def confirmable_in_session(session) -> list[str]:
    """会话**最近一轮**里可确认执行的动作。

    **从记录里推，不从内存里取**——这样刷新页面、甚至重启服务之后，
    「确认执行」按钮还在（只要会话记录还在）。早先版本存在内存里，
    一刷新按钮就没了，"被拦"却还留在记录里，用户只能干瞪眼。
    """
    for message in reversed(session.messages):
        if message.role != ROLE_EMPLOYEE:
            continue
        return confirmable_actions(step.to_dict() for step in message.steps)
    return []


