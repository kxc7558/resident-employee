"""入口把关：**认证归宿主系统**。

因为对话台是**嵌在业务系统内部**的，用户已经登录过业务系统了——
我们不该再要一次登录，更**不该自己造一套账号体系**（那等于多一套要维护的账号
加多一处会出错的地方）。所以这里只做一件事：**认宿主的令牌**。

两种情形：

| 情形 | 行为 |
|---|---|
| 没配 `host_token`（独立模式） | **只接受来自本机的请求**——别人从局域网也连不上 |
| 配了 `host_token` | 请求必须带令牌（头 `X-Employee-Token`，或首次用 `?token=` 换来的 HttpOnly cookie） |

**cookie 里放的是令牌的指纹，不是令牌本身。** cookie 会进浏览器存储、可能被截图或导出，
放明文等于把令牌抄了一份到别处。

比对用 `hmac.compare_digest`——`==` 会按字节短路，能被计时差猜出来。
**比的是编码后的字节，不是字符串**：`compare_digest` 对 str 要求两边都是纯 ASCII，
令牌里有个中文字就直接抛 `TypeError`（变成 500，而不是干净的 401）。
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

COOKIE_NAME = "re_host"
HEADER_NAME = "X-Employee-Token"

LOOPBACK_PREFIXES = ("127.", "::1", "::ffff:127.")


def token_fingerprint(token: str) -> str:
    """令牌的指纹。cookie 里只放这个。"""
    return hashlib.sha256(token.strip().encode("utf-8")).hexdigest()


def is_loopback(host: str | None) -> bool:
    """是不是本机来的。"""
    if not host:
        return False
    host = host.strip()
    return any(host.startswith(prefix) for prefix in LOOPBACK_PREFIXES) or host == "localhost"


def _same(candidate: str | None, expected: str) -> bool:
    """定时安全比较。**编码成字节再比**，免得非 ASCII 令牌把 compare_digest 惹炸。"""
    if not candidate:
        return False
    return hmac.compare_digest(candidate.strip().encode("utf-8"), expected.strip().encode("utf-8"))


@dataclass(frozen=True)
class Access:
    """一次请求的准入结论。"""

    allowed: bool
    reason: str = ""
    set_cookie: str = ""
    """需要下发 cookie 时填（值就是 `COOKIE_NAME` 该有的值）。"""


def check_access(
    *,
    client_host: str | None,
    header_token: str | None = None,
    cookie_value: str | None = None,
    host_token: str = "",
) -> Access:
    """判断这次请求放不放行。

    纯函数——不碰 socket、不碰 header 解析，所以能直接测各种组合。
    """
    if not host_token:
        # 独立模式：只认本机。**不给"没配令牌就谁都能连"这种默认**。
        if is_loopback(client_host):
            return Access(allowed=True)
        return Access(
            allowed=False,
            reason=(
                "独立模式只接受本机访问。要从别处连，请在配置里设 host_token——"
                "认证归宿主系统，我们不自建账号体系。"
            ),
        )

    expected = token_fingerprint(host_token)

    # 头里放**原令牌**（宿主手里就是它），cookie 里放**指纹**——两者分别比。
    if _same(header_token, host_token):
        return Access(allowed=True)
    if _same(cookie_value, expected):
        return Access(allowed=True)

    return Access(
        allowed=False,
        reason=(
            "没带宿主令牌，或令牌不对。"
            "用 ?token=<宿主令牌> 访问一次即可（会换成 cookie，之后不用再带）——"
            "认证归宿主系统，我们不自建账号体系。"
        ),
    )


def access_from_query_token(query_token: str | None, host_token: str) -> Access:
    """页面首次加载用 `?token=` 换 cookie。

    换到 cookie 之后就不必让令牌留在 iframe 的地址里（那会进历史记录、进截图）。
    """
    if not host_token or not query_token:
        return Access(allowed=False, reason="没有查询参数令牌")
    if not _same(query_token, host_token):
        return Access(allowed=False, reason="查询参数里的宿主令牌不对")
    return Access(allowed=True, set_cookie=token_fingerprint(host_token))
