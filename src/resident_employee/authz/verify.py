"""校验流程——执行器侧的闸门。

七道检查，顺序是**先认证后授权**：

```
1 档案里有这份授权吗          → GRANT_NOT_FOUND
2 授权码对吗                  → BAD_CODE      ← 认证边界
3 被撤销了吗                  → REVOKED
4 过期了吗                    → EXPIRED
5 次数用完了吗                → EXHAUSTED
6 这个动作在白名单里吗        → ACTION_NOT_ALLOWED
7 这个资源在白名单里吗        → RESOURCE_NOT_ALLOWED
   全过                       → OK（并消耗一次）
```

**每一道拒绝都写进审计。** 只记成功的审计是没用的——
想知道"谁试过碰不该碰的"，恰恰要记被拒的那些。
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass

from .audit import EVENT_ALLOWED, EVENT_DENIED, AuditChain
from .grant import now_utc
from .store import GrantStore, hash_code

# 机器可读结论
OK = "OK"
GRANT_NOT_FOUND = "GRANT_NOT_FOUND"
BAD_CODE = "BAD_CODE"
REVOKED = "REVOKED"
EXPIRED = "EXPIRED"
EXHAUSTED = "EXHAUSTED"
ACTION_NOT_ALLOWED = "ACTION_NOT_ALLOWED"
RESOURCE_NOT_ALLOWED = "RESOURCE_NOT_ALLOWED"


@dataclass(frozen=True)
class AuthDecision:
    allowed: bool
    code: str
    reason: str

    def brief(self) -> str:
        return ("放行：" if self.allowed else "拒绝：") + f"{self.code} {self.reason}"


def authorize(
    store: GrantStore,
    *,
    grant_id: str,
    action: str,
    resource: str,
    code: str = "",
    actor: str = "executor",
    at: str | None = None,
) -> AuthDecision:
    """校验一次操作是否被授权。通过则消耗一次使用次数。

    Args:
        store: 授权档案。
        grant_id: 授权编号。
        action: 想做的动作。
        resource: 作用的对象。
        code: ② 档的授权码。③ 档（签名令牌）走 `signed.verify_signed`。
        actor: 谁在请求（写进审计）。
        at: 校验时刻，默认现在。**可注入**是为了让过期逻辑能被测试。
    """
    moment = at or now_utc()

    def decide(decision: AuthDecision, record=None) -> AuthDecision:
        """统一出口——**所有结论都落审计**，一条都别漏。"""
        if store.audit is not None:
            store.audit.append(
                EVENT_ALLOWED if decision.allowed else EVENT_DENIED,
                grant_id=grant_id,
                actor=actor,
                detail={
                    "action": action,
                    "resource": resource,
                    "decision": decision.code,
                    "reason": decision.reason,
                    "subject": record.grant.subject if record else "",
                },
            )
        return decision

    # --- 1. 档案里有吗 ---------------------------------------------------
    record = store.get(grant_id)
    if record is None:
        return decide(AuthDecision(False, GRANT_NOT_FOUND, f"档案里没有授权编号 {grant_id}"))

    # --- 2. 授权码对吗（认证边界）-----------------------------------------
    if record.code_hash:
        # 用 compare_digest 而不是 == ：后者按字节短路比较，能靠计时差猜码
        if not hmac.compare_digest(hash_code(code), record.code_hash):
            return decide(AuthDecision(False, BAD_CODE, "授权码不对"), record)
    elif code:
        # 无码档（③ 签名令牌）被塞了码 —— 说明调用方用错了通道
        return decide(
            AuthDecision(False, BAD_CODE, "这份授权是无码档（签名令牌），不应传授权码"),
            record,
        )

    grant = record.grant

    # --- 3. 撤销了吗 -----------------------------------------------------
    if record.is_revoked:
        return decide(
            AuthDecision(False, REVOKED, f"授权已于 {record.revoked_at} 被 {record.revoked_by} 撤销"),
            record,
        )

    # --- 4. 过期了吗 -----------------------------------------------------
    if grant.is_expired(moment):
        return decide(
            AuthDecision(False, EXPIRED, f"授权已于 {grant.expires_at} 过期"), record
        )

    # --- 5. 次数用完了吗 -------------------------------------------------
    remaining = record.remaining
    if remaining is not None and remaining <= 0:
        return decide(
            AuthDecision(False, EXHAUSTED, f"授权次数已用尽（上限 {grant.max_uses} 次）"),
            record,
        )

    # --- 6/7. 动作与资源在白名单里吗 --------------------------------------
    if not grant.allows_action(action):
        return decide(
            AuthDecision(
                False, ACTION_NOT_ALLOWED, f"动作 {action} 不在白名单：{'、'.join(grant.actions)}"
            ),
            record,
        )
    if not grant.allows_resource(resource):
        return decide(
            AuthDecision(
                False,
                RESOURCE_NOT_ALLOWED,
                f"资源 {resource} 不在白名单：{'、'.join(grant.resources)}",
            ),
            record,
        )

    # --- 全过：消耗一次 ---------------------------------------------------
    store.consume(grant_id)
    left = record.remaining
    tail = "次数不限" if left is None else f"剩余 {left} 次"
    return decide(AuthDecision(True, OK, f"授权有效（{tail}）"), record)
