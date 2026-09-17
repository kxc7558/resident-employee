"""网页前台的服务端：HTTP 路由 + 流式事件 + 托一个单文件页面。

**只用标准库**（`http.server` + `ThreadingHTTPServer`），不引 FastAPI——
这样前台是零额外依赖的。

数据与业务在 `app.py`，配置在 `config.py`；这里只管"请求怎么进、怎么出"。

流式用 **NDJSON**（一行一个 JSON）而不是 SSE：浏览器端用
`fetch(...).getReader()` 读，比 `EventSource` 两端都简单，也不必迁就它的重连语义。

**只绑 127.0.0.1**。这是本机工具，不是给人从公网访问的。要对外必须先加认证。
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from ..runtime import KIND_TOOL_RESULT
from .app import EmployeeApp, confirmable_in_session, step_from_dict
from .approvals import blocked_hard, confirmable_actions
from .auth import COOKIE_NAME, HEADER_NAME, access_from_query_token, check_access
from .config import AppConfig
from .sessions import ROLE_EMPLOYEE, ROLE_USER, Message

STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_CONFIG_FILE = "web-config.json"
STREAM_SENTINEL = "\x00__end__"
"""流结束的哨兵。用不可打印字符，免得跟正常内容撞上。"""


# --- 请求处理 ---------------------------------------------------------------


def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or 0)
    if not length:
        return {}
    raw = handler.rfile.read(length)
    try:
        data = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def make_handler(app: EmployeeApp):
    """造一个绑定到 `app` 的请求处理器。"""

    class Handler(BaseHTTPRequestHandler):
        server_version = "resident-employee"
        protocol_version = "HTTP/1.1"

        # --- 工具 -------------------------------------------------------

        def log_message(self, fmt: str, *args: Any) -> None:  # 别把访问日志打到 stderr
            return

        def _send_json(self, payload: Any, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_error_json(self, message: str, status: int = 400) -> None:
            self._send_json({"ok": False, "error": message}, status)

        def _send_file(self, path: Path, content_type: str) -> None:
            if not path.exists():
                self._send_error_json(f"找不到 {path.name}", 404)
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _chunk(self, text: str) -> None:
            data = text.encode("utf-8")
            self.wfile.write(f"{len(data):X}\r\n".encode("ascii") + data + b"\r\n")
            self.wfile.flush()

        def _begin_stream(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

        # --- 准入门（认证归宿主系统，见 auth.py）-----------------------------

        def _access(self):
            raw_cookie = self.headers.get("Cookie") or ""
            jar = SimpleCookie()
            try:
                jar.load(raw_cookie)
            except Exception:  # 畸形 cookie 不该把请求打挂
                jar = SimpleCookie()
            morsel = jar.get(COOKIE_NAME)
            return check_access(
                client_host=self.client_address[0] if self.client_address else "",
                header_token=self.headers.get(HEADER_NAME),
                cookie_value=morsel.value if morsel else "",
                host_token=app.config.host_token,
            )

        def _guard(self) -> bool:
            # 有请求就说明还活着——顺手更新心跳（内部节流，不会每请求都写盘）
            app.heartbeat.touch()
            access = self._access()
            if access.allowed:
                return True
            self._send_error_json(access.reason, 401)
            return False

        # --- 路由 -------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path

            if path == "/embed.js":
                # 嵌入脚本本身不设门槛：它只是个静态脚本，里面没有数据。
                # 真正的门在它加载的那个页面、以及页面上所有接口上。
                self._send_file(
                    STATIC_DIR / "embed.js", "application/javascript; charset=utf-8"
                )
                return

            if path in ("/", "/index.html"):
                self._serve_page(parsed)
                return

            if not self._guard():
                return

            if path == "/api/status":
                self._send_json({"ok": True, "data": app.status()})
            elif path == "/api/sessions":
                self._send_json(
                    {
                        "ok": True,
                        "data": [s.to_dict(with_messages=False) for s in app.sessions.list()],
                    }
                )
            elif path.startswith("/api/sessions/"):
                self._session_detail(path.rsplit("/", 1)[-1])
            elif path == "/api/config":
                self._send_json({"ok": True, "data": app.config.to_public_dict()})
            elif path == "/api/audit":
                tail = int(parse_qs(parsed.query).get("tail", ["30"])[0])
                self._send_json({"ok": True, "data": [e.to_dict() for e in app.audit.tail(tail)]})
            else:
                self._send_error_json("没有这个路径", 404)

        def _serve_page(self, parsed) -> None:
            """页面本身也要过门——否则没令牌的人能打开界面（只是接口会失败）。

            带 `?token=` 时校验并把令牌换成 **HttpOnly cookie**，然后重定向到
            干净地址：令牌不该留在 iframe 的地址里（会进历史记录、进截图）。
            """
            raw_token = (parse_qs(parsed.query).get("token") or [""])[0]
            if raw_token and app.config.host_token:
                exchanged = access_from_query_token(raw_token, app.config.host_token)
                if not exchanged.allowed:
                    self._send_error_json(exchanged.reason, 401)
                    return
                self.send_response(302)
                self.send_header("Location", parsed.path or "/")
                self.send_header(
                    "Set-Cookie",
                    f"{COOKIE_NAME}={exchanged.set_cookie}; Path=/; HttpOnly; SameSite=Lax",
                )
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            if not self._guard():
                return
            self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")

        def _session_detail(self, session_id: str) -> None:
            try:
                session = app.sessions.get(session_id)
            except KeyError:
                self._send_error_json("没有这个会话", 404)
                return
            self._send_json({"ok": True, "data": session.to_dict()})

        def do_POST(self) -> None:  # noqa: N802
            if not self._guard():
                return
            path = urlparse(self.path).path
            if path == "/api/ask":
                self._ask()
            elif path == "/api/sessions":
                body = _read_json(self)
                session = app.sessions.create(str(body.get("title", "")))
                self._send_json({"ok": True, "data": session.to_dict(with_messages=False)})
            elif path == "/api/approve":
                self._approve()
            elif path == "/api/config":
                self._save_config()
            else:
                self._send_error_json("没有这个路径", 404)

        def do_DELETE(self) -> None:  # noqa: N802
            if not self._guard():
                return
            path = urlparse(self.path).path
            if path.startswith("/api/sessions/"):
                session_id = path.rsplit("/", 1)[-1]
                try:
                    app.sessions.delete(session_id)
                except KeyError:
                    self._send_error_json("没有这个会话", 404)
                    return
                self._send_json({"ok": True})
            else:
                self._send_error_json("没有这个路径", 404)

        # --- 核心：跑一轮，边跑边推 -------------------------------------

        def _ask(self) -> None:
            body = _read_json(self)
            prompt = str(body.get("prompt", "")).strip()
            session_id = str(body.get("session", ""))
            if not prompt:
                self._send_error_json("prompt 不能为空")
                return

            session = app.sessions.ensure(session_id)
            session_id = session.session_id
            app.sessions.append(session_id, Message(role=ROLE_USER, text=prompt))

            events: queue.Queue[Any] = queue.Queue()
            steps: list[dict[str, Any]] = []

            async def push(event) -> None:
                """过程可见：每个事件当场推给前台，不等跑完。"""
                if event.kind == "tool_use":
                    # 记住最近一次工具调用：工具结果事件本身不带工具名，
                    # 被拦时要靠它才能说清「是哪个动作被拦了」
                    last_tool["name"] = event.tool
                    steps.append({"kind": "tool", "tool": event.tool, "args": dict(event.args), "ok": True})
                    events.put({"type": "tool", "tool": event.tool, "args": dict(event.args)})
                elif event.kind == KIND_TOOL_RESULT and event.meta.get("denied"):
                    reason = str(event.meta.get("reason", ""))
                    tool = last_tool.get("name", "")
                    steps.append(
                        {
                            "kind": "denied",
                            "tool": tool,
                            "reason": reason,
                            "gate": event.meta.get("gate", ""),
                            "ok": False,
                        }
                    )
                    events.put(
                        {
                            "type": "denied",
                            "tool": tool,
                            "reason": reason,
                            "gate": event.meta.get("gate", ""),
                        }
                    )
                elif event.kind in ("text", "thinking"):
                    events.put({"type": event.kind, "text": event.text})
                elif event.kind == "done":
                    events.put({"type": "done", "cost": event.cost_usd})
                elif event.kind == "error":
                    events.put({"type": "error", "text": event.text})

            last_tool: dict[str, str] = {}

            def work() -> None:
                try:
                    runner = app.build_runner()
                    result = asyncio.run(
                        runner.run_turn(
                            prompt, session=session_id, resume=session.resume, on_event=push
                        )
                    )
                    if result.resume:
                        app.sessions.set_resume(session_id, result.resume)
                    steps.append({"kind": "text", "text": result.text, "ok": True})

                    # **只有「授权」闸门拦下的动作，确认执行才可能放行。**
                    # 工具面/循环检测拦下的是结构性边界——批准它没用，
                    # 重跑还是会被同一道闸拦住。给用户一个点了没用的按钮比不给更糟。
                    approvable = confirmable_actions(steps)
                    hard = blocked_hard(steps)

                    app.sessions.append(
                        session_id,
                        Message(
                            role=ROLE_EMPLOYEE,
                            text=result.text,
                            steps=[step_from_dict(s) for s in steps],
                            stop_reason=result.stop_reason,
                            cost_usd=result.cost_usd,
                        ),
                    )
                    events.put(
                        {
                            "type": "result",
                            "text": result.text,
                            "brief": result.brief(),
                            "stop_reason": result.stop_reason,
                            "needs_approval": approvable,
                            "blocked_hard": hard,
                        }
                    )
                except Exception as exc:  # 引擎崩了也要让前台看到原因
                    events.put({"type": "error", "text": f"{type(exc).__name__}: {exc}"})
                finally:
                    events.put(STREAM_SENTINEL)

            threading.Thread(target=work, daemon=True).start()

            self._begin_stream()
            while True:
                item = events.get()
                if item == STREAM_SENTINEL:
                    break
                self._chunk(json.dumps(item, ensure_ascii=False) + "\n")
            self._chunk("")  # 结束块

        def _approve(self) -> None:
            body = _read_json(self)
            session_id = str(body.get("session", ""))
            action = str(body.get("action", ""))
            if not session_id or not action:
                self._send_error_json("要指定 session 和 action", 400)
                return

            try:
                session = app.sessions.get(session_id)
            except KeyError:
                self._send_error_json("没有这个会话", 404)
                return

            # **只认会话记录里真的被拦过的动作。**
            # 不能"客户端说批什么就批什么"——否则任何本机页面都能给自己
            # 签一个从没被拦过的动作。这条校验同时也是「刷新后按钮还在」的实现：
            # 可确认的动作是从记录里推出来的，不是内存里的临时状态。
            if action not in confirmable_in_session(session):
                self._send_error_json(
                    f"这个会话的最近一轮里，没有「{action}」被授权闸门拦下的记录", 409
                )
                return

            grant, _code = app.approvals.sign(action, note=f"前台确认执行：{action}")
            self._send_json(
                {
                    "ok": True,
                    "data": {
                        "grant_id": grant.grant_id,
                        "action": list(grant.actions),
                        "max_uses": grant.max_uses,
                        "expires_at": grant.expires_at,
                        "message": "已签一次性授权（只覆盖这一个动作，用一次即废）。请重新发送刚才那句话。",
                    },
                }
            )

        def _save_config(self) -> None:
            body = _read_json(self)
            merged = app.config.to_public_dict()
            merged.pop("has_token", None)
            merged.pop("has_host_token", None)
            merged.update({k: v for k, v in body.items() if not k.startswith("has_")})
            # 没传就保留原来的——前台从不回传这两个，不该因为"没传"把它们清掉
            if not body.get("token"):
                merged["token"] = app.config.token
            if not body.get("host_token"):
                merged["host_token"] = app.config.host_token
            try:
                app.config = AppConfig.from_dict(merged)
            except ValueError as exc:
                # 配置不合格就说清楚哪里不合格，别让它到请求层才神秘失败
                self._send_error_json(str(exc), 400)
                return
            app.config.save(app.data_dir / DEFAULT_CONFIG_FILE)
            self._send_json({"ok": True, "data": app.config.to_public_dict()})

    return Handler


# --- 启动 -------------------------------------------------------------------


def serve(app: EmployeeApp, host: str = "127.0.0.1", port: int = 8765) -> None:
    """起服务。**默认只绑本机**——对话台是嵌在业务系统内部的，不单独对外。"""
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    try:
        employee = app.employee().name
    except FileNotFoundError:
        employee = ""
    app.heartbeat.start(port=port, employee=employee)

    print(f"前台已启动： http://{host}:{port}/")
    print(f"数据目录： {app.data_dir}")
    print(f"接入方式： {host}:{port}/embed.js（宿主页面引它）")
    print("按 Ctrl+C 停。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停。")
    finally:
        httpd.server_close()
        app.heartbeat.stop()  # 别留个"看着在岗其实已经关了"的状态文件
