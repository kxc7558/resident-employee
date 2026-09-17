"""CLI 的公共设施：退出码、数据目录解析、写前备份、输出。

抽出来单独放，是因为「命令怎么实现」和「命令跑在哪、怎么落盘、怎么输出」
是两件事——后者所有命令共用。
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .audit import AuditChain
from .store import GrantStore

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REFUSED = 2
"""拒绝执行（未取得人类确认）——与"出错"区分开，便于调用方判断。"""

DEFAULT_BASE = "data"
GRANTS_FILE = "grants.json"
AUDIT_FILE = "audit.jsonl"

OUTPUT_WIDTH = 72


# --- 基础设施 ---------------------------------------------------------------


def resolve_base(raw: str | None) -> Path:
    base = raw or os.environ.get("RESIDENT_EMPLOYEE_BASE") or DEFAULT_BASE
    return Path(base).expanduser().resolve()


def open_world(base: Path) -> tuple[GrantStore, AuditChain]:
    base.mkdir(parents=True, exist_ok=True)
    audit = AuditChain(base / AUDIT_FILE)
    store = GrantStore(base / GRANTS_FILE, audit)
    return store, audit


def backup(base: Path, filename: str) -> Path | None:
    """写前备份——能回滚。备份落在 ``<base>/backups/``，固定目录。"""
    source = base / filename
    if not source.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    store_dir = base / "backups"
    store_dir.mkdir(parents=True, exist_ok=True)
    target = store_dir / f"{source.stem}-{stamp}{source.suffix}"
    shutil.copy2(source, target)
    return target


def rule(title: str = "") -> None:
    print(("- " + title + " ") if title else "", end="")
    print("-" * max(4, OUTPUT_WIDTH - (len(title) + 3 if title else 0)))


def emit(payload: dict, *, as_json: bool, text: str) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2) if as_json else text)


