"""前台配置：模型后端、工具面、步数——**改这里，不改代码**。

规范要求"模型/后端在界面上可切换"。所以它是个可读写的数据对象，
由 `GET/POST /api/config` 操作，落一份 JSON。

**令牌永不回传**：`to_public_dict()` 只说有没有配，不说是多少——
它会进浏览器、进缓存、进截图。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping


# --- 配置 -------------------------------------------------------------------


@dataclass
class AppConfig:
    """前台配置。**改模型后端在这里改，不改代码**（规范要求界面上能切）。"""

    employee_file: str = "examples/employee.example.json"
    base_url: str = ""
    token: str = ""
    model: str = ""
    allowed_tools: tuple[str, ...] = ("Read", "Glob", "Grep", "Bash")
    max_steps: int = 20
    timeout_s: float = 180.0
    resource: str = "employee"
    """授权书里"对什么"那一栏的值。"""

    def to_public_dict(self) -> dict[str, Any]:
        """给前台的版本——**token 只说有没有，不说是多少**。"""
        data = asdict(self)
        data.pop("token", None)
        data["has_token"] = bool(self.token)
        data["allowed_tools"] = list(self.allowed_tools)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AppConfig":
        tools = data.get("allowed_tools")
        return cls(
            employee_file=str(data.get("employee_file", "examples/employee.example.json")),
            base_url=str(data.get("base_url", "")),
            token=str(data.get("token", "")),
            model=str(data.get("model", "")),
            allowed_tools=tuple(str(t) for t in tools) if tools else cls.allowed_tools,
            max_steps=int(data.get("max_steps", 20)),
            timeout_s=float(data.get("timeout_s", 180.0)),
            resource=str(data.get("resource", "employee")),
        )

    @classmethod
    def load(cls, path: str | Path) -> "AppConfig":
        p = Path(path)
        if not p.exists():
            return cls()
        try:
            return cls.from_dict(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            return cls()

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)


