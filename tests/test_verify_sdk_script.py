"""守护 verify_sdk.py 的靶子文件。

**为什么需要这条测试**：关二拿一个文件当靶子让模型去 Read。曾经用的是临时探针
`_probe_sdk.py`，改名收尾时把它删了——结果这一关会报「文件不存在」，
而不是真的走一遍工具调用，**命门那一关给出假阴性**。

假阴性比报错更危险：它看起来像"通过了"，实际什么都没测。
所以靶子必须由测试守着。
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "verify_sdk.py"


def _read_constant(name: str) -> str:
    source = SCRIPT.read_text(encoding="utf-8")
    match = re.search(rf'^{name} = "([^"]+)"', source, re.MULTILINE)
    assert match, f"verify_sdk.py 里找不到常量 {name}"
    return match.group(1)


def test_target_file_exists():
    target = _read_constant("TARGET_FILE")
    path = ROOT / target
    assert path.exists(), (
        f"关二的靶子文件 {target} 不存在——这一关会报「文件不存在」而不是真的读，"
        f"给出假阴性。请改成仓库里稳定存在的文件，或把该文件加回来。"
    )


def test_target_contains_expected_string():
    """靶子里的内容要和 EXPECTED_IN_TARGET 对得上，否则判分会永远不过。"""
    target = _read_constant("TARGET_FILE")
    expected = _read_constant("EXPECTED_IN_TARGET")

    text = (ROOT / target).read_text(encoding="utf-8")

    assert expected in text, f"{target} 里没有 {expected!r}，关二的判分会永远不过"


def test_script_starts_with_utf8_reconfigure():
    """Windows 控制台默认 GBK，中文输出会乱码——脚本必须自己切 UTF-8。"""
    source = SCRIPT.read_text(encoding="utf-8")
    assert "reconfigure(encoding=" in source
