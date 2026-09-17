"""心跳与在岗状态测试。

**"在岗"的判据有两个，缺一不可**：状态文件够新 **且** 那个 pid 还活着。
只看文件的话，进程被强杀留下的陈旧文件会一直显示"在岗"——
那比没有状态更糟，因为它会让你以为没事。
"""

from __future__ import annotations

import json
import os
import time

import pytest

from resident_employee.web.heartbeat import (
    HEARTBEAT_FILE,
    Heartbeat,
    check_health,
    pid_alive,
)

DEAD_PID = 999_999_999
"""一个几乎不可能存在的 pid——用来验"进程没了"那条路。"""


@pytest.fixture
def hb(tmp_path) -> Heartbeat:
    return Heartbeat(tmp_path / HEARTBEAT_FILE, throttle_s=0.0)


# --- 写 -------------------------------------------------------------------


def test_start_writes_file(hb, tmp_path):
    hb.start(port=8765, employee="key-pool-operator")

    data = json.loads((tmp_path / HEARTBEAT_FILE).read_text(encoding="utf-8"))
    assert data["pid"] == os.getpid()
    assert data["port"] == 8765
    assert data["employee"] == "key-pool-operator"
    assert data["started_at"] > 0


def test_touch_updates_last_seen(hb):
    hb.start()
    before = hb.read()["last_seen"]
    time.sleep(0.02)

    hb.touch()

    assert hb.read()["last_seen"] > before


def test_touch_is_throttled(tmp_path):
    """每次请求都写盘没必要——本机盘也经不起长年累月地写。"""
    throttled = Heartbeat(tmp_path / HEARTBEAT_FILE, throttle_s=60.0)
    throttled.start()
    before = throttled.read()["last_seen"]

    throttled.touch()

    assert throttled.read()["last_seen"] == before, "节流期内不该写盘"


def test_touch_without_file_is_a_noop(hb):
    hb.touch()  # 没有状态文件时不该炸
    assert hb.read() is None


def test_stop_removes_file(hb, tmp_path):
    """主动下线要把文件删掉，别留个"看着在岗其实已经关了"的状态。"""
    hb.start()
    hb.stop()
    assert not (tmp_path / HEARTBEAT_FILE).exists()


def test_stop_is_idempotent(hb):
    hb.start()
    hb.stop()
    hb.stop()


def test_read_survives_broken_json(tmp_path):
    path = tmp_path / HEARTBEAT_FILE
    path.write_text("{不是 json", encoding="utf-8")
    assert Heartbeat(path).read() is None


# --- 判在岗 -----------------------------------------------------------------


def test_no_file_means_off_duty(tmp_path):
    health = check_health(tmp_path / HEARTBEAT_FILE)

    assert not health.alive
    assert health.label == "离岗"
    assert "没起过" in health.detail or "退出" in health.detail


def test_fresh_file_with_live_pid_means_on_duty(hb, tmp_path):
    hb.start(port=8765, employee="key-pool-operator")

    health = check_health(tmp_path / HEARTBEAT_FILE)

    assert health.alive
    assert health.label == "在岗"
    assert "key-pool-operator" in health.detail
    assert "8765" in health.detail


def test_dead_pid_means_off_duty(tmp_path):
    """**进程没了就是离岗**——哪怕文件还挺新（被强杀就是这个样子）。"""
    path = tmp_path / HEARTBEAT_FILE
    path.write_text(
        json.dumps({"pid": DEAD_PID, "started_at": time.time(), "last_seen": time.time()}),
        encoding="utf-8",
    )

    health = check_health(path)

    assert not health.alive
    assert "已经不在了" in health.detail


def test_stale_file_means_可能卡住(tmp_path):
    """进程还在但很久没动静——**别报"在岗"**，那是更危险的假象。"""
    path = tmp_path / HEARTBEAT_FILE
    path.write_text(
        json.dumps(
            {"pid": os.getpid(), "started_at": time.time() - 600, "last_seen": time.time() - 300}
        ),
        encoding="utf-8",
    )

    health = check_health(path, stale_after=90)

    assert not health.alive
    assert health.label == "疑似卡住"
    assert "没有活动" in health.detail


def test_uptime_is_reported(hb, tmp_path):
    hb.start()
    health = check_health(tmp_path / HEARTBEAT_FILE)
    assert health.uptime_s >= 0


def test_health_brief_is_one_line(hb, tmp_path):
    hb.start()
    assert "\n" not in check_health(tmp_path / HEARTBEAT_FILE).brief()


def test_pid_alive_for_self():
    assert pid_alive(os.getpid())


def test_pid_alive_for_bogus():
    assert not pid_alive(DEAD_PID)
    assert not pid_alive(0)
    assert not pid_alive(-1)
