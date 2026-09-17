"""会话存储：多会话 + 消息，落 JSON。

规范要求「消息存服务端（跨设备）」——所以不放 localStorage，落文件。

存的东西**故意只留必要字段**：

- 用户说了什么、员工回了什么
- **过程**：调了哪些工具、哪些被拦了（前台刷新后还得看得见"它当时干了什么"）
- 续接句柄（引擎给的 session_id），下一轮接着聊用

不留模型的原始消息流：那玩意儿又大又只在单轮里有意义。
"""

from __future__ import annotations

import json
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROLE_USER = "user"
ROLE_EMPLOYEE = "employee"

TITLE_MAX = 40


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def make_title(prompt: str) -> str:
    """拿第一句话当前 40 字当标题——比"未命名会话"有用得多。"""
    text = " ".join(prompt.split())
    return text[:TITLE_MAX] + ("…" if len(text) > TITLE_MAX else "")


@dataclass
class Step:
    """过程里的一步。**前台"过程可见"就是把这些画出来。**"""

    kind: str
    """`tool`（调用了工具）/ `denied`（被拦）/ `text`（说了句话）。"""

    tool: str = ""
    args: Mapping[str, Any] = field(default_factory=dict)
    ok: bool = True
    reason: str = ""
    gate: str = ""
    text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "tool": self.tool,
            "args": dict(self.args),
            "ok": self.ok,
            "reason": self.reason,
            "gate": self.gate,
            "text": self.text,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Step":
        return cls(
            kind=str(data.get("kind", "")),
            tool=str(data.get("tool", "")),
            args=dict(data.get("args") or {}),
            ok=bool(data.get("ok", True)),
            reason=str(data.get("reason", "")),
            gate=str(data.get("gate", "")),
            text=str(data.get("text", "")),
        )


@dataclass
class Message:
    role: str
    text: str
    at: str = field(default_factory=now_iso)
    steps: list[Step] = field(default_factory=list)
    stop_reason: str = ""
    cost_usd: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "text": self.text,
            "at": self.at,
            "steps": [s.to_dict() for s in self.steps],
            "stop_reason": self.stop_reason,
            "cost_usd": self.cost_usd,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Message":
        return cls(
            role=str(data.get("role", ROLE_EMPLOYEE)),
            text=str(data.get("text", "")),
            at=str(data.get("at", "")) or now_iso(),
            steps=[Step.from_dict(s) for s in (data.get("steps") or [])],
            stop_reason=str(data.get("stop_reason", "")),
            cost_usd=float(data.get("cost_usd", 0.0)),
        )


@dataclass
class Session:
    session_id: str
    title: str = ""
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    messages: list[Message] = field(default_factory=list)
    resume: str = ""
    """引擎给的续接句柄。丢了就换新会话，不会串到别人的上下文里。"""

    def to_dict(self, *, with_messages: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "session_id": self.session_id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "message_count": len(self.messages),
        }
        if with_messages:
            data["messages"] = [m.to_dict() for m in self.messages]
            data["resume"] = self.resume
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Session":
        return cls(
            session_id=str(data["session_id"]),
            title=str(data.get("title", "")),
            created_at=str(data.get("created_at", "")) or now_iso(),
            updated_at=str(data.get("updated_at", "")) or now_iso(),
            messages=[Message.from_dict(m) for m in (data.get("messages") or [])],
            resume=str(data.get("resume", "")),
        )


class SessionNotFound(KeyError):
    """没有这个会话。"""


class SessionStore:
    """JSON 落盘的会话库。人能直接打开看。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._sessions: dict[str, Session] | None = None

    # --- 落盘 -------------------------------------------------------------

    def _load(self) -> dict[str, Session]:
        if self._sessions is not None:
            return self._sessions
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._sessions = {
                str(row["session_id"]): Session.from_dict(row)
                for row in data.get("sessions", [])
            }
        else:
            self._sessions = {}
        return self._sessions

    def _flush(self) -> None:
        sessions = self._load()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "sessions": [s.to_dict() for s in sessions.values()],
        }
        # 先写临时文件再替换：写一半崩掉不会毁掉整个会话库
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(self.path)

    # --- 会话 -------------------------------------------------------------

    def list(self) -> list[Session]:
        """按最近活动排序——前台左边的会话列表。"""
        with self._lock:
            sessions = list(self._load().values())
        return sorted(sessions, key=lambda s: s.updated_at, reverse=True)

    def create(self, title: str = "") -> Session:
        with self._lock:
            sessions = self._load()
            session = Session(
                session_id=f"s-{secrets.token_hex(6)}",
                title=title or "新会话",
            )
            sessions[session.session_id] = session
            self._flush()
            return session

    def get(self, session_id: str) -> Session:
        with self._lock:
            session = self._load().get(session_id)
            if session is None:
                raise SessionNotFound(session_id)
            return session

    def ensure(self, session_id: str) -> Session:
        """有就取，没有就建——省得前台每次都要先查一遍。

        **给定了编号就用给定的那个**（而不是另生成一个随机号）：
        否则调用方重试一次就会多出一个孤儿会话，而且它拿到的号跟页面上显示的
        对不上。空编号才生成新号——那是"开个新会话"的意思。
        """
        if not session_id:
            return self.create()
        try:
            return self.get(session_id)
        except SessionNotFound:
            with self._lock:
                sessions = self._load()
                session = Session(session_id=session_id, title="新会话")
                sessions[session_id] = session
                self._flush()
                return session

    def rename(self, session_id: str, title: str) -> Session:
        with self._lock:
            session = self._load().get(session_id)
            if session is None:
                raise SessionNotFound(session_id)
            session.title = title.strip() or session.title
            session.updated_at = now_iso()
            self._flush()
            return session

    def delete(self, session_id: str) -> None:
        with self._lock:
            sessions = self._load()
            if session_id not in sessions:
                raise SessionNotFound(session_id)
            del sessions[session_id]
            self._flush()

    # --- 消息 -------------------------------------------------------------

    def append(self, session_id: str, message: Message) -> Session:
        with self._lock:
            session = self._load().get(session_id)
            if session is None:
                raise SessionNotFound(session_id)
            session.messages.append(message)
            session.updated_at = now_iso()
            # 第一句话当标题——比"未命名会话"有用得多
            if message.role == ROLE_USER and (not session.title or session.title == "新会话"):
                session.title = make_title(message.text)
            self._flush()
            return session

    def set_resume(self, session_id: str, resume: str) -> None:
        with self._lock:
            session = self._load().get(session_id)
            if session is None:
                raise SessionNotFound(session_id)
            session.resume = resume
            self._flush()

    def last_prompt(self, session_id: str) -> str:
        """最近一次用户提问——「确认执行」后要拿它重跑。"""
        session = self.get(session_id)
        for message in reversed(session.messages):
            if message.role == ROLE_USER:
                return message.text
        return ""
