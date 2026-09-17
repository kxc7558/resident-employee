"""人类侧 CLI 的测试。

重点验两条**由代码强制**的规矩（规范 `digital-employee.md` 验收单）：
- 高风险命令（revoke）不带 `--yes` 会被**拒绝**（真跑一次）
- 写操作有备份文件，能回滚
"""

from __future__ import annotations

import json

import pytest

from resident_employee.authz.cli import (
    EXIT_ERROR,
    EXIT_OK,
    EXIT_REFUSED,
    main,
)


@pytest.fixture
def base(tmp_path):
    return str(tmp_path / "data")


def run(base: str, *argv: str) -> int:
    return main(["--base", base, *argv])


# --- 只读命令 ---------------------------------------------------------------


def test_status_on_empty_base_says_zero(base, capsys):
    assert run(base, "status") == EXIT_OK
    out = capsys.readouterr().out
    assert "共 0 张" in out
    assert "链完整" in out


def test_check_passes_end_to_end(base, capsys):
    """自证可用：不碰真实数据，在临时目录跑完整生命周期。

    断言**具体结论码**而不只是"通过"——曾出现过"白名单外拒绝"实际报
    `EXHAUSTED`（用同一张一次性授权跑了两步，第一次就把次数用完），
    断言太松查不出来。
    """
    assert run(base, "check") == EXIT_OK
    out = capsys.readouterr().out

    assert "全部通过" in out
    assert "✗" not in out
    assert "ACTION_NOT_ALLOWED" in out, "白名单外动作必须报动作越权，不能报别的"
    assert "RESOURCE_NOT_ALLOWED" in out
    assert "EXHAUSTED" in out
    assert "REVOKED" in out


def test_verify_chain_detects_tampering(base, tmp_path, capsys):
    run(base, "issue", "--issuer", "张三", "--subject", "emp",
        "--actions", "ping", "--resources", "self", "--hours", "1")
    capsys.readouterr()

    audit_path = tmp_path / "data" / "audit.jsonl"
    rows = [json.loads(x) for x in audit_path.read_text(encoding="utf-8").splitlines()]
    rows[0]["actor"] = "李四"  # 事后改记录
    audit_path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True, separators=(",", ":")) for r in rows) + "\n",
        encoding="utf-8",
    )

    assert run(base, "verify-chain") == EXIT_ERROR
    assert "已断" in capsys.readouterr().out


# --- 签发（低风险写） -------------------------------------------------------


def test_issue_prints_code_once_and_stores_hash_only(base, tmp_path, capsys):
    assert run(base, "issue", "--issuer", "张三", "--subject", "key-pool-operator",
               "--actions", "add-alias,set-strategy", "--resources", "api-key-pool") == EXIT_OK

    out = capsys.readouterr().out
    assert "授权码（只显示这一次" in out

    on_disk = (tmp_path / "data" / "grants.json").read_text(encoding="utf-8")
    code = [line.strip() for line in out.splitlines() if line.strip().startswith("Ck") or len(line.strip()) == 32]
    for candidate in code:
        assert candidate not in on_disk


def test_issue_writes_a_backup(base, tmp_path, capsys):
    run(base, "issue", "--issuer", "张三", "--subject", "emp",
        "--actions", "ping", "--resources", "self", "--hours", "1")
    capsys.readouterr()
    run(base, "issue", "--issuer", "张三", "--subject", "emp2",
        "--actions", "ping", "--resources", "self", "--hours", "1")
    out = capsys.readouterr().out

    backups = list((tmp_path / "data" / "backups").glob("grants-*.json"))
    assert backups, "第二次写入前必须留下备份"
    assert "备份" in out


def test_issue_rejects_incomplete_grant(base, capsys):
    """六字段缺一个就签不出来——CLI 也不给"凑合"。"""
    assert run(base, "issue", "--issuer", "张三", "--subject", "",
               "--actions", "ping", "--resources", "self") == EXIT_ERROR
    assert "签发被拒" in capsys.readouterr().err


def test_issue_rejects_empty_actions(base, capsys):
    assert run(base, "issue", "--issuer", "张三", "--subject", "emp",
               "--actions", ",,", "--resources", "self") == EXIT_ERROR
    assert "actions 为空" in capsys.readouterr().err


def test_issue_json_output_is_machine_readable(base, capsys):
    assert run(base, "--json", "issue", "--issuer", "张三", "--subject", "emp",
               "--actions", "ping", "--resources", "self") == EXIT_OK

    payload = json.loads(capsys.readouterr().out)
    assert payload["grant_id"].startswith("g-")
    assert payload["code"]


def test_no_code_flag_for_signed_mode(base, capsys):
    assert run(base, "--json", "issue", "--issuer", "张三", "--subject", "emp",
               "--actions", "ping", "--resources", "self", "--no-code") == EXIT_OK
    assert json.loads(capsys.readouterr().out)["code"] == ""


# --- 撤销（高风险写）-------------------------------------------------------


def test_revoke_without_yes_is_refused_and_changes_nothing(base, tmp_path, capsys):
    """**代码闸门**：没有 --yes 直接拒，不给"再确认一下"的余地。"""
    run(base, "--json", "issue", "--issuer", "张三", "--subject", "emp",
        "--actions", "ping", "--resources", "self")
    grant_id = json.loads(capsys.readouterr().out)["grant_id"]

    assert run(base, "revoke", grant_id) == EXIT_REFUSED
    assert "必须人类确认" in capsys.readouterr().err

    grants = json.loads((tmp_path / "data" / "grants.json").read_text(encoding="utf-8"))
    assert grants["grants"][0]["revoked_at"] == "", "被拒的命令不许产生任何写入"


def test_revoke_with_yes_works(base, tmp_path, capsys):
    run(base, "--json", "issue", "--issuer", "张三", "--subject", "emp",
        "--actions", "ping", "--resources", "self")
    grant_id = json.loads(capsys.readouterr().out)["grant_id"]

    assert run(base, "revoke", grant_id, "--yes") == EXIT_OK

    grants = json.loads((tmp_path / "data" / "grants.json").read_text(encoding="utf-8"))
    assert grants["grants"][0]["revoked_at"]


def test_revoke_unknown_id_is_error_not_refusal(base, capsys):
    assert run(base, "revoke", "g-不存在", "--yes") == EXIT_ERROR
    assert "没有这个编号" in capsys.readouterr().err


# --- 查看 -------------------------------------------------------------------


def test_list_hides_revoked_unless_all(base, capsys):
    run(base, "--json", "issue", "--issuer", "张三", "--subject", "emp",
        "--actions", "ping", "--resources", "self")
    grant_id = json.loads(capsys.readouterr().out)["grant_id"]
    run(base, "revoke", grant_id, "--yes")
    capsys.readouterr()

    assert json.loads(_capture_json(base, capsys, "list"))["grants"] == []
    assert len(json.loads(_capture_json(base, capsys, "list", "--all"))["grants"]) == 1


def _capture_json(base: str, capsys, *argv: str) -> str:
    run(base, "--json", *argv)
    return capsys.readouterr().out


def test_audit_records_denials(base, capsys):
    """拒绝也进审计——只记成功的审计查不出"谁试过碰不该碰的"。"""
    from resident_employee.authz import AuditChain, GrantStore, authorize

    store = GrantStore(f"{base}/grants.json", AuditChain(f"{base}/audit.jsonl"))
    from resident_employee.authz.grant import Grant, in_hours, now_utc

    record, code = store.issue(
        Grant(issuer="张三", subject="emp", actions=("ping",),
              resources=("self",), issued_at=now_utc(), expires_at=in_hours(1))
    )
    authorize(store, grant_id=record.grant.grant_id, action="删库", resource="self", code=code)

    assert run(base, "--json", "audit", "--event", "denied") == EXIT_OK
    entries = json.loads(capsys.readouterr().out)["entries"]
    assert entries
    assert entries[-1]["detail"]["decision"] == "ACTION_NOT_ALLOWED"
