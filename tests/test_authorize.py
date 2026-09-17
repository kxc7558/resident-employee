"""校验流程测试：② 一次性授权码档 + ③ 人类私钥签名档。

对应规范 `authorization.md` 的验收单：
- AI 尝试**续期/自派生** → 必须失败
- **撤销**：签发后撤销，下一次执行必须被拒
- **过期**：令牌过期后执行必须被拒
- 私钥不在项目目录、不在 git 里
"""

from __future__ import annotations

import json

import pytest

from resident_employee.authz import (
    ACTION_NOT_ALLOWED,
    BAD_CODE,
    EXHAUSTED,
    EXPIRED,
    GRANT_NOT_FOUND,
    OK,
    RESOURCE_NOT_ALLOWED,
    REVOKED,
    AuditChain,
    Grant,
    GrantStore,
    authorize,
    generate_keypair,
    in_hours,
    issue_token,
    now_utc,
    verify_signed,
)
from resident_employee.authz.audit import EVENT_ALLOWED, EVENT_DENIED
from resident_employee.authz.grant import format_dt

from datetime import datetime, timedelta, timezone


def hours_ago(hours: float) -> str:
    return format_dt(datetime.now(timezone.utc) - timedelta(hours=hours))


def make_grant(**overrides) -> Grant:
    base = {
        "issuer": "张三",
        "subject": "key-pool-operator",
        "actions": ("add-alias",),
        "resources": ("api-key-pool",),
        "issued_at": hours_ago(1),
        "expires_at": in_hours(24),
    }
    base.update(overrides)
    return Grant(**base)


@pytest.fixture
def world(tmp_path):
    audit = AuditChain(tmp_path / "audit.jsonl")
    store = GrantStore(tmp_path / "grants.json", audit)
    return store, audit, tmp_path


# ===== ② 一次性授权码档 =====================================================


def test_happy_path(world):
    store, _, _ = world
    record, code = store.issue(make_grant())

    decision = authorize(
        store, grant_id=record.grant.grant_id, action="add-alias",
        resource="api-key-pool", code=code,
    )

    assert decision.allowed
    assert decision.code == OK


def test_plaintext_code_is_never_persisted(world):
    """档案泄露 ≠ 授权被冒用：只存哈希。"""
    store, _, tmp_path = world
    record, code = store.issue(make_grant())

    on_disk = (tmp_path / "grants.json").read_text(encoding="utf-8")

    assert code not in on_disk
    assert record.code_hash in on_disk


def test_wrong_code_rejected(world):
    store, _, _ = world
    record, _ = store.issue(make_grant())

    decision = authorize(
        store, grant_id=record.grant.grant_id, action="add-alias",
        resource="api-key-pool", code="猜的码",
    )

    assert not decision.allowed
    assert decision.code == BAD_CODE


def test_unknown_grant_rejected(world):
    store, _, _ = world
    decision = authorize(
        store, grant_id="g-不存在", action="add-alias", resource="api-key-pool", code="x"
    )
    assert decision.code == GRANT_NOT_FOUND


def test_expired_grant_rejected(world):
    store, _, _ = world
    record, code = store.issue(
        make_grant(issued_at=hours_ago(3), expires_at=hours_ago(1))
    )

    decision = authorize(
        store, grant_id=record.grant.grant_id, action="add-alias",
        resource="api-key-pool", code=code,
    )

    assert decision.code == EXPIRED


def test_revoked_grant_rejected(world):
    store, _, _ = world
    record, code = store.issue(make_grant())
    store.revoke(record.grant.grant_id, actor="张三")

    decision = authorize(
        store, grant_id=record.grant.grant_id, action="add-alias",
        resource="api-key-pool", code=code,
    )

    assert decision.code == REVOKED


def test_revoke_is_idempotent(world):
    store, audit, _ = world
    record, _ = store.issue(make_grant())

    store.revoke(record.grant.grant_id)
    first = len(audit)
    store.revoke(record.grant.grant_id)

    assert len(audit) == first  # 不重复记审计


def test_one_time_grant_is_exhausted_after_use(world):
    store, _, _ = world
    record, code = store.issue(make_grant(max_uses=1))

    first = authorize(
        store, grant_id=record.grant.grant_id, action="add-alias",
        resource="api-key-pool", code=code,
    )
    second = authorize(
        store, grant_id=record.grant.grant_id, action="add-alias",
        resource="api-key-pool", code=code,
    )

    assert first.allowed
    assert second.code == EXHAUSTED


def test_unlimited_grant_keeps_working(world):
    store, _, _ = world
    record, code = store.issue(make_grant(max_uses=None))

    for _ in range(3):
        decision = authorize(
            store, grant_id=record.grant.grant_id, action="add-alias",
            resource="api-key-pool", code=code,
        )
        assert decision.allowed


def test_action_outside_whitelist_rejected(world):
    store, _, _ = world
    record, code = store.issue(make_grant(actions=("add-alias",)))

    decision = authorize(
        store, grant_id=record.grant.grant_id, action="del-provider",
        resource="api-key-pool", code=code,
    )

    assert decision.code == ACTION_NOT_ALLOWED


def test_resource_outside_whitelist_rejected(world):
    store, _, _ = world
    record, code = store.issue(make_grant(resources=("api-key-pool",)))

    decision = authorize(
        store, grant_id=record.grant.grant_id, action="add-alias",
        resource="别人的台账", code=code,
    )

    assert decision.code == RESOURCE_NOT_ALLOWED


def test_denials_are_audited_too(world):
    """只记成功的审计没用——要知道谁试过碰不该碰的。"""
    store, audit, _ = world
    record, code = store.issue(make_grant())

    authorize(
        store, grant_id=record.grant.grant_id, action="del-provider",
        resource="api-key-pool", code=code,
    )

    events = [(e.event, e.detail.get("decision")) for e in audit]
    assert (EVENT_ALLOWED, None) not in events
    assert (EVENT_DENIED, ACTION_NOT_ALLOWED) in events
    assert audit.verify()[0]


def test_success_is_audited(world):
    store, audit, _ = world
    record, code = store.issue(make_grant())

    authorize(
        store, grant_id=record.grant.grant_id, action="add-alias",
        resource="api-key-pool", code=code,
    )

    assert any(e.event == EVENT_ALLOWED for e in audit)


def test_audit_chain_stays_valid_through_full_lifecycle(world):
    store, audit, _ = world
    record, code = store.issue(make_grant(max_uses=2))
    authorize(store, grant_id=record.grant.grant_id, action="add-alias", resource="api-key-pool", code=code)
    authorize(store, grant_id=record.grant.grant_id, action="违禁", resource="api-key-pool", code=code)
    store.revoke(record.grant.grant_id)

    ok, note = audit.verify()
    assert ok, note


def test_ai_cannot_self_issue(world):
    """AI 自己"续期"只能靠调用 store.issue —— 而那是人类侧接口。

    这条测试固化的是**设计约束**：`issue` 不在执行器侧的调用路径里。
    执行器拿到的只有 `authorize`，它无法凭空造出授权。
    """
    import resident_employee.authz.verify as verify_mod

    exported = set(verify_mod.__dict__)
    assert "issue" not in exported
    assert not any(name.endswith("_issue") for name in exported)


# ===== ③ 人类私钥签名档 =====================================================


def test_signed_roundtrip():
    private, public = generate_keypair()
    token = issue_token(make_grant(), private)

    decision = verify_signed(
        token, public, action="add-alias", resource="api-key-pool"
    )

    assert decision.allowed, decision.brief()


def test_tampered_token_rejected():
    """把 actions 改成 'del-provider' 再验——签名必须验不过。"""
    private, public = generate_keypair()
    token = issue_token(make_grant(actions=("add-alias",)), private)

    payload = json.loads(token)
    payload["grant"]["actions"] = ["del-provider"]
    tampered = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    decision = verify_signed(tampered, public, action="del-provider", resource="api-key-pool")

    assert not decision.allowed
    assert decision.code == BAD_CODE
    assert "签名验不过" in decision.reason


def test_token_from_another_key_is_rejected():
    """别的私钥签的令牌，用这把公钥验不过——这就是"AI 造不出签名"的落点。"""
    attacker_private, _ = generate_keypair()
    _, victim_public = generate_keypair()

    forged = issue_token(make_grant(), attacker_private)

    decision = verify_signed(forged, victim_public, action="add-alias", resource="api-key-pool")

    assert decision.code == BAD_CODE


def test_signed_revocation_list_is_honored():
    """补丁二：签名令牌天生不可撤销，靠撤销名单补。"""
    private, public = generate_keypair()
    grant = make_grant()
    token = issue_token(grant, private)

    decision = verify_signed(
        token, public, action="add-alias", resource="api-key-pool",
        revoked_ids={grant.grant_id},
    )

    assert decision.code == REVOKED


def test_signed_one_time_nonce_replay_rejected():
    """补丁三：一次性令牌靠 nonce 记录防重放。"""
    private, public = generate_keypair()
    grant = make_grant(max_uses=1)
    token = issue_token(grant, private)

    first = verify_signed(token, public, action="add-alias", resource="api-key-pool")
    second = verify_signed(
        token, public, action="add-alias", resource="api-key-pool",
        used_nonces={grant.nonce},
    )

    assert first.allowed
    assert second.code == EXHAUSTED


def test_signed_multi_use_is_explicitly_refused():
    """签名令牌没法自行计数——明确拒绝并说明原因，不假装支持。"""
    private, public = generate_keypair()
    token = issue_token(make_grant(max_uses=5), private)

    decision = verify_signed(token, public, action="add-alias", resource="api-key-pool")

    assert not decision.allowed
    assert decision.code == EXHAUSTED
    assert "无法自行计数" in decision.reason


def test_signed_expiry_honored():
    private, public = generate_keypair()
    token = issue_token(make_grant(issued_at=hours_ago(3), expires_at=hours_ago(1)), private)

    decision = verify_signed(token, public, action="add-alias", resource="api-key-pool")

    assert decision.code == EXPIRED


def test_signed_scope_is_enforced():
    private, public = generate_keypair()
    token = issue_token(make_grant(actions=("add-alias",)), private)

    decision = verify_signed(token, public, action="清空池子", resource="api-key-pool")

    assert decision.code == ACTION_NOT_ALLOWED


def test_garbage_token_rejected_not_crashed():
    _, public = generate_keypair()

    for bad in ("不是 json", "{}", '{"alg":"rsa","grant":{},"signature":""}'):
        decision = verify_signed(bad, public, action="a", resource="r")
        assert decision.code == BAD_CODE


def test_signed_audit_records_mode():
    private, public = generate_keypair()
    token = issue_token(make_grant(), private)

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        audit = AuditChain(Path(tmp) / "audit.jsonl")
        verify_signed(
            token, public, action="add-alias", resource="api-key-pool", audit=audit
        )
        assert audit.verify()[0]
        assert audit.tail(1)[0].detail["mode"] == "signed"
