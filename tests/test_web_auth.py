"""准入门测试：**认证归宿主系统**。

对话台是嵌在业务系统内部的，用户已经登录过业务系统——我们不自己造账号体系，
只认宿主给的令牌。这套判断是纯函数，所以各种组合都能直接测。
"""

from __future__ import annotations

import pytest

from resident_employee.web.auth import (
    COOKIE_NAME,
    HEADER_NAME,
    access_from_query_token,
    check_access,
    is_loopback,
    token_fingerprint,
)

TOKEN = "宿主给的共享令牌"


def access(**kwargs):
    base = {"client_host": "127.0.0.1", "header_token": None, "cookie_value": None, "host_token": ""}
    base.update(kwargs)
    return check_access(**base)


# --- 独立模式（没配宿主令牌）------------------------------------------------


@pytest.mark.parametrize("host", ["127.0.0.1", "127.1.2.3", "::1", "::ffff:127.0.0.1", "localhost"])
def test_loopback_is_allowed_in_standalone_mode(host):
    assert access(client_host=host).allowed


@pytest.mark.parametrize("host", ["192.168.1.10", "10.0.0.5", "8.8.8.8", ""])
def test_remote_is_denied_in_standalone_mode(host):
    """**不给"没配令牌就谁都能连"这种默认。**"""
    decision = access(client_host=host)

    assert not decision.allowed
    assert "只接受本机" in decision.reason


def test_standalone_denial_explains_how_to_open_up():
    reason = access(client_host="192.168.1.10").reason
    assert "host_token" in reason
    assert "认证归宿主系统" in reason


def test_is_loopback_handles_none():
    assert not is_loopback(None)


# --- 令牌模式 ---------------------------------------------------------------


def test_correct_header_token_allows():
    """头里放**原令牌**（宿主手里就是它），不是指纹。"""
    assert access(host_token=TOKEN, header_token=TOKEN).allowed


def test_correct_cookie_token_allows():
    """cookie 里放**指纹**。两边分别比，别搞混。"""
    assert access(host_token=TOKEN, cookie_value=token_fingerprint(TOKEN)).allowed


def test_cookie_with_raw_token_is_not_enough():
    """cookie 里该是指纹；塞原令牌不算数（免得哪天有人以为随便哪种都行）。"""
    assert not access(host_token=TOKEN, cookie_value=TOKEN).allowed


def test_header_with_fingerprint_is_not_enough():
    assert not access(host_token=TOKEN, header_token=token_fingerprint(TOKEN)).allowed


def test_wrong_token_denied():
    assert not access(host_token=TOKEN, header_token="猜的").allowed


def test_non_ascii_token_does_not_crash():
    """**回归测试**：`compare_digest` 对 str 要求纯 ASCII，
    中文令牌会抛 TypeError（变成 500 而不是干净的 401）。比字节才对。
    """
    decision = access(host_token="中文令牌", header_token="中文令牌")
    assert decision.allowed

    wrong = access(host_token="中文令牌", header_token="另一个中文令牌")
    assert not wrong.allowed
    assert wrong.reason


def test_missing_token_denied_even_from_loopback():
    """配了令牌就**连本机也要带**——省得"本机免检"变成绕过口子。"""
    decision = access(host_token=TOKEN)

    assert not decision.allowed
    assert "?token=" in decision.reason, "拒绝理由要说清怎么拿到令牌"


def test_remote_with_correct_token_allows():
    assert access(client_host="192.168.1.10", host_token=TOKEN, header_token=TOKEN).allowed


def test_whitespace_is_tolerated():
    assert access(host_token=TOKEN, header_token=f"  {TOKEN}  ").allowed


def test_fingerprint_is_not_the_token():
    """**cookie 里放指纹，不放令牌本身**——cookie 会进浏览器存储、可能被截图导出。"""
    assert token_fingerprint(TOKEN) != TOKEN
    assert len(token_fingerprint(TOKEN)) == 64


def test_fingerprint_is_stable():
    assert token_fingerprint(TOKEN) == token_fingerprint(f"  {TOKEN}  ")


# --- ?token= 换 cookie ------------------------------------------------------


def test_query_token_exchanges_for_cookie():
    decision = access_from_query_token(TOKEN, TOKEN)

    assert decision.allowed
    assert decision.set_cookie == token_fingerprint(TOKEN), "cookie 值该是指纹"


def test_wrong_query_token_refused():
    assert not access_from_query_token("假的", TOKEN).allowed


def test_query_token_ignored_when_no_host_token():
    """没配令牌就没有"换 cookie"这回事——独立模式靠来源判断。"""
    assert not access_from_query_token(TOKEN, "").allowed


def test_empty_query_token_refused():
    assert not access_from_query_token("", TOKEN).allowed


def test_cookie_name_and_header_name_are_defined():
    assert COOKIE_NAME and HEADER_NAME
