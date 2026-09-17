"""② 档人类侧命令行：签发 / 撤销 / 查看。

**分级授权由代码强制**，不靠使用者自觉：

```
只读（status / list / audit / verify-chain / check） → 直接可用
低风险写（issue）                                    → 自动备份后执行
高风险写（revoke）                                   → 必须 --yes，否则退出码 2
```

`revoke` 的 `--yes` 不是礼貌提示，是**代码闸门**——没有它直接拒，
调用方（包括 AI）拿不到"再确认一下"的余地。

数据目录用 `--base` 切换（默认 `./data`，或环境变量 `RESIDENT_EMPLOYEE_BASE`）：
**换环境不改代码**。
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from .cli_base import (
    AUDIT_FILE,
    DEFAULT_BASE,
    EXIT_ERROR,
    EXIT_OK,
    EXIT_REFUSED,
    GRANTS_FILE,
    OUTPUT_WIDTH,
    backup,
    emit,
    open_world,
    resolve_base,
    rule,
)
from .grant import Grant, GrantError, in_hours, now_utc

# --- 只读命令 ---------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    base = resolve_base(args.base)
    store, audit = open_world(base)
    rows = store.list()
    everything = store.list(include_revoked=True)
    now = now_utc()

    expired = [r for r in rows if r.grant.is_expired(now)]
    live = [r for r in rows if not r.grant.is_expired(now)]
    revoked = [r for r in everything if r.is_revoked]
    chain_ok, chain_note = audit.verify()

    payload = {
        "base": str(base),
        "grants_total": len(everything),
        "grants_live": len(live),
        "grants_expired": len(expired),
        "grants_revoked": len(revoked),
        "audit_entries": len(audit),
        "chain_ok": chain_ok,
        "chain_note": chain_note,
    }
    text = "\n".join(
        [
            f"数据目录：{base}",
            f"授权档案：共 {len(everything)} 张（有效 {len(live)} / 已过期 {len(expired)} / 已撤销 {len(revoked)}）",
            f"审计：{len(audit)} 条，链{'完整 ✓' if chain_ok else '已断 ✗'}（{chain_note}）",
        ]
        + (
            ["", "有效授权："]
            + [
                f"  {r.grant.grant_id}  {r.grant.subject}  "
                f"{'/'.join(r.grant.actions)} @ {'/'.join(r.grant.resources)}  到 {r.grant.expires_at}"
                for r in live[:10]
            ]
            if live
            else ["", "有效授权：无"]
        )
    )
    emit(payload, as_json=args.json, text=text)
    return EXIT_OK if chain_ok else EXIT_ERROR


def cmd_list(args: argparse.Namespace) -> int:
    base = resolve_base(args.base)
    store, _ = open_world(base)
    rows = store.list(include_revoked=args.all)
    now = now_utc()

    payload = {"grants": []}
    lines = [f"{'编号':<20} {'给谁':<24} {'状态':<8} 到期"]
    for record in rows:
        grant = record.grant
        if record.is_revoked:
            state = "已撤销"
        elif grant.is_expired(now):
            state = "已过期"
        else:
            state = "有效"
        left = "不限" if record.remaining is None else f"剩 {record.remaining} 次"
        lines.append(f"{grant.grant_id:<20} {grant.subject:<24} {state:<8} {grant.expires_at}（{left}）")
        payload["grants"].append(
            {
                "grant_id": grant.grant_id,
                "subject": grant.subject,
                "issuer": grant.issuer,
                "actions": list(grant.actions),
                "resources": list(grant.resources),
                "expires_at": grant.expires_at,
                "state": state,
                "remaining": record.remaining,
            }
        )
    if not rows:
        lines = ["（没有授权）"]
    emit(payload, as_json=args.json, text="\n".join(lines))
    return EXIT_OK


def cmd_audit(args: argparse.Namespace) -> int:
    base = resolve_base(args.base)
    _, audit = open_world(base)
    entries = [e for e in audit if not args.event or e.event == args.event]
    entries = entries[-args.tail :]

    lines = [f"{'seq':>4}  {'时间':<26} {'事件':<8} {'结论':<22} 演员"]
    for entry in entries:
        lines.append(
            f"{entry.seq:>4}  {entry.at:<26} {entry.event:<8} "
            f"{str(entry.detail.get('decision', '-')):<22} {entry.actor}"
        )
    if not entries:
        lines = ["（没有审计记录）"]
    emit(
        {"entries": [e.to_dict() for e in entries]},
        as_json=args.json,
        text="\n".join(lines),
    )
    return EXIT_OK


def cmd_verify_chain(args: argparse.Namespace) -> int:
    base = resolve_base(args.base)
    _, audit = open_world(base)
    ok, note = audit.verify()
    emit(
        {"ok": ok, "note": note, "entries": len(audit)},
        as_json=args.json,
        text=f"审计链：{'完整 ✓' if ok else '已断 ✗'} —— {note}",
    )
    return EXIT_OK if ok else EXIT_ERROR


def cmd_check(args: argparse.Namespace) -> int:
    """自证可用：在临时目录跑一遍完整生命周期。**不碰真实数据。**

    每一步只验**一件事**，并比对**具体结论码**——只断言"被拒"是不够的：
    曾出现过"白名单外拒绝"实际报的是 `EXHAUSTED`（因为用同一张
    `max_uses=1` 的授权跑了两步，第一次就把次数用完了），
    断言太松就查不出来。
    """
    from .verify import (
        ACTION_NOT_ALLOWED,
        EXHAUSTED,
        REVOKED,
        RESOURCE_NOT_ALLOWED,
        authorize,
    )

    with tempfile.TemporaryDirectory(prefix="authz-check-") as tmp:
        base = Path(tmp)
        store, audit = open_world(base)
        steps: list[tuple[str, bool, str]] = []

        def make(subject: str, **overrides) -> tuple:
            fields = {
                "issuer": "自检",
                "subject": subject,
                "actions": ("ping",),
                "resources": ("self",),
                "issued_at": now_utc(),
                "expires_at": in_hours(1),
            }
            fields.update(overrides)
            return store.issue(Grant(**fields))

        # 长期授权：用来验放行、越权、撤销（不掺次数限制）
        record, code = make("check-long")
        steps.append(("签发", bool(code), record.grant.grant_id))

        def ask(action: str, resource: str, grant_id=None, grant_code=code):
            return authorize(
                store,
                grant_id=grant_id or record.grant.grant_id,
                action=action,
                resource=resource,
                code=grant_code,
            )

        allowed = ask("ping", "self")
        steps.append(("白名单内放行", allowed.allowed, allowed.code))

        bad_action = ask("rm-rf", "self")
        steps.append(
            ("白名单外动作被拒", bad_action.code == ACTION_NOT_ALLOWED, bad_action.code)
        )

        bad_resource = ask("ping", "别人的台账")
        steps.append(
            ("白名单外资源被拒", bad_resource.code == RESOURCE_NOT_ALLOWED, bad_resource.code)
        )

        # 一次性授权：单独一张，避免把上面那步的次数吃掉
        one_shot, one_code = make("check-once", max_uses=1)
        first = ask("ping", "self", one_shot.grant.grant_id, one_code)
        steps.append(("一次性授权首次放行", first.allowed, first.code))
        reused = ask("ping", "self", one_shot.grant.grant_id, one_code)
        steps.append(
            ("一次性授权不可重用", reused.code == EXHAUSTED, reused.code)
        )

        store.revoke(record.grant.grant_id)
        after = ask("ping", "self")
        steps.append(("撤销后失效", after.code == REVOKED, after.code))

        chain_ok, chain_note = audit.verify()
        steps.append(("审计链完整", chain_ok, chain_note))

    failures = [name for name, ok, _ in steps if not ok]
    lines = [f"  {'✓' if ok else '✗'} {name:<22} {note}" for name, ok, note in steps]
    text = "\n".join(["自检（临时目录，不碰真实数据）：", *lines, ""])
    text += "全部通过 ✓" if not failures else f"失败 {len(failures)} 项：{'、'.join(failures)}"
    emit(
        {"ok": not failures, "steps": [{"name": n, "ok": o, "note": d} for n, o, d in steps]},
        as_json=args.json,
        text=text,
    )
    return EXIT_OK if not failures else EXIT_ERROR


# --- 写命令 ---------------------------------------------------------------


def cmd_issue(args: argparse.Namespace) -> int:
    base = resolve_base(args.base)
    store, _ = open_world(base)

    if args.expires_at:
        expires_at = args.expires_at
    else:
        expires_at = in_hours(args.hours)

    try:
        grant = Grant(
            issuer=args.issuer,
            subject=args.subject,
            actions=args.actions,
            resources=args.resources,
            issued_at=now_utc(),
            expires_at=expires_at,
            max_uses=args.max_uses,
            note=args.note or "",
        ).validate()
    except GrantError as exc:
        print(f"签发被拒：{exc}", file=sys.stderr)
        return EXIT_ERROR

    saved = backup(base, GRANTS_FILE)
    record, code = store.issue(grant, with_code=not args.no_code, actor=args.issuer)

    payload = {
        "grant_id": record.grant.grant_id,
        "code": code,
        "expires_at": record.grant.expires_at,
        "max_uses": record.grant.max_uses,
        "backup": str(saved) if saved else None,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return EXIT_OK

    print(f"已签发：{record.grant.grant_id}")
    print(f"  谁签的：{record.grant.issuer}")
    print(f"  给谁：  {record.grant.subject}")
    print(f"  做什么：{'、'.join(record.grant.actions)}")
    print(f"  对什么：{'、'.join(record.grant.resources)}")
    print(f"  有效期：到 {record.grant.expires_at}")
    print(f"  次数：  {'不限' if record.grant.max_uses is None else record.grant.max_uses}")
    if saved:
        print(f"  备份：  {saved}（回滚用）")
    if code:
        print()
        print("=" * OUTPUT_WIDTH)
        print(f"  授权码（只显示这一次，档案里只存哈希）：\n\n      {code}\n")
        print("  把它交给【执行器】，不要放进 AI 读得到的地方。")
        print("=" * OUTPUT_WIDTH)
    return EXIT_OK


def cmd_revoke(args: argparse.Namespace) -> int:
    if not args.yes:
        print(
            "拒绝执行：撤销是高风险操作，必须人类确认。\n"
            f"确认后请加 --yes：resident-authz revoke {args.grant_id} --yes",
            file=sys.stderr,
        )
        return EXIT_REFUSED

    base = resolve_base(args.base)
    store, _ = open_world(base)
    try:
        saved = backup(base, GRANTS_FILE)
        record = store.revoke(args.grant_id, actor=args.actor)
    except KeyError:
        print(f"档案里没有这个编号：{args.grant_id}", file=sys.stderr)
        return EXIT_ERROR

    text = f"已撤销：{record.grant.grant_id}（{record.revoked_at}）"
    if saved:
        text += f"\n备份：{saved}"
    emit({"grant_id": record.grant.grant_id, "revoked_at": record.revoked_at},
         as_json=args.json, text=text)
    return EXIT_OK


# --- 装配 -------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="resident-authz",
        description="② 档人类侧命令行：签发 / 撤销 / 查看（分级授权由代码强制）",
    )
    parser.add_argument("--base", help=f"数据目录（默认 ./{DEFAULT_BASE}）")
    parser.add_argument("--json", action="store_true", help="输出 JSON（给程序读）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="一条命令看清健康 [只读]").set_defaults(func=cmd_status)
    sub.add_parser("verify-chain", help="验证审计链是否被改过 [只读]").set_defaults(func=cmd_verify_chain)
    sub.add_parser("check", help="自证可用：临时目录跑完整生命周期 [只读]").set_defaults(func=cmd_check)

    p_list = sub.add_parser("list", help="列出授权 [只读]")
    p_list.add_argument("--all", action="store_true", help="包含已撤销的")
    p_list.set_defaults(func=cmd_list)

    p_audit = sub.add_parser("audit", help="看审计记录 [只读]")
    p_audit.add_argument("--tail", type=int, default=20, help="只看最近 N 条")
    p_audit.add_argument("--event", help="只看某类事件（issued/allowed/denied/revoked）")
    p_audit.set_defaults(func=cmd_audit)

    p_issue = sub.add_parser("issue", help="签发授权 [低风险写：自动备份]")
    p_issue.add_argument("--issuer", required=True, help="谁签的（人类身份）")
    p_issue.add_argument("--subject", required=True, help="给谁（员工/AI 身份）")
    p_issue.add_argument("--actions", required=True, help="做什么，逗号分隔（* = 全部）")
    p_issue.add_argument("--resources", required=True, help="对什么，逗号分隔（* = 全部）")
    p_issue.add_argument("--hours", type=float, default=24.0, help="有效期小时数（默认 24）")
    p_issue.add_argument("--expires-at", help="直接指定到期时间（ISO-8601），覆盖 --hours")
    p_issue.add_argument("--max-uses", type=int, help="次数上限（不填 = 有效期内不限）")
    p_issue.add_argument("--no-code", action="store_true", help="不发授权码（③ 签名档用）")
    p_issue.add_argument("--note", help="备注（给人看，不进签名）")
    p_issue.set_defaults(func=cmd_issue)

    p_revoke = sub.add_parser("revoke", help="撤销授权 [高风险写：必须 --yes]")
    p_revoke.add_argument("grant_id", help="授权编号")
    p_revoke.add_argument("--yes", action="store_true", help="人类已确认（缺这个会被拒绝）")
    p_revoke.add_argument("--actor", default="人类", help="谁撤销的")
    p_revoke.set_defaults(func=cmd_revoke)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:  # 兜底：别把栈糊到使用者脸上
        print(f"出错了：{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
