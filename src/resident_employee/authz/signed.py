"""③ 签名令牌：人类持私钥签发，AI 只持公钥验签。

与 ② 档的**关键区别**：

| | ② 一次性授权码 | ③ 签名令牌 |
|---|---|---|
| 凭据在哪 | 执行器的档案里 | 令牌自己带着 |
| AI 读到凭据会怎样 | **能自己把闸门打开** | **读到了也造不出签名** |
| 要不要服务端存档 | 要 | 不要，有公钥就能验 |

**"读得到就能用" vs "读得到也造不出"——这就是 ②→③ 的全部意义。**

## 三个补丁一个都不能少

签名令牌**天生不可撤销**——签出去就在有效期内一直有效。规范 `authorization.md`
要求三样一起上，少一样就是纸门：

1. **短有效期**（`Grant.expires_at` 本身就短）
2. **撤销名单**（执行前查一次，见 `revoked_ids`）
3. **高风险用一次性**（`max_uses=1` + 用过的 nonce 要记下来，见 `used_nonces`）

注意第 3 条：签名令牌是自带的，**"用过几次"这个状态没地方存**——
必须由执行器记 nonce。这不是设计缺陷，是自带凭据的必然代价：
**无状态的凭据 + 有状态的防重放。**
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass

from .audit import EVENT_ALLOWED, EVENT_DENIED, AuditChain
from .grant import Grant, canonical_json, now_utc
from .verify import (
    ACTION_NOT_ALLOWED,
    BAD_CODE,
    EXPIRED,
    EXHAUSTED,
    RESOURCE_NOT_ALLOWED,
    REVOKED,
    AuthDecision,
)

# ③ 档才需要密码学库；② 档（授权码 + 哈希链）**零依赖**。
# 做成可选导入而不是硬依赖：核心装不上密码学库也应该能用。
try:  # pragma: no cover - 分支取决于环境
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric import ed25519
    HAVE_CRYPTO = True
except ImportError:  # pragma: no cover
    ed25519 = None  # type: ignore[assignment]
    InvalidSignature = Exception  # type: ignore[assignment,misc]
    HAVE_CRYPTO = False

CRYPTO_HINT = (
    "③ 签名档需要密码学库：pip install 'resident-employee[signed]'（即 cryptography）。"
    "只用 ② 档（一次性授权码 + 审计哈希链）不需要它。"
)


def _require_crypto() -> None:
    """缺依赖就**明确报错**，不静默降级——降级成"验不过"会让人以为签名被伪造了。"""
    if not HAVE_CRYPTO:  # pragma: no cover - 取决于环境
        raise ImportError(CRYPTO_HINT)


ALGORITHM = "ed25519"
KEY_BYTES = 32
"""Ed25519 raw key 长度。"""


class TokenError(ValueError):
    """令牌读不动：不是合法 JSON、算法不符、签名格式错。"""


def generate_keypair() -> tuple[bytes, bytes]:
    """生成 ``(私钥 raw, 公钥 raw)``，各 32 字节。

    **私钥绝不落进项目目录、绝不进 git。** 放哪见规范 `authorization.md`：
    硬件密钥 / 系统钥匙串·TPM / 手机 App + 指纹。
    最不该做的就是存成一个文件放在代码旁边——那样等于没签。
    """
    _require_crypto()
    private = ed25519.Ed25519PrivateKey.generate()
    return private.private_bytes_raw(), private.public_key().public_bytes_raw()


def public_from_private(private_key: bytes) -> bytes:
    """从私钥推公钥。给"只想存一份私钥"的场景用。"""
    _require_crypto()
    return ed25519.Ed25519PrivateKey.from_private_bytes(private_key).public_key().public_bytes_raw()


def issue_token(grant: Grant, private_key: bytes) -> str:
    """人类侧：签出令牌。返回一个 JSON 字符串（人和机器都能看）。"""
    _require_crypto()
    grant.validate()
    key = ed25519.Ed25519PrivateKey.from_private_bytes(private_key)
    signature = key.sign(grant.signed_payload())
    return canonical_json(
        {
            "alg": ALGORITHM,
            "grant": grant.to_dict(),
            "signature": base64.b64encode(signature).decode("ascii"),
        }
    )


@dataclass(frozen=True)
class SignedGrant:
    grant: Grant
    signature: bytes


def parse_token(token: str) -> SignedGrant:
    try:
        data = json.loads(token)
    except json.JSONDecodeError as exc:
        raise TokenError(f"令牌不是合法 JSON：{exc}") from exc

    if data.get("alg") != ALGORITHM:
        raise TokenError(f"算法不支持：{data.get('alg')!r}，只支持 {ALGORITHM}")
    if "grant" not in data or "signature" not in data:
        raise TokenError("令牌缺 grant 或 signature")

    try:
        signature = base64.b64decode(data["signature"], validate=True)
    except Exception as exc:  # binascii.Error 等
        raise TokenError(f"签名不是合法 base64：{exc}") from exc

    return SignedGrant(grant=Grant.from_dict(data["grant"]), signature=signature)


def verify_signed(
    token: str,
    public_key: bytes,
    *,
    action: str,
    resource: str,
    revoked_ids: frozenset[str] | set[str] = frozenset(),
    used_nonces: frozenset[str] | set[str] = frozenset(),
    actor: str = "executor",
    at: str | None = None,
    audit: AuditChain | None = None,
) -> AuthDecision:
    """执行器侧：验签 + 三个补丁 + 白名单。

    Args:
        token: 人类签出的令牌。
        public_key: 人类公钥（raw 32 字节）。
        revoked_ids: 已撤销的授权编号。**必须传**——签名令牌本身不可撤销。
        used_nonces: 已用过的 nonce。`max_uses` 有限时靠它防重放。
        at: 校验时刻，可注入（让过期逻辑可测）。
    """
    moment = at or now_utc()

    _require_crypto()

    def decide(decision: AuthDecision, grant_id: str = "", subject: str = "") -> AuthDecision:
        if audit is not None:
            audit.append(
                EVENT_ALLOWED if decision.allowed else EVENT_DENIED,
                grant_id=grant_id,
                actor=actor,
                detail={
                    "action": action,
                    "resource": resource,
                    "decision": decision.code,
                    "reason": decision.reason,
                    "subject": subject,
                    "mode": "signed",
                },
            )
        return decision

    # --- 1. 令牌读得动吗 --------------------------------------------------
    try:
        parsed = parse_token(token)
    except (TokenError, ValueError) as exc:
        return decide(AuthDecision(False, BAD_CODE, f"令牌读不动：{exc}"))

    grant = parsed.grant

    # --- 2. 验签（认证边界）----------------------------------------------
    try:
        key = ed25519.Ed25519PublicKey.from_public_bytes(public_key)
        key.verify(parsed.signature, grant.signed_payload())
    except InvalidSignature:
        return decide(
            AuthDecision(False, BAD_CODE, "签名验不过——令牌被改过，或不是这把私钥签的"),
            grant.grant_id,
            grant.subject,
        )
    except Exception as exc:
        return decide(AuthDecision(False, BAD_CODE, f"公钥不可用：{exc}"))

    # --- 3. 撤销名单（补丁二）--------------------------------------------
    if grant.grant_id in revoked_ids:
        return decide(
            AuthDecision(False, REVOKED, f"授权 {grant.grant_id} 在撤销名单里"),
            grant.grant_id,
            grant.subject,
        )

    # --- 4. 有效期（补丁一）----------------------------------------------
    if grant.is_expired(moment):
        return decide(
            AuthDecision(False, EXPIRED, f"授权已于 {grant.expires_at} 过期"),
            grant.grant_id,
            grant.subject,
        )

    # --- 5. 防重放（补丁三）----------------------------------------------
    if grant.max_uses is not None:
        if grant.max_uses > 1:
            return decide(
                AuthDecision(
                    False,
                    EXHAUSTED,
                    "签名令牌无法自行计数使用次数；要用多次请签 max_uses=None 的长期令牌，"
                    "或用 ② 档（授权码由执行器记数）",
                ),
                grant.grant_id,
                grant.subject,
            )
        if grant.nonce in used_nonces:
            return decide(
                AuthDecision(False, EXHAUSTED, f"一次性令牌已被用过（nonce {grant.nonce[:8]}…）"),
                grant.grant_id,
                grant.subject,
            )

    # --- 6/7. 动作与资源白名单 -------------------------------------------
    if not grant.allows_action(action):
        return decide(
            AuthDecision(
                False, ACTION_NOT_ALLOWED, f"动作 {action} 不在白名单：{'、'.join(grant.actions)}"
            ),
            grant.grant_id,
            grant.subject,
        )
    if not grant.allows_resource(resource):
        return decide(
            AuthDecision(
                False,
                RESOURCE_NOT_ALLOWED,
                f"资源 {resource} 不在白名单：{'、'.join(grant.resources)}",
            ),
            grant.grant_id,
            grant.subject,
        )

    return decide(
        AuthDecision(
            True,
            "OK",
            f"签名有效。用完请把 nonce {grant.nonce[:8]}… 记进 used_nonces（一次性令牌靠它防重放）",
        ),
        grant.grant_id,
        grant.subject,
    )
