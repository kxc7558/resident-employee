"""授权书：六字段 + 校验。

规范（`authorization.md`）要求授权书必须写清六样，**缺一个就是不合格设计**：

| 字段 | 含义 | 缺了会怎样 |
|---|---|---|
| 谁签的 | 人类身份 | 出了事无法追责 |
| 给谁 | 员工／AI 身份 | 别人捡到就能用 |
| 做什么 | 动作白名单 | 权限无限大 |
| 对什么 | 资源范围 | 越权 |
| 到什么时候 | 有效期 | 永久有效 |
| 多少次 | 次数上限 | 一次授权刷到底 |

**不可变对象**：授权书一旦被改动，签名/哈希就废了——所以用 `frozen=True`。
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

# 六字段（顺序即规范里的顺序）
REQUIRED_FIELDS: tuple[str, ...] = (
    "issuer",  # 谁签的
    "subject",  # 给谁
    "actions",  # 做什么
    "resources",  # 对什么
    "issued_at",  # 从什么时候生效
    "expires_at",  # 到什么时候为止
)

# 可选字段
OPTIONAL_FIELDS: tuple[str, ...] = ("max_uses", "nonce", "grant_id", "note")

ACTION_SEPARATOR = ","


class GrantError(ValueError):
    """授权书不合格——六字段缺失或逻辑不自洽。"""


def now_utc() -> str:
    """当前 UTC 时间（ISO-8601，秒级）。"""
    return format_dt(datetime.now(timezone.utc))


def format_dt(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_dt(text: str) -> datetime:
    """解析 ISO-8601。**不带时区的按 UTC 处理**，不猜本地时区。"""
    moment = datetime.fromisoformat(str(text))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def in_hours(hours: float) -> str:
    """从现在起 N 小时后的时间戳。"""
    return format_dt(datetime.now(timezone.utc) + timedelta(hours=hours))


def canonical_json(obj: Any) -> str:
    """规范 JSON：键排序、无多余空白。

    签名和哈希必须基于**规范化**的字节——否则同一个对象在不同实现里
    序列化出的字节不同，签名验不过。这是个经典坑。
    """
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _as_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(ACTION_SEPARATOR)]
        return tuple(p for p in parts if p)
    if isinstance(value, (list, tuple)):
        return tuple(str(v).strip() for v in value if str(v).strip())
    raise GrantError(f"{field_name} 必须是字符串或字符串列表，收到 {type(value).__name__}")


@dataclass(frozen=True)
class Grant:
    """一份授权书。字段不可变。"""

    issuer: str
    subject: str
    actions: tuple[str, ...]
    resources: tuple[str, ...]
    issued_at: str
    expires_at: str
    max_uses: int | None = None
    """`None` = 有效期内次数不限（低风险档：一天签一次）；
    高风险档应设 `1`（一次性令牌，用完即废）。"""

    nonce: str = ""
    grant_id: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "issuer", str(self.issuer).strip())
        object.__setattr__(self, "subject", str(self.subject).strip())
        object.__setattr__(self, "actions", _as_tuple(self.actions, "actions"))
        object.__setattr__(self, "resources", _as_tuple(self.resources, "resources"))
        object.__setattr__(self, "issued_at", str(self.issued_at))
        object.__setattr__(self, "expires_at", str(self.expires_at))
        if not self.grant_id:
            object.__setattr__(self, "grant_id", f"g-{secrets.token_hex(8)}")
        if not self.nonce:
            object.__setattr__(self, "nonce", secrets.token_hex(16))
        if self.max_uses is not None:
            object.__setattr__(self, "max_uses", int(self.max_uses))

    # --- 校验 ---------------------------------------------------------------

    def validate(self) -> "Grant":
        """六字段齐全 + 逻辑自洽。不合格就抛 `GrantError`（**不静默通过**）。"""
        missing = [
            name
            for name in REQUIRED_FIELDS
            if not getattr(self, name) and getattr(self, name) != ()
        ]
        if missing:
            raise GrantError(f"授权书缺字段：{'、'.join(missing)}（六字段缺一个就是不合格）")

        if not self.actions:
            raise GrantError("actions 为空——没给任何权限的授权书不该签，请写明能做什么")
        if not self.resources:
            raise GrantError("resources 为空——请写明这份授权作用在什么资源上")

        issued = parse_dt(self.issued_at)
        expires = parse_dt(self.expires_at)
        if expires <= issued:
            raise GrantError(f"expires_at({self.expires_at}) 必须晚于 issued_at({self.issued_at})")

        if self.max_uses is not None and self.max_uses < 1:
            raise GrantError(f"max_uses 必须 ≥1 或为 None，收到 {self.max_uses}")

        return self

    # --- 判定 ---------------------------------------------------------------

    def is_expired(self, at: str | None = None) -> bool:
        return parse_dt(at or now_utc()) >= parse_dt(self.expires_at)

    def allows_action(self, action: str) -> bool:
        return "*" in self.actions or action in self.actions

    def allows_resource(self, resource: str) -> bool:
        return "*" in self.resources or resource in self.resources

    # --- 序列化 -------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "grant_id": self.grant_id,
            "issuer": self.issuer,
            "subject": self.subject,
            "actions": list(self.actions),
            "resources": list(self.resources),
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "max_uses": self.max_uses,
            "nonce": self.nonce,
            "note": self.note,
        }

    def signed_payload(self) -> bytes:
        """签名/哈希的**对象**：六字段 + 编号 + nonce。

        `note` 不进签名——它是给人看的备注，改了不该导致签名失效。
        """
        return canonical_json(
            {
                "grant_id": self.grant_id,
                "issuer": self.issuer,
                "subject": self.subject,
                "actions": list(self.actions),
                "resources": list(self.resources),
                "issued_at": self.issued_at,
                "expires_at": self.expires_at,
                "max_uses": self.max_uses,
                "nonce": self.nonce,
            }
        ).encode("utf-8")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Grant":
        return cls(
            issuer=data.get("issuer", ""),
            subject=data.get("subject", ""),
            actions=data.get("actions", ()),
            resources=data.get("resources", ()),
            issued_at=data.get("issued_at", ""),
            expires_at=data.get("expires_at", ""),
            max_uses=data.get("max_uses"),
            nonce=data.get("nonce", ""),
            grant_id=data.get("grant_id", ""),
            note=data.get("note", ""),
        )
