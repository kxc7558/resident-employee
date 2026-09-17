"""网页前台测试：会话存储 + HTTP 往返 + 流式 + 审批。

**全都不需要真引擎**——`EmployeeApp` 的引擎是注入的，测试里换成假引擎，
所以整套前台的行为都能在这里验（不起进程、不花钱、不依赖网络）。

HTTP 部分是**真起一个服务**再用 urllib 打它：路由、分块编码、流式协议
这些东西，只有真过一遍网络栈才算验过。
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
from fakes import FakeEngine

from resident_employee.web import (
    ROLE_USER,
    AppConfig,
    EmployeeApp,
    Message,
    SessionNotFound,
    SessionStore,
    make_handler,
)

EMPLOYEE = {
    "name": "测试员工",
    "system": "这是个测试系统。",
    "actions": "`ping` 看看通不通。",
    "boundaries": "只读直接做。",
    "pitfalls": "没有坑。",
    "triage": "先 ping。",
    "output_format": "一句话说结论。",
}


# ===== 会话存储 =============================================================


@pytest.fixture
def store(tmp_path) -> SessionStore:
    return SessionStore(tmp_path / "sessions.json")


def test_create_and_list(store):
    s = store.create()
    assert s.session_id.startswith("s-")
    assert [x.session_id for x in store.list()] == [s.session_id]


def test_title_comes_from_first_prompt(store):
    """拿第一句话当标题——比"未命名会话"有用得多。"""
    s = store.create()
    store.append(s.session_id, Message(role=ROLE_USER, text="帮我看看代理池的健康状态怎么样了呢"))

    assert store.get(s.session_id).title.startswith("帮我看看代理池")


def test_long_title_is_truncated(store):
    s = store.create()
    store.append(s.session_id, Message(role=ROLE_USER, text="啊" * 100))
    assert store.get(s.session_id).title.endswith("…")


def test_employee_message_does_not_set_title(store):
    s = store.create()
    store.append(s.session_id, Message(role="employee", text="我先看看"))
    assert store.get(s.session_id).title == "新会话"


def test_messages_persist_across_reload(tmp_path):
    path = tmp_path / "sessions.json"
    first = SessionStore(path)
    s = first.create()
    first.append(s.session_id, Message(role=ROLE_USER, text="你好"))

    second = SessionStore(path)

    assert second.get(s.session_id).messages[0].text == "你好"


def test_steps_survive_reload(tmp_path):
    """过程要留得住——刷新页面后还得看得见"它当时干了什么"。"""
    from resident_employee.web import Step

    path = tmp_path / "sessions.json"
    first = SessionStore(path)
    s = first.create()
    first.append(
        s.session_id,
        Message(role="employee", text="好了", steps=[Step(kind="tool", tool="Read", ok=True)]),
    )

    reloaded = SessionStore(path).get(s.session_id)

    assert reloaded.messages[0].steps[0].tool == "Read"


def test_missing_session_raises(store):
    with pytest.raises(SessionNotFound):
        store.get("s-不存在")


def test_ensure_creates_with_the_given_id(store):
    """给定了编号就用给定的那个——另生成随机号会造出孤儿会话。"""
    assert store.ensure("s-新").session_id == "s-新"


def test_ensure_with_blank_id_makes_a_fresh_one(store):
    """空编号 = "开个新会话"，这时才该生成随机号。"""
    created = store.ensure("")
    assert created.session_id.startswith("s-")
    assert created.session_id != ""


def test_delete(store):
    s = store.create()
    store.delete(s.session_id)
    assert store.list() == []


def test_delete_missing_raises(store):
    with pytest.raises(SessionNotFound):
        store.delete("s-不存在")


def test_rename(store):
    s = store.create()
    assert store.rename(s.session_id, "换个名字").title == "换个名字"


def test_last_prompt(store):
    s = store.create()
    store.append(s.session_id, Message(role=ROLE_USER, text="第一问"))
    store.append(s.session_id, Message(role="employee", text="答"))
    store.append(s.session_id, Message(role=ROLE_USER, text="第二问"))

    assert store.last_prompt(s.session_id) == "第二问"


def test_resume_roundtrip(store):
    s = store.create()
    store.set_resume(s.session_id, "sess-xyz")
    assert store.get(s.session_id).resume == "sess-xyz"


# ===== HTTP ================================================================


@pytest.fixture
def live(tmp_path):
    """真起一个服务，用完关掉。"""
    employee_file = tmp_path / "employee.json"
    employee_file.write_text(json.dumps(EMPLOYEE, ensure_ascii=False), encoding="utf-8")

    app = EmployeeApp(
        data_dir=tmp_path / "data",
        # Bash **在**工具面里、但没授权（卡在授权闸门）；Write 不在工具面里。
        # 这两种被拦的原因不同，前台要分开报——所以夹具必须各留一个。
        config=AppConfig(employee_file=str(employee_file), allowed_tools=("Read", "Bash")),
        engine_factory=lambda: FakeEngine(
            [
                ("tool", "Bash", {"command": "node x.js status"}),   # 缺授权 → 可批准
                ("tool", "Write", {"file_path": "x"}),               # 不在工具面 → 批准也没用
                ("text", "这件事我做不了。"),
                ("done",),
            ]
        ),
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}", app
    finally:
        httpd.shutdown()
        httpd.server_close()


def get(url: str):
    with urllib.request.urlopen(url, timeout=10) as res:
        return res.status, res.read().decode("utf-8")


def post(url: str, payload: dict):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=10) as res:
        return res.status, json.loads(res.read().decode("utf-8"))


def test_index_is_served(live):
    base, _ = live
    status, body = get(base + "/")

    assert status == 200
    assert "<!DOCTYPE html>" in body
    assert "数字员工" in body


def test_status_reports_health(live):
    base, _ = live
    _, body = get(base + "/api/status")
    data = json.loads(body)["data"]

    assert data["employee"] == "测试员工"
    assert data["chain_ok"] is True


def test_session_crud_over_http(live):
    base, _ = live
    _, created = post(base + "/api/sessions", {})
    sid = created["data"]["session_id"]

    _, listed = get(base + "/api/sessions")
    assert [s["session_id"] for s in json.loads(listed)["data"]] == [sid]

    req = urllib.request.Request(base + "/api/sessions/" + sid, method="DELETE")
    with urllib.request.urlopen(req, timeout=10) as res:
        assert res.status == 200

    _, after = get(base + "/api/sessions")
    assert json.loads(after)["data"] == []


def test_ask_streams_ndjson(live):
    """流式：拉到的每一行都是一个 JSON 帧。"""
    base, _ = live
    _, created = post(base + "/api/sessions", {})
    sid = created["data"]["session_id"]

    frames = _collect_ask(base, sid, "改一下配置")

    kinds = [f["type"] for f in frames]
    assert "tool" in kinds, "过程可见：工具调用要推过来"
    assert "denied" in kinds, "被拦的也要推"
    assert kinds[-1] == "result"


def test_ask_result_separates_approvable_from_hard_blocked(live):
    """**真跑才发现的一条**：只有「授权」闸门拦下的动作，批准才可能放行。

    工具面 / 循环检测拦下的是结构性边界——批准它没用，重跑还是会被同一道闸拦住。
    给用户一个点了没用的按钮比不给更糟，所以要分开报。
    """
    base, _ = live
    _, created = post(base + "/api/sessions", {})
    sid = created["data"]["session_id"]

    result = _collect_ask(base, sid, "改一下配置")[-1]

    assert result["text"] == "这件事我做不了。"
    assert result["needs_approval"] == ["Bash"], "缺授权的，可以批准"
    assert result["blocked_hard"] == ["Write"], "不在工具面的，批准也没用"
    assert "被拦" in result["brief"]


def test_approve_only_remembers_approvable_actions(live):
    """待确认列表里只有"缺授权"那种——那个批准也没用的不该混进来。"""
    base, app = live
    _, created = post(base + "/api/sessions", {})
    sid = created["data"]["session_id"]
    _collect_ask(base, sid, "改一下配置")

    session = app.sessions.get(sid)
    from resident_employee.web.approvals import confirmable_actions

    assert confirmable_actions(s.to_dict() for s in session.messages[-1].steps) == ["Bash"]


def test_ask_persists_messages_and_steps(live):
    base, _ = live
    _, created = post(base + "/api/sessions", {})
    sid = created["data"]["session_id"]
    _collect_ask(base, sid, "改一下配置")

    _, body = get(base + "/api/sessions/" + sid)
    messages = json.loads(body)["data"]["messages"]

    assert messages[0]["role"] == ROLE_USER
    assert messages[-1]["role"] == "employee"
    assert any(s["kind"] == "denied" for s in messages[-1]["steps"])


def test_approve_signs_one_time_grant_and_unblocks(live):
    """**这是「人在环」的核心一测**：确认执行 = 签一张只覆盖那个动作的一次性授权。"""
    base, app = live
    _, created = post(base + "/api/sessions", {})
    sid = created["data"]["session_id"]
    _collect_ask(base, sid, "改一下配置")

    # 确认前：没授权
    assert not app.approvals.decide("Bash", app.config.resource).allowed

    _, approved = post(base + "/api/approve", {"session": sid, "action": "Bash"})
    data = approved["data"]
    assert data["max_uses"] == 1
    assert data["action"] == ["Bash"]

    # 确认后：同一次动作放行，第二次就废了
    assert app.approvals.decide("Bash", app.config.resource).allowed
    assert not app.approvals.decide("Bash", app.config.resource).allowed


def test_approve_refuses_action_that_was_never_denied(live):
    """**不能"客户端说批什么就批什么"**——否则任何本机页面都能给自己签任意动作。

    只认会话记录里真的被授权闸门拦过的动作。
    """
    base, app = live
    _, created = post(base + "/api/sessions", {})
    sid = created["data"]["session_id"]
    _collect_ask(base, sid, "改一下配置")

    with pytest.raises(urllib.error.HTTPError) as excinfo:
        post(base + "/api/approve", {"session": sid, "action": "del-provider"})

    assert excinfo.value.code == 409
    assert not app.approvals.decide("del-provider", app.config.resource).allowed


def test_approve_refuses_hard_blocked_action(live):
    """Write 是被工具面拦的——批准也没用，服务端不该给它签授权。"""
    base, app = live
    _, created = post(base + "/api/sessions", {})
    sid = created["data"]["session_id"]
    _collect_ask(base, sid, "改一下配置")

    with pytest.raises(urllib.error.HTTPError) as excinfo:
        post(base + "/api/approve", {"session": sid, "action": "Write"})

    assert excinfo.value.code == 409


def test_approve_needs_session_and_action(live):
    base, _ = live
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        post(base + "/api/approve", {"session": "s-x"})
    assert excinfo.value.code == 400


def test_confirmable_survives_reload(live):
    """**刷新后按钮还得在**——所以可确认的动作是从记录里推的，不是内存里的临时状态。

    早先版本把待确认存在内存里，一刷新"被拦"还在记录里、按钮却没了。
    """
    from resident_employee.web.approvals import blocked_hard, confirmable_actions

    base, app = live
    _, created = post(base + "/api/sessions", {})
    sid = created["data"]["session_id"]
    _collect_ask(base, sid, "改一下配置")

    # 换一个全新的 SessionStore 从磁盘读——模拟服务重启
    reloaded = SessionStore(app.data_dir / "sessions.json").get(sid)
    steps = [s.to_dict() for s in reloaded.messages[-1].steps]

    assert confirmable_actions(steps) == ["Bash"]
    assert blocked_hard(steps) == ["Write"]


def test_ask_rejects_empty_prompt(live):
    base, _ = live
    req = urllib.request.Request(
        base + "/api/ask",
        data=json.dumps({"prompt": "  "}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(req, timeout=10)

    assert excinfo.value.code == 400


# ===== 配置与安全 ==========================================================


def test_config_never_returns_the_token(live):
    """**token 只说有没有，不说是多少**——它会进浏览器、进缓存、进截图。"""
    base, app = live
    post(base + "/api/config", {"token": "超级机密的令牌"})

    _, body = get(base + "/api/config")
    text = body

    assert "超级机密的令牌" not in text
    assert json.loads(text)["data"]["has_token"] is True


def test_config_save_keeps_token_when_omitted(live):
    """前台从不回传 token，所以保存时不能因为"没传"就把它清掉。"""
    base, app = live
    post(base + "/api/config", {"token": "tok-1"})
    post(base + "/api/config", {"model": "some-model"})

    assert app.config.token == "tok-1"
    assert app.config.model == "some-model"


def test_config_roundtrips_tool_scope(live):
    base, app = live
    post(base + "/api/config", {"allowed_tools": ["Read", "Grep"], "max_steps": 7})

    assert app.config.allowed_tools == ("Read", "Grep")
    assert app.config.max_steps == 7


def test_unknown_path_is_404(live):
    base, _ = live
    # 路径必须走 ASCII：HTTP 请求行编不了非 ASCII，这是测试自己的约束
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(base + "/api/nope", timeout=10)
    assert excinfo.value.code == 404


# ===== 助手 =================================================================


def _collect_ask(base: str, session: str, prompt: str) -> list[dict]:
    """打 /api/ask，把 NDJSON 流读成帧列表。"""
    req = urllib.request.Request(
        base + "/api/ask",
        data=json.dumps({"session": session, "prompt": prompt}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    frames: list[dict] = []
    with urllib.request.urlopen(req, timeout=20) as res:
        assert res.headers.get("Content-Type", "").startswith("application/x-ndjson")
        for line in res.read().decode("utf-8").splitlines():
            line = line.strip()
            if line:
                frames.append(json.loads(line))
    return frames
