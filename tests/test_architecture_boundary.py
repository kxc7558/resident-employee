"""守架构边界：运行时核心不许 import 任何 agent SDK。

**为什么要把这条做成测试**：一旦核心 import 了 SDK，它就没法脱离
"真起一个 agent 进程"来测——每次跑测试都要花真钱、起进程、依赖网络。
这不是洁癖，是**可测性的前提**。写在注释里挡不住人顺手 import，
所以要机器守着。

用 AST 而非 grep：文档字符串里出现 `claude_agent_sdk` 是正常的
（说明适配器在哪），grep 会误报。
"""

from __future__ import annotations

import ast
import pathlib

CORE = pathlib.Path(__file__).resolve().parent.parent / "src" / "resident_employee" / "runtime"
ADAPTERS = CORE / "adapters"
BANNED = ("claude_agent_sdk",)


def _imported_modules(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_core_does_not_import_any_sdk():
    offenders: list[str] = []
    for path in sorted(CORE.rglob("*.py")):
        if ADAPTERS in path.parents:
            continue
        for module in _imported_modules(path):
            if any(banned in module for banned in BANNED):
                offenders.append(f"{path.relative_to(CORE)} -> {module}")

    assert not offenders, (
        "运行时核心出现了 SDK import：" + "；".join(offenders)
        + "。核心必须不认识具体引擎——那是 adapters/ 的事，"
        "否则运行时就没法用假引擎测试了。"
    )


def test_adapter_does_import_the_sdk():
    """反向确认：适配器**必须**真的 import SDK，否则它就是个空壳。"""
    found = _imported_modules(ADAPTERS / "claude_agent_sdk.py")
    assert any("claude_agent_sdk" in module for module in found), (
        "适配器没有 import claude_agent_sdk——它应该是唯一 import 的地方"
    )


def test_adapter_import_is_guarded():
    """SDK 必须**可选导入**：没装它也要能用授权/审计部件。"""
    source = (ADAPTERS / "claude_agent_sdk.py").read_text(encoding="utf-8")
    assert "try:" in source and "ImportError" in source
    assert "HAVE_SDK" in source
