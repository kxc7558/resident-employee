"""网页前台：把员工摆到人面前。

规范里那条「员工不能隐形」——员工只活在配置文件和命令行里就等于没有。
前台提供三样，缺一不算交付：

| 要什么 | 在哪 |
|---|---|
| **对话界面** | `static/index.html`（单文件、内联、零外部依赖） |
| **过程可见** | 事件流经 NDJSON 实时推到页面：调了什么、被拦了什么，当场显示 |
| **配置页** | `GET/POST /api/config`——模型、后端、工具面、步数都在界面上改，不改文件 |

外加**人在环**：「签授权书」和「点确认执行」是同一个动作（见 `approvals.py`）。

服务端**只用标准库**，所以前台是零额外依赖的。

起服务::

    python -m resident_employee.web --data data --port 8765

然后开 http://127.0.0.1:8765/。
"""

from .approvals import (
    CONFIRMABLE_GATE,
    ApprovalBook,
    blocked_hard,
    confirmable_actions,
)
from .sessions import (
    ROLE_EMPLOYEE,
    ROLE_USER,
    Message,
    Session,
    SessionNotFound,
    SessionStore,
    Step,
)
from .server import AppConfig, EmployeeApp, make_handler, serve

__all__ = [
    "CONFIRMABLE_GATE",
    "ROLE_EMPLOYEE",
    "ROLE_USER",
    "AppConfig",
    "ApprovalBook",
    "EmployeeApp",
    "Message",
    "Session",
    "SessionNotFound",
    "SessionStore",
    "Step",
    "blocked_hard",
    "confirmable_actions",
    "make_handler",
    "serve",
]
