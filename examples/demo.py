"""跑一遍看效果：签发 → 放行 → 越权被拒 → 撤销 → 篡改被验出。

    python examples/demo.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

# Windows 控制台默认 GBK，中文会乱码——先切到 UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from resident_employee.authz import (  # noqa: E402
    AuditChain,
    Grant,
    GrantError,
    GrantStore,
    authorize,
    generate_keypair,
    in_hours,
    issue_token,
    now_utc,
    verify_signed,
)


def line(title: str) -> None:
    print(f"\n{title}")
    print("-" * 68)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="resident-employee-demo-"))
    audit = AuditChain(tmp / "audit.jsonl")
    store = GrantStore(tmp / "grants.json", audit)
    print(f"工作目录：{tmp}")

    # --- ① 六字段缺一个就签不出来 -----------------------------------------
    line("① 授权书六字段：缺一个就是不合格设计")
    try:
        Grant(
            issuer="张三", subject="",  # ← 没写"给谁"
            actions=("add-alias",), resources=("api-key-pool",),
            issued_at=now_utc(), expires_at=in_hours(24),
        ).validate()
    except GrantError as exc:
        print(f"  ✗ 如预期被拒：{exc}")

    # --- ② 签发（明文码只出现这一次） --------------------------------------
    line("② 签发：明文码只出现一次，档案里只有哈希")
    grant = Grant(
        issuer="张三",
        subject="key-pool-operator",
        actions=("add-alias", "set-strategy"),
        resources=("api-key-pool",),
        issued_at=now_utc(),
        expires_at=in_hours(24),
        note="低风险档：一天签一次，次数不限",
    )
    record, code = store.issue(grant)
    print(f"  授权编号：{record.grant.grant_id}")
    print(f"  明文授权码：{code}")
    on_disk = (tmp / "grants.json").read_text(encoding="utf-8")
    print(f"  档案里有明文码吗：{'有（不合格！）' if code in on_disk else '没有 ✓ 只有哈希'}")

    # --- ③ 正常操作放行 ----------------------------------------------------
    line("③ 员工请求动作：白名单内 → 放行")
    decision = authorize(
        store, grant_id=record.grant.grant_id, action="add-alias",
        resource="api-key-pool", code=code, actor="key-pool-operator",
    )
    print(f"  {decision.brief()}")

    # --- ④ 越权被拒 --------------------------------------------------------
    line("④ 员工请求白名单外的动作 → 拒绝（并记审计）")
    for action, resource in [
        ("del-provider", "api-key-pool"),
        ("add-alias", "同事的台账"),
    ]:
        bad = authorize(
            store, grant_id=record.grant.grant_id, action=action,
            resource=resource, code=code, actor="key-pool-operator",
        )
        print(f"  {bad.brief()}")

    # --- ⑤ 撤销后立刻失效 --------------------------------------------------
    line("⑤ 撤销：人确认后撤销，下一次执行必须被拒")
    store.revoke(record.grant.grant_id, actor="张三")
    after = authorize(
        store, grant_id=record.grant.grant_id, action="add-alias",
        resource="api-key-pool", code=code, actor="key-pool-operator",
    )
    print(f"  {after.brief()}")

    # --- ⑥ 审计：允许和拒绝都记，链可验 ------------------------------------
    line("⑥ 审计哈希链：允许和拒绝都记，改一条能验出")
    print(f"  链长 {len(audit)} 条，校验：{audit.verify()[1]}")
    for entry in audit:
        decision_code = entry.detail.get("decision", "-")
        print(f"    seq={entry.seq} {entry.event:8} {decision_code:22} 演员={entry.actor}")

    # 篡改演示：把第一次放行改成别的动作
    audit_path = tmp / "audit.jsonl"
    rows = [json.loads(x) for x in audit_path.read_text(encoding="utf-8").splitlines()]
    for row in rows:
        if row["event"] == "allowed":
            row["detail"]["action"] = "del-provider"  # 事后美化自己的操作记录
    audit_path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True, separators=(",", ":")) for r in rows) + "\n",
        encoding="utf-8",
    )
    ok, note = AuditChain(audit_path).verify()
    print(f"  事后篡改一条后重新校验：{'通过（不合格！）' if ok else '✗ 如预期报错 → ' + note}")

    # --- ⑦ ③ 档：签名令牌 --------------------------------------------------
    line("⑦ ③ 档签名令牌：人类私钥签，AI 只持公钥")
    private, public = generate_keypair()
    signed_grant = Grant(
        issuer="张三", subject="key-pool-operator",
        actions=("add-alias",), resources=("api-key-pool",),
        issued_at=now_utc(), expires_at=in_hours(1), max_uses=1,
    )
    token = issue_token(signed_grant, private)
    good = verify_signed(token, public, action="add-alias", resource="api-key-pool")
    print(f"  真令牌：{good.brief()}")

    payload = json.loads(token)
    payload["grant"]["actions"] = ["del-provider"]  # AI 试图自己扩权
    tampered = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    bad = verify_signed(tampered, public, action="del-provider", resource="api-key-pool")
    print(f"  改过权限后：{bad.brief()}")

    attacker_private, _ = generate_keypair()
    forged = issue_token(signed_grant, attacker_private)
    forged_decision = verify_signed(forged, public, action="add-alias", resource="api-key-pool")
    print(f"  拿别人的私钥签：{forged_decision.brief()}")

    replay = verify_signed(
        token, public, action="add-alias", resource="api-key-pool",
        used_nonces={signed_grant.nonce},
    )
    print(f"  一次性令牌重放：{replay.brief()}")

    print("\n" + "=" * 68)
    print("要点：② 的码读得到就能用（所以码要放在 AI 读不到的地方）；")
    print("      ③ 的签名读得到也造不出（没有私钥，数学上办不到）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
