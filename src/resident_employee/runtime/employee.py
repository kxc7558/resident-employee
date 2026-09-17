"""员工定义：六段式 → 系统提示。

规范 `digital-employee.md` 要求每个数字员工写清六段，缺一段员工就会瞎干：

| 段 | 作用 | 缺了会怎样 |
|---|---|---|
| ① 我管的系统 | 地址/位置/部署方式/凭证在哪 | 员工找不到系统 |
| ② 常用动作 | 直接可执行的命令 | 员工每次现写代码 |
| ③ 权限边界 | 哪些直接做、哪些要问人 | 员工要么不敢做、要么乱做 |
| ④ 这个系统的坑 | 踩过的坑（越具体越好） | 重复踩坑 |
| ⑤ 排障顺序 | 用户说"不对了"时的固定流程 | 员工瞎猜 |
| ⑥ 输出要求 | 干完怎么汇报 | 长篇废话 |

**外加一段不由员工自定的「通用铁律」**——它跟具体系统无关，写死在运行时里，
免得每个员工定义都要抄一遍、抄漏一处就出事。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

# 六段的字段名（顺序即规范里的顺序）
SECTIONS: tuple[tuple[str, str], ...] = (
    ("system", "① 我管的系统"),
    ("actions", "② 常用动作"),
    ("boundaries", "③ 权限边界"),
    ("pitfalls", "④ 这个系统的坑"),
    ("triage", "⑤ 排障顺序"),
    ("output_format", "⑥ 输出要求"),
)

HARD_RULES = """## 通用铁律（不由你决定，必须遵守）

1. **不猜接口**——先读代码/文档，或直接试调。猜出来的命令一定是错的。
2. **做完必须验证**——跑一次 `test`/`status` 之类的自证命令确认生效，
   **不能只看"保存成功"**。
3. **别人的业务数据只读**——不写入、不修改、不删除他人数据，一个字都不改。
4. **高风险操作要人确认**——删除、改凭证、清数据这类，先停下来说清楚，
   等人类确认再做。
5. **查不到就说查不到**——禁止用常识补全。编出看起来很像的数据是最严重的问题。
6. **你没有凭据，也无法给自己开权限**——你的每个动作都会被执行器校验。
   被拒时按给出的理由改写，**不要试图绕过**（换个说法、拆成几步、改用别的工具
   都属于绕过）。
7. **输出简短、具体、可核对**——按第⑥段要求的格式说，不要长篇技术解释，
   也不要罗列尝试过的失败路径（除非用户问）。
"""


class EmployeeError(ValueError):
    """员工定义不合格——六段缺失。"""


@dataclass(frozen=True)
class Employee:
    """一个专属数字员工。"""

    name: str
    system: str = ""
    actions: str = ""
    boundaries: str = ""
    pitfalls: str = ""
    triage: str = ""
    output_format: str = ""
    persona: str = ""
    """一句话身份定位（可选）。不填就按 name 生成。"""

    extra: Mapping[str, str] = field(default_factory=dict)
    """额外补充，会拼在六段之后。给项目特有的约定用。"""

    def validate(self, *, require_all: bool = True) -> "Employee":
        """六段齐全才能上岗。缺一段就抛错——**不静默通过**。

        `require_all=False` 时只要求 `name`，给写测试或做半成品用。
        """
        if not self.name.strip():
            raise EmployeeError("员工必须有名字")

        if require_all:
            missing = [label for key, label in SECTIONS if not getattr(self, key).strip()]
            if missing:
                raise EmployeeError(
                    "员工定义缺这几段：" + "、".join(missing)
                    + "（六段缺一段，员工就会瞎干）"
                )
        return self

    def to_system_prompt(self) -> str:
        """组装系统提示。这是员工「上任时带着的东西」。"""
        lines: list[str] = []
        persona = self.persona.strip() or f"你是「{self.name}」——这个系统的专属数字员工。"
        lines.append(persona)
        lines.append("")
        lines.append("你**只管这一个系统**。不用去了解别的系统，也不要去猜它们。")

        for key, label in SECTIONS:
            body = getattr(self, key).strip()
            if body:
                lines.append("")
                lines.append(f"## {label}")
                lines.append(body)

        for key, body in self.extra.items():
            if str(body).strip():
                lines.append("")
                lines.append(f"## {key}")
                lines.append(str(body).strip())

        lines.append("")
        lines.append(HARD_RULES.strip())
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"name": self.name, "persona": self.persona}
        for key, _ in SECTIONS:
            data[key] = getattr(self, key)
        if self.extra:
            data["extra"] = dict(self.extra)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Employee":
        return cls(
            name=str(data.get("name", "")),
            persona=str(data.get("persona", "")),
            system=str(data.get("system", "")),
            actions=str(data.get("actions", "")),
            boundaries=str(data.get("boundaries", "")),
            pitfalls=str(data.get("pitfalls", "")),
            triage=str(data.get("triage", "")),
            output_format=str(data.get("output_format", "")),
            extra={str(k): str(v) for k, v in (data.get("extra") or {}).items()},
        )

    @classmethod
    def from_file(cls, path: str | Path) -> "Employee":
        """从 JSON 读员工定义。

        YAML 需要 pyyaml；没装会给出明确提示，不静默失败。
        """
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"员工定义不存在：{p}")
        text = p.read_text(encoding="utf-8")

        if p.suffix.lower() in (".yaml", ".yml"):
            try:
                import yaml  # type: ignore[import-untyped]
            except ImportError as exc:  # pragma: no cover - 取决于环境
                raise ImportError(
                    "读取 YAML 员工定义需要 pyyaml：pip install pyyaml；或改用 .json"
                ) from exc
            data = yaml.safe_load(text)
        else:
            data = json.loads(text)

        return cls.from_dict(data)
