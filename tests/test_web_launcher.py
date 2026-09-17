"""前台启动入口的测试。

`__main__.py` 是用户真正会跑的那条路（`python -m resident_employee.web`），
之前是 0% 覆盖。**用户会走的路径没人测**，比某个函数没覆盖更值得补——
因为那条路上的错，只有用户会撞见。

这里不测 `serve()`（它会一直阻塞），测的是它前面那一段：
参数怎么变成应用、引擎怎么选、配置从哪来。
"""

from __future__ import annotations

import argparse
import asyncio

import pytest

from resident_employee.runtime.gates import GateChain, ToolScopeGate
from resident_employee.web import AppConfig
from resident_employee.web.__main__ import TrialEngine, build_app
from resident_employee.web.server import DEFAULT_CONFIG_FILE


def ns(**overrides) -> argparse.Namespace:
    base = {
        "data": None,
        "employee": None,
        "no_engine": False,
        "trial_engine": False,
        "port": 8765,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_trial_engine_is_selectable(tmp_path):
    app = build_app(ns(data=str(tmp_path), trial_engine=True))
    assert app.engine_factory is TrialEngine


def test_no_engine_leaves_factory_empty(tmp_path):
    """不装 SDK 也要能起服务看页面——引擎缺席不等于服务起不来。"""
    app = build_app(ns(data=str(tmp_path), no_engine=True))
    assert app.engine_factory is None


def test_employee_override_wins(tmp_path):
    app = build_app(ns(data=str(tmp_path), no_engine=True, employee="some/employee.json"))
    assert app.config.employee_file == "some/employee.json"


def test_config_is_read_from_the_data_dir(tmp_path):
    AppConfig(max_steps=9, model="x-model").save(tmp_path / DEFAULT_CONFIG_FILE)

    app = build_app(ns(data=str(tmp_path), no_engine=True))

    assert app.config.max_steps == 9
    assert app.config.model == "x-model"


def test_missing_config_falls_back_to_defaults(tmp_path):
    app = build_app(ns(data=str(tmp_path), no_engine=True))
    assert app.config.max_steps == 20


def test_data_dir_is_created(tmp_path):
    target = tmp_path / "还没建的目录"
    build_app(ns(data=str(target), no_engine=True))
    assert target.is_dir()


# --- 试用引擎 ---------------------------------------------------------------


def test_trial_engine_goes_through_the_gates(tmp_path):
    """**试用引擎也必须真过闸门**，不能绕过它直接吐事件。

    否则用 `--trial-engine` 试用时看到的就不是真路径——"过程可见"和"被拦"
    都成了演示效果，验不出问题。
    """
    chain = GateChain([ToolScopeGate(["Read"])])
    engine = TrialEngine()

    async def collect():
        return [e async for e in engine.run(_request(), chain)]

    events = asyncio.run(collect())

    kinds = [e.kind for e in events]
    assert "tool_use" in kinds
    assert "done" in kinds

    denied = [e for e in events if e.kind == "tool_result" and e.meta.get("denied")]
    assert denied, "试用引擎也要能在闸门下产生被拦事件"


def test_trial_engine_flags_denials_with_gate(tmp_path):
    """被拦事件要带上是哪道闸拦的——前台靠它分「可批准」和「批准也没用」。"""
    chain = GateChain([ToolScopeGate(["Read", "Bash"])])
    engine = TrialEngine()

    async def collect():
        return [e async for e in engine.run(_request(), chain)]

    denied = [
        e for e in asyncio.run(collect())
        if e.kind == "tool_result" and e.meta.get("denied")
    ]

    assert denied
    assert all(e.meta.get("gate") for e in denied)


def _request():
    from resident_employee.runtime.ports import TurnRequest

    return TurnRequest(prompt="试用", session="trial")
