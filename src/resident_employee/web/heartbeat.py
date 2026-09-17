"""心跳与在岗状态。

常驻员工要能回答一个问题：**它现在在不在岗？**

做法是**状态文件**，不是轮询监控——一条命令读文件就知道，不用起 agent、
不用挂个后台脚本一直 sleep 盯着。规范里那条"能一条命令解决的事不启动 agent"
在这里就是这个意思。

写的东西：

```json
{"pid": 12345, "started_at": "...", "last_seen": "...", "employee": "key-pool-operator", "port": 8765}
```

**"在岗"的判据有两个，缺一不可**：文件够新 **且** 那个 pid 还活着。
只看文件的话，进程被强杀留下的陈旧文件会一直显示"在岗"——
那比没有状态更糟，因为它会让你以为没事。
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

HEARTBEAT_FILE = "heartbeat.json"
STALE_AFTER_S = 90.0
"""多久没动过就算离岗。比一次请求的间隔宽得多，避免误报。"""

TOUCH_THROTTLE_S = 5.0
"""写文件的节流。每次请求都写盘没必要，本机盘也经不起长年累月地写。"""


def pid_alive(pid: int) -> bool:
    """那个进程还在不在。

    Windows 上用 `os.kill(pid, 0)` 会真的去终止进程（信号语义不同），
    所以那边换个法子问。
    """
    if pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - 平台分支
        import subprocess

        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
            return str(pid) in (out.stdout or "")
        except (OSError, subprocess.SubprocessError):
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在但不归我管
    return True


def now_ts() -> float:
    return time.time()


@dataclass
class Heartbeat:
    """往状态文件里写"我还活着"。"""

    path: Path
    throttle_s: float = TOUCH_THROTTLE_S
    _last_write: float = field(default=0.0, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def start(self, *, port: int = 0, employee: str = "") -> dict[str, Any]:
        payload = {
            "pid": os.getpid(),
            "started_at": now_ts(),
            "last_seen": now_ts(),
            "port": port,
            "employee": employee,
        }
        self._write(payload)
        return payload

    def touch(self) -> None:
        """每次请求调一下。节流，别把盘写坏。"""
        moment = now_ts()
        with self._lock:
            if moment - self._last_write < self.throttle_s:
                return
        current = self.read()
        if not current:
            return
        current["last_seen"] = moment
        self._write(current)

    def stop(self) -> None:
        """主动下线：把文件删掉，别留个"看着在岗其实已经关了"的状态。"""
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    def _write(self, payload: Mapping[str, Any]) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(dict(payload), ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self.path)
            self._last_write = now_ts()

    # --- 读 ---------------------------------------------------------------

    def read(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None


@dataclass(frozen=True)
class Health:
    """在岗状态结论。"""

    alive: bool
    label: str
    detail: str
    uptime_s: float = 0.0

    def brief(self) -> str:
        return f"{self.label} —— {self.detail}"


def check_health(path: str | Path, *, stale_after: float = STALE_AFTER_S) -> Health:
    """读状态文件判断在岗与否。**纯函数**，不依赖进程自己——所以外部也能查。"""
    heartbeat = Heartbeat(Path(path))
    data = heartbeat.read()

    if not data:
        return Health(False, "离岗", "没有状态文件——服务没起过，或者已经退出了")

    pid = int(data.get("pid") or 0)
    last_seen = float(data.get("last_seen") or 0.0)
    started_at = float(data.get("started_at") or 0.0)
    age = now_ts() - last_seen
    uptime = max(0.0, now_ts() - started_at) if started_at else 0.0

    if not pid_alive(pid):
        return Health(
            False, "离岗", f"进程 {pid} 已经不在了（状态文件是陈旧的，建议清掉）", uptime
        )
    if age > stale_after:
        return Health(
            False,
            "疑似卡住",
            f"进程 {pid} 还在，但已经 {age:.0f} 秒没有活动（超过 {stale_after:.0f} 秒阈值）",
            uptime,
        )

    who = data.get("employee") or "(未配置员工)"
    port = data.get("port") or "?"
    return Health(
        True, "在岗", f"pid {pid}，员工 {who}，端口 {port}，已运行 {uptime / 60:.1f} 分钟", uptime
    )
