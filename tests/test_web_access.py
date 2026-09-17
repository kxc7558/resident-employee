"""HTTP 层面的准入门测试。

纯函数那层（`test_web_auth.py`）已经覆盖了各种组合；这里验的是**它真的接到了
路由上**——每个接口都过门，而不是只在某个函数里判了一下。

用 `http.client` 而不是 urllib：要看 302 和 `Set-Cookie` 的原始头，
urllib 会自动跟重定向、把中间那一步藏起来。
"""

from __future__ import annotations

import http.client
import json
import threading
import urllib.parse
from http.server import ThreadingHTTPServer

import pytest

from resident_employee.web import AppConfig, EmployeeApp
from resident_employee.web.approvals import CONFIRMABLE_GATE
from resident_employee.web.auth import COOKIE_NAME, HEADER_NAME, token_fingerprint
from resident_employee.web.server import make_handler

TOKEN = "host-shared-token-9f2c"
"""宿主令牌用 ASCII——HTTP 头的值只能走 latin-1，中文根本发不出去（见下）。"""

FP = token_fingerprint(TOKEN)

EMPLOYEE = {
    "name": "测试员工",
    "system": "s",
    "actions": "a",
    "boundaries": "b",
    "pitfalls": "p",
    "triage": "t",
    "output_format": "o",
}


@pytest.fixture
def locked(tmp_path):
    """配了宿主令牌的实例。"""
    employee_file = tmp_path / "employee.json"
    employee_file.write_text(json.dumps(EMPLOYEE, ensure_ascii=False), encoding="utf-8")
    app = EmployeeApp(
        data_dir=tmp_path / "data",
        config=AppConfig(employee_file=str(employee_file), host_token=TOKEN),
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield port, app
    finally:
        httpd.shutdown()
        httpd.server_close()


def call(port: int, method: str, path: str, *, headers=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, body=body, headers=headers or {})
    res = conn.getresponse()
    payload = res.read().decode("utf-8", "replace")
    head = {k.lower(): v for k, v in res.getheaders()}
    conn.close()
    return res.status, head, payload


def get(port, path, headers=None):
    return call(port, "GET", path, headers=headers)


# --- 配了令牌：什么都得过门 -------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/api/status", "/api/sessions", "/api/config", "/api/audit"])
def test_protected_paths_reject_without_token(locked, path):
    port, _ = locked
    status, _, _ = get(port, path)
    assert status == 401, f"{path} 没带令牌也该被拒"


@pytest.mark.parametrize("path", ["/api/ask", "/api/approve", "/api/config"])
def test_post_also_guarded(locked, path):
    port, _ = locked
    status, _, _ = call(port, "POST", path, body="{}",
                        headers={"Content-Type": "application/json"})
    assert status == 401


def test_delete_also_guarded(locked):
    port, _ = locked
    status, _, _ = call(port, "DELETE", "/api/sessions/s-x")
    assert status == 401


def test_header_token_allows(locked):
    port, _ = locked
    status, _, body = get(port, "/api/status", headers={HEADER_NAME: TOKEN})

    assert status == 200
    assert json.loads(body)["data"]["employee"] == "测试员工"


def test_wrong_token_still_rejected(locked):
    port, _ = locked
    status, _, _ = get(port, "/api/status", headers={HEADER_NAME: "guessed"})
    assert status == 401


def test_denial_message_tells_you_how_to_get_in(locked):
    port, _ = locked
    _, _, body = get(port, "/api/status")

    assert "?token=" in body, "光说「没授权」没用，要说清怎么进"


# --- ?token= 换 cookie ------------------------------------------------------


def test_page_accepts_query_token_and_sets_cookie(locked):
    port, _ = locked
    status, head, _ = get(port, "/?token=" + urllib.parse.quote(TOKEN))

    assert status == 302, "换到 cookie 后该重定向到干净地址——令牌不该留在 iframe 地址里"
    assert head["location"] == "/"
    cookie = head.get("set-cookie", "")
    assert f"{COOKIE_NAME}={FP}" in cookie
    assert "HttpOnly" in cookie, "HttpOnly 挡掉脚本读取"
    assert "SameSite=Lax" in cookie
    assert TOKEN not in cookie, "cookie 里放的是指纹，不是令牌本身"


def test_cookie_opens_apis(locked):
    port, _ = locked
    status, _, body = get(port, "/api/status", headers={"Cookie": f"{COOKIE_NAME}={FP}"})

    assert status == 200
    assert json.loads(body)["data"]["chain_ok"] is True


def test_wrong_query_token_rejected(locked):
    port, _ = locked
    status, _, _ = get(port, "/?token=" + urllib.parse.quote("假的"))
    assert status == 401


def test_page_rejects_when_no_token_at_all(locked):
    """页面本身也要过门——否则没令牌的人能打开界面（只是接口会失败，看着像坏了）。"""
    port, _ = locked
    status, _, _ = get(port, "/")
    assert status == 401


def test_embed_js_needs_no_token(locked):
    """嵌入脚本本身不设门槛：它只是个静态脚本，里面没有数据。

    真正的门在它加载的那个页面、以及页面上所有接口上。
    """
    port, _ = locked
    status, head, body = get(port, "/embed.js")

    assert status == 200
    assert "javascript" in head.get("content-type", "")
    assert "re-height" in body, "少了它对话台会被 iframe 截掉"


def test_cookie_does_not_leak_the_token(locked):
    port, app = locked
    status, _, body = get(port, "/api/config", headers={HEADER_NAME: TOKEN})

    assert TOKEN not in body
    assert json.loads(body)["data"]["has_host_token"] is True


def test_host_token_survives_config_save(locked):
    """前台从不回传宿主令牌，保存配置不该把它清掉。"""
    port, app = locked
    call(port, "POST", "/api/config", body=json.dumps({"model": "x"}),
         headers={"Content-Type": "application/json", HEADER_NAME: TOKEN})

    assert app.config.host_token == TOKEN
    assert app.config.model == "x"


# --- 有权限时功能照常 --------------------------------------------------------


def test_status_reports_alive_and_uptime(locked):
    port, app = locked
    app.heartbeat.start(port=port, employee="测试员工")

    _, _, body = get(port, "/api/status", headers={HEADER_NAME: TOKEN})
    data = json.loads(body)["data"]

    assert data["alive"] is True
    assert "uptime_s" in data


def test_confirmable_gate_name_is_stable(locked):
    """前台和测试都按这个字符串分类"被拦"，改它会静默破坏"确认执行"。"""
    assert CONFIRMABLE_GATE == "授权"


# --- 宿主令牌必须 ASCII -----------------------------------------------------


def test_non_ascii_host_token_is_rejected_at_config_time():
    """**在配置层拦住，别让它到 HTTP 层神秘失败。**

    HTTP 头的值只能走 latin-1——中文令牌连发都发不出去：
    客户端抛 `UnicodeEncodeError`，服务端收到的是残缺值。
    与其让人对着一个莫名的编码错误排查，不如设的时候就说清楚。
    """
    from resident_employee.web import AppConfig

    with pytest.raises(ValueError, match="ASCII"):
        AppConfig.from_dict({"host_token": "中文令牌"})


def test_config_api_returns_400_for_non_ascii_token(locked):
    port, _ = locked
    status, _, body = call(
        port, "POST", "/api/config",
        body=json.dumps({"host_token": "中文令牌"}),
        headers={"Content-Type": "application/json", HEADER_NAME: TOKEN},
    )

    assert status == 400
    assert "ASCII" in body


def test_ascii_token_with_symbols_is_fine():
    from resident_employee.web import AppConfig

    config = AppConfig.from_dict({"host_token": "aZ09-_./+="})
    assert config.host_token == "aZ09-_./+="


def test_empty_host_token_is_still_standalone():
    from resident_employee.web import AppConfig

    assert AppConfig.from_dict({"host_token": ""}).host_token == ""
