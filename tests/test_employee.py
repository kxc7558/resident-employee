"""员工定义测试。

守的是规范 `digital-employee.md` 那句「六段缺一段，员工就会瞎干」——
缺段必须**报错**，不能默默生成一段四不像的提示词。
"""

from __future__ import annotations

import json

import pytest

from resident_employee.runtime import SECTIONS, Employee, EmployeeError

FULL = {
    "name": "key-pool-operator",
    "system": "代理池在 127.0.0.1:8787，配置在 config.json。",
    "actions": "`status` 看健康；`keys` 看明细；`test` 自证可用。",
    "boundaries": "只读直接做；删除类要人确认。",
    "pitfalls": "同账号多 Key 不叠加额度。",
    "triage": "先 status → 再 keys → 再 logs。",
    "output_format": "四行：【做了什么】【结果】【验证】【影响】",
}


def test_complete_employee_validates():
    employee = Employee.from_dict(FULL).validate()
    assert employee.name == "key-pool-operator"


@pytest.mark.parametrize("missing_key", [key for key, _ in SECTIONS])
def test_each_missing_section_is_rejected(missing_key):
    """六段里**每一段**缺了都要被拦住——所以逐段参数化测，不抽查一段。"""
    data = dict(FULL)
    data.pop(missing_key)

    with pytest.raises(EmployeeError) as excinfo:
        Employee.from_dict(data).validate()

    assert "缺这几段" in str(excinfo.value)


def test_error_names_the_missing_sections():
    data = dict(FULL)
    data.pop("triage")
    data.pop("output_format")

    with pytest.raises(EmployeeError) as excinfo:
        Employee.from_dict(data).validate()

    message = str(excinfo.value)
    assert "⑤ 排障顺序" in message
    assert "⑥ 输出要求" in message


def test_name_is_mandatory_even_when_lenient():
    with pytest.raises(EmployeeError, match="名字"):
        Employee.from_dict({"name": "  "}).validate(require_all=False)


def test_lenient_mode_only_needs_name():
    Employee(name="半成品").validate(require_all=False)


def test_system_prompt_carries_all_six_sections():
    prompt = Employee.from_dict(FULL).validate().to_system_prompt()

    for _, label in SECTIONS:
        assert label in prompt
    assert "127.0.0.1:8787" in prompt
    assert "同账号多 Key 不叠加额度" in prompt


def test_system_prompt_scopes_the_employee():
    """「专属，不通用」是规范的核心理念——提示词里必须说清只管一个系统。"""
    prompt = Employee.from_dict(FULL).validate().to_system_prompt()
    assert "只管这一个系统" in prompt


def test_hard_rules_are_injected():
    """通用铁律不由员工自定——写死在运行时里，免得每个定义抄一遍还抄漏。"""
    prompt = Employee.from_dict(FULL).validate().to_system_prompt()

    assert "通用铁律" in prompt
    assert "不能只看" in prompt and "保存成功" in prompt
    assert "别人的业务数据只读" in prompt
    assert "不要试图绕过" in prompt
    assert "查不到就说查不到" in prompt


def test_persona_can_be_overridden():
    employee = Employee.from_dict({**FULL, "persona": "你是食堂的配菜师。"})
    assert employee.to_system_prompt().startswith("你是食堂的配菜师。")


def test_default_persona_uses_name():
    prompt = Employee.from_dict(FULL).to_system_prompt()
    assert "key-pool-operator" in prompt.splitlines()[0]


def test_extra_sections_are_appended():
    employee = Employee.from_dict({**FULL, "extra": {"本项目特有约定": "改配置前先备份。"}})

    prompt = employee.to_system_prompt()

    assert "本项目特有约定" in prompt
    assert "改配置前先备份" in prompt


def test_blank_extra_is_skipped():
    employee = Employee.from_dict({**FULL, "extra": {"空的": "   "}})
    assert "空的" not in employee.to_system_prompt()


def test_roundtrip_through_dict():
    employee = Employee.from_dict(FULL).validate()
    again = Employee.from_dict(employee.to_dict()).validate()
    assert again.to_system_prompt() == employee.to_system_prompt()


def test_from_file_json(tmp_path):
    path = tmp_path / "employee.json"
    path.write_text(json.dumps(FULL, ensure_ascii=False), encoding="utf-8")

    employee = Employee.from_file(path).validate()

    assert employee.name == "key-pool-operator"
    assert "代理池" in employee.system


def test_from_file_missing_raises():
    with pytest.raises(FileNotFoundError):
        Employee.from_file("不存在的员工.json")
