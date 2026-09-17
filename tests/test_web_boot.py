"""托管相关：一条命令查健康 + 开机自启脚本。

自启脚本的**编码是对的一半功能**：`.bat` 用 UTF-8 存，cmd 会按 GBK 解——
中文变乱码、严重时首字节被吃掉，报「'ho' 不是内部或外部命令」。踩过。
"""

from __future__ import annotations

import json
import time

import pytest

from resident_employee.web.__main__ import BAT_DEFAULT, main
from resident_employee.web.heartbeat import HEARTBEAT_FILE, Heartbeat


# --- 一条命令查健康 ---------------------------------------------------------


def test_check_reports_off_duty_when_never_started(tmp_path, capsys):
    code = main(["--check", "--data", str(tmp_path)])

    assert code == 1
    out = capsys.readouterr().out
    assert "离岗" in out
    assert HEARTBEAT_FILE in out, "要说清状态文件在哪，好排查"


def test_check_reports_on_duty_after_start(tmp_path, capsys):
    Heartbeat(tmp_path / HEARTBEAT_FILE).start(port=8765, employee="key-pool-operator")

    code = main(["--check", "--data", str(tmp_path)])

    assert code == 0
    out = capsys.readouterr().out
    assert "在岗" in out
    assert "key-pool-operator" in out


def test_check_does_not_start_a_server(tmp_path, capsys):
    """**一条命令能解决的事，不该顺带把服务起起来。**"""
    import threading

    before = threading.active_count()
    main(["--check", "--data", str(tmp_path)])
    assert threading.active_count() == before


# --- 开机自启脚本 -----------------------------------------------------------


def test_write_boot_windows_produces_gbk_crlf(tmp_path, capsys):
    out = tmp_path / "启动.bat"

    code = main([
        "--write-boot", "--boot-target", "windows",
        "--data", str(tmp_path), "--port", "8799", "--out", str(out),
    ])

    assert code == 0
    raw = out.read_bytes()

    assert b"\r\n" in raw, ".bat 必须 CRLF"
    assert b"\n\n" not in raw.replace(b"\r\n", b""), "不该混着裸 LF"
    text = raw.decode("gbk")           # 用 GBK 解得开，才说明存的是 GBK
    assert "chcp 936" in text
    assert "8799" in text
    assert "resident_employee.web" in text
    assert "pause" in text, "双击运行的窗口崩了要停住，否则看不见错误"


def test_write_boot_windows_reverse_proof(tmp_path):
    """反向确认：同样内容用 UTF-8 存，用 GBK 读就是乱码。

    注意是**乱码**，不是抛 `UnicodeDecodeError`——UTF-8 的中文字节序列
    往往是合法的 GBK 序列，解得出东西来，只是全错。所以这条断言的是
    "读不出原名"，而不是"解不开"。第一版就写成了 expecting raise，是错的。
    """
    out = tmp_path / "坏.bat"
    out.write_bytes("title 数字员工\r\n".encode("utf-8"))

    assert "数字员工" not in out.read_bytes().decode("gbk")


def test_write_boot_systemd_unit(tmp_path, capsys):
    out = tmp_path / "re.service"

    code = main([
        "--write-boot", "--boot-target", "systemd",
        "--data", str(tmp_path), "--host", "127.0.0.1", "--port", "8799", "--out", str(out),
    ])

    assert code == 0
    text = out.read_text(encoding="utf-8")

    assert "[Unit]" in text and "[Service]" in text and "[Install]" in text
    assert "ExecStart=" in text
    assert "8799" in text
    assert "Restart=on-failure" in text, "挂了要能自己起来，否则谈不上常驻"


def test_write_boot_systemd_prints_install_steps(tmp_path, capsys):
    main(["--write-boot", "--boot-target", "systemd", "--data", str(tmp_path),
          "--out", str(tmp_path / "re.service")])

    out = capsys.readouterr().out
    assert "systemctl" in out
    assert "/etc/systemd/system/" in out


def test_write_boot_does_not_start_a_server(tmp_path):
    main(["--write-boot", "--boot-target", "systemd", "--data", str(tmp_path),
          "--out", str(tmp_path / "re.service")])
    # 能跑到这里就说明没进 serve()（那个会阻塞）


def test_default_out_path_is_named_for_humans(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    main(["--write-boot", "--boot-target", "windows", "--data", str(tmp_path)])
    assert (tmp_path / BAT_DEFAULT).exists(), "默认文件名要让人一眼看懂是什么"
