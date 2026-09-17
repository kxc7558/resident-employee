"""审计哈希链：改一条，后面全对不上。

规范要求「每条 SQL／每个动作都留痕」，且记录**不可篡改**。
实现不靠区块链，靠最便宜的那一招——**哈希链**：每条记录带上
前一条的哈希，改任何一条，从它往后全部校验失败。成本≈0。

配合人类签名，两条性质就齐了：

| 机制 | 保证 |
|---|---|
| 哈希链 | 记录**不可篡改** |
| 人类签名 | 授权**不可抵赖** |

**允许和拒绝都要记。** 只记成功的审计是没用的——想知道"谁试过碰不该碰的"，
恰恰要记被拒的那些。
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from .grant import canonical_json, now_utc

GENESIS = "0" * 64
"""链首的前哈希。用全零而不是空串，便于一眼看出这是链首。"""

# 事件类型
EVENT_ISSUED = "issued"
EVENT_REVOKED = "revoked"
EVENT_ALLOWED = "allowed"
EVENT_DENIED = "denied"


@dataclass(frozen=True)
class AuditEntry:
    seq: int
    at: str
    event: str
    grant_id: str
    actor: str
    detail: Mapping[str, Any]
    prev_hash: str
    hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "at": self.at,
            "event": self.event,
            "grant_id": self.grant_id,
            "actor": self.actor,
            "detail": dict(self.detail),
            "prev_hash": self.prev_hash,
            "hash": self.hash,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AuditEntry":
        return cls(
            seq=int(data["seq"]),
            at=str(data["at"]),
            event=str(data["event"]),
            grant_id=str(data.get("grant_id", "")),
            actor=str(data.get("actor", "")),
            detail=dict(data.get("detail") or {}),
            prev_hash=str(data.get("prev_hash", GENESIS)),
            hash=str(data["hash"]),
        )


def compute_hash(
    *, seq: int, at: str, event: str, grant_id: str, actor: str,
    detail: Mapping[str, Any], prev_hash: str,
) -> str:
    """一条记录的哈希 = SHA-256(前哈希 + 本记录规范化内容)。"""
    body = canonical_json(
        {
            "seq": seq,
            "at": at,
            "event": event,
            "grant_id": grant_id,
            "actor": actor,
            "detail": dict(detail),
        }
    )
    return hashlib.sha256((prev_hash + body).encode("utf-8")).hexdigest()


class AuditChain:
    """JSONL 落盘的哈希链。一行一条记录，人能直接看。

    用 JSONL 而不是单个 JSON 数组，是为了**追加不用重写整个文件**——
    审计日志会一直长，每次重写全文迟早出事。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._entries: list[AuditEntry] | None = None

    # --- 读 ---------------------------------------------------------------

    def _load(self) -> list[AuditEntry]:
        if self._entries is not None:
            return self._entries
        if not self.path.exists():
            self._entries = []
            return self._entries
        entries: list[AuditEntry] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            entries.append(AuditEntry.from_dict(json.loads(line)))
        self._entries = entries
        return entries

    def __iter__(self) -> Iterator[AuditEntry]:
        return iter(list(self._load()))

    def __len__(self) -> int:
        return len(self._load())

    def tail(self, count: int = 10) -> list[AuditEntry]:
        return self._load()[-count:]

    # --- 写 ---------------------------------------------------------------

    def append(
        self,
        event: str,
        *,
        grant_id: str = "",
        actor: str = "",
        detail: Mapping[str, Any] | None = None,
    ) -> AuditEntry:
        """追加一条。哈希接在链尾。"""
        with self._lock:
            entries = self._load()
            prev_hash = entries[-1].hash if entries else GENESIS
            seq = (entries[-1].seq + 1) if entries else 1
            at = now_utc()
            body = dict(detail or {})
            entry = AuditEntry(
                seq=seq,
                at=at,
                event=event,
                grant_id=grant_id,
                actor=actor,
                detail=body,
                prev_hash=prev_hash,
                hash=compute_hash(
                    seq=seq, at=at, event=event, grant_id=grant_id,
                    actor=actor, detail=body, prev_hash=prev_hash,
                ),
            )
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(canonical_json(entry.to_dict()) + "\n")
            entries.append(entry)
            return entry

    # --- 校验 -------------------------------------------------------------

    def verify(self) -> tuple[bool, str]:
        """从头验证整条链。返回 ``(是否完整, 说明)``。

        三种坏法都要能查出来：记录被改、记录被删（前哈希对不上）、
        顺序被换（seq 不连续）。
        """
        entries = self._load()
        expected_prev = GENESIS
        for index, entry in enumerate(entries, start=1):
            if entry.seq != index:
                return False, f"第 {index} 条的 seq 是 {entry.seq}，顺序被改过或记录被删"
            if entry.prev_hash != expected_prev:
                return False, f"第 {index} 条的前哈希对不上——它前面那条被改过或删过"
            recomputed = compute_hash(
                seq=entry.seq, at=entry.at, event=entry.event,
                grant_id=entry.grant_id, actor=entry.actor,
                detail=entry.detail, prev_hash=entry.prev_hash,
            )
            if recomputed != entry.hash:
                return False, f"第 {index} 条内容被改过（哈希不符）"
            expected_prev = entry.hash
        return True, f"{len(entries)} 条记录，链完整"
