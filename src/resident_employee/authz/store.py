"""授权档案：签发 / 撤销 / 消费 / 查询。

两条**关键设计**，都写在代码里而不是文档里：

1. **授权码只存哈希，不存明文。** 档案文件泄露 ≠ 授权被冒用。
   明文码只在签发的那一刻返回一次，之后系统里再也拿不到。

2. **码必须存在 AI 读不到的地方。** 这是 ② 档（一次性授权码）的**唯一弱点**：
   码和执行者在同一侧，AI 若能读到配置里的码，就自己把闸门开了。
   所以本模块的使用方式是——**执行器（独立进程／CLI）持码，AI 调执行器的接口但不持码**。
   换句话说：**闸门的钥匙不能挂在门上**（同 `data-scope.md`）。
   要连"执行器也信不过"，就得上 ③ 档（人类私钥签名，AI 造不出签名）。
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .audit import EVENT_ISSUED, EVENT_REVOKED, AuditChain
from .grant import Grant, now_utc

CODE_BYTES = 24
"""授权码的熵。24 字节 ≈ 192 位，远超暴力破解可行性。"""


def hash_code(code: str) -> str:
    """授权码的哈希。**不进签名链**——它只是档案里的查找键。"""
    return hashlib.sha256(code.strip().encode("utf-8")).hexdigest()


def make_code() -> str:
    return secrets.token_urlsafe(CODE_BYTES)


@dataclass
class GrantRecord:
    """档案里的一行。

    这里**故意用可变对象**——`used_count` 本来就会随使用增长，
    装成不可变反而要在每次消费时重建整个档案。
    """

    grant: Grant
    code_hash: str = ""
    """空串 = 无码档（③ 签名令牌自带凭据，不需要码）。"""

    used_count: int = 0
    revoked_at: str = ""
    revoked_by: str = ""

    @property
    def is_revoked(self) -> bool:
        return bool(self.revoked_at)

    @property
    def remaining(self) -> int | None:
        """还能用几次。`None` = 有效期内的次数不限。"""
        if self.grant.max_uses is None:
            return None
        return max(0, self.grant.max_uses - self.used_count)

    def to_dict(self) -> dict[str, Any]:
        return {
            "grant": self.grant.to_dict(),
            "code_hash": self.code_hash,
            "used_count": self.used_count,
            "revoked_at": self.revoked_at,
            "revoked_by": self.revoked_by,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "GrantRecord":
        return cls(
            grant=Grant.from_dict(data["grant"]),
            code_hash=str(data.get("code_hash", "")),
            used_count=int(data.get("used_count", 0)),
            revoked_at=str(data.get("revoked_at", "")),
            revoked_by=str(data.get("revoked_by", "")),
        )


class GrantNotFound(KeyError):
    """档案里没有这个编号的授权书。"""


class GrantStore:
    """JSON 落盘的授权档案。人能直接打开看、能 grep、能进版本管理。"""

    def __init__(self, path: str | Path, audit: AuditChain | None = None) -> None:
        self.path = Path(path)
        self.audit = audit
        self._lock = threading.Lock()
        self._records: dict[str, GrantRecord] = {}
        self._loaded = False

    # --- 落盘 -------------------------------------------------------------

    def _load(self) -> None:
        if self._loaded:
            return
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._records = {
                str(row["grant"]["grant_id"]): GrantRecord.from_dict(row)
                for row in data.get("grants", [])
            }
        self._loaded = True

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"grants": [r.to_dict() for r in self._records.values()]}
        # 先写临时文件再替换：写一半崩掉不会毁掉整份档案
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    # --- 人类侧：签发 / 撤销 -----------------------------------------------

    def issue(
        self,
        grant: Grant,
        *,
        with_code: bool = True,
        actor: str = "人类",
    ) -> tuple[GrantRecord, str]:
        """签发一份授权。校验不过就抛异常，**不静默通过**。

        Returns:
            ``(档案记录, 明文授权码)``。明文码**只在这一刻出现**，之后档案里只有哈希。
            `with_code=False` 时返回空串（③ 签名档不需要码）。
        """
        grant.validate()

        with self._lock:
            self._load()
            if grant.grant_id in self._records:
                raise ValueError(f"授权编号已存在：{grant.grant_id}")

            code = make_code() if with_code else ""
            record = GrantRecord(
                grant=grant,
                code_hash=hash_code(code) if with_code else "",
            )
            self._records[grant.grant_id] = record
            self._flush()

        if self.audit is not None:
            self.audit.append(
                EVENT_ISSUED,
                grant_id=grant.grant_id,
                actor=actor,
                detail={
                    "subject": grant.subject,
                    "actions": list(grant.actions),
                    "resources": list(grant.resources),
                    "expires_at": grant.expires_at,
                    "max_uses": grant.max_uses,
                    "has_code": with_code,
                },
            )
        return record, code

    def revoke(self, grant_id: str, *, actor: str = "人类") -> GrantRecord:
        """撤销。**高风险类操作**——调用方必须先取得人类确认。"""
        with self._lock:
            self._load()
            record = self._records.get(grant_id)
            if record is None:
                raise GrantNotFound(grant_id)
            if record.revoked_at:
                return record  # 幂等：重复撤销不报错，也不重复记审计
            record.revoked_at = now_utc()
            record.revoked_by = actor
            self._flush()

        if self.audit is not None:
            self.audit.append(
                EVENT_REVOKED, grant_id=grant_id, actor=actor, detail={"revoked_by": actor}
            )
        return record

    # --- 读 ---------------------------------------------------------------

    def get(self, grant_id: str) -> GrantRecord | None:
        with self._lock:
            self._load()
            return self._records.get(grant_id)

    def list(self, *, include_revoked: bool = False) -> list[GrantRecord]:
        with self._lock:
            self._load()
            rows = list(self._records.values())
        if not include_revoked:
            rows = [r for r in rows if not r.is_revoked]
        return rows

    # --- 执行器侧：消费 ----------------------------------------------------

    def consume(self, grant_id: str) -> GrantRecord:
        """记一次使用。**只由校验通过后的执行器调用**，不要单独调。"""
        with self._lock:
            self._load()
            record = self._records.get(grant_id)
            if record is None:
                raise GrantNotFound(grant_id)
            record.used_count += 1
            self._flush()
            return record
