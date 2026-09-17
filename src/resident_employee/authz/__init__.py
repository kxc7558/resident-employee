"""授权（authz）：让"AI 绕不过去"的那一半。

框架的另一半是 `sql-guard`（**能看见什么**）；这里管**能做什么**。

| 档 | 机制 | 拦什么 | AI 能否绕过 |
|---|---|---|---|
| ② | 一次性授权码 + 审计哈希链 | 执行器持码 | **能**——读到码就行（所以码不能放在 AI 读得到的地方） |
| ③ | 人类私钥签名的令牌 | 密码学验签 | **不能**——没有私钥，数学上造不出签名 |

`--yes` 是约定，签名是物理。所以标准底线是 ②，要"不可抵赖"就上 ③。

典型用法（人类侧签发）::

    from resident_employee.authz import Grant, GrantStore, AuditChain, in_hours

    audit = AuditChain("data/audit.jsonl")
    store = GrantStore("data/grants.json", audit)

    grant = Grant(
        issuer="张三",                 # 谁签的
        subject="key-pool-operator",   # 给谁
        actions=("add-alias",),        # 做什么
        resources=("api-key-pool",),   # 对什么
        issued_at=now_utc(),           # 从什么时候
        expires_at=in_hours(24),       # 到什么时候
    )
    record, code = store.issue(grant)   # code 只在这一刻出现，档案里只有哈希

典型用法（执行器侧校验）::

    from resident_employee.authz import authorize

    decision = authorize(
        store, grant_id=record.grant.grant_id,
        action="add-alias", resource="api-key-pool", code=code,
    )
    if not decision.allowed:
        return decision.brief()
"""

from .audit import (
    EVENT_ALLOWED,
    EVENT_DENIED,
    EVENT_ISSUED,
    EVENT_REVOKED,
    GENESIS,
    AuditChain,
    AuditEntry,
)
from .grant import (
    REQUIRED_FIELDS,
    Grant,
    GrantError,
    canonical_json,
    format_dt,
    in_hours,
    now_utc,
    parse_dt,
)
from .signed import (
    SignedGrant,
    TokenError,
    generate_keypair,
    issue_token,
    parse_token,
    public_from_private,
    verify_signed,
)
from .store import GrantNotFound, GrantRecord, GrantStore, hash_code, make_code
from .verify import (
    ACTION_NOT_ALLOWED,
    BAD_CODE,
    EXHAUSTED,
    EXPIRED,
    GRANT_NOT_FOUND,
    OK,
    RESOURCE_NOT_ALLOWED,
    REVOKED,
    AuthDecision,
    authorize,
)

__all__ = [
    "ACTION_NOT_ALLOWED",
    "AuditChain",
    "AuditEntry",
    "AuthDecision",
    "BAD_CODE",
    "EVENT_ALLOWED",
    "EVENT_DENIED",
    "EVENT_ISSUED",
    "EVENT_REVOKED",
    "EXHAUSTED",
    "EXPIRED",
    "GENESIS",
    "GRANT_NOT_FOUND",
    "Grant",
    "GrantError",
    "GrantNotFound",
    "GrantRecord",
    "GrantStore",
    "OK",
    "REQUIRED_FIELDS",
    "RESOURCE_NOT_ALLOWED",
    "REVOKED",
    "SignedGrant",
    "TokenError",
    "authorize",
    "canonical_json",
    "format_dt",
    "generate_keypair",
    "hash_code",
    "in_hours",
    "issue_token",
    "make_code",
    "now_utc",
    "parse_dt",
    "parse_token",
    "public_from_private",
    "verify_signed",
]
