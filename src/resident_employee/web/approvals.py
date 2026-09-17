"""审批簿：把「人在环」那一步接上授权。

规范里那句 **「签授权书」和「点确认执行」是同一个动作** 就落在这里：

```
员工想做一个没被授权的动作
  → 闸门拦住，理由回灌给模型（它会跟用户说"这件事我需要授权"）
  → 前台显示「⚠️ 需要你确认：要执行 X」+ 一个「确认执行」按钮
  → 用户点下去 = 签一张一次性授权（max_uses=1，只覆盖那一个动作）
  → 重跑，这次过得去
```

## 授权码握在谁手里

**握在这里（服务端），模型拿不到。** 这是规范里那条「闸门的钥匙不能挂在门上」的落点：
② 档的授权码是"读得到就能用"的，所以绝不能进模型能读的地方。

代价是：**码只在内存里**。服务重启后，之前签到的一次性授权就没人拿得到码了
（授权记录还在档案里，只是没人能用）。对一次性审批来说这反而是对的——
重启后要重新确认一次，比让一个旧许可继续有效安全。

## 为什么"待确认"不放在这里

早先版本把「哪个动作待确认」也记在内存里，于是**刷新页面就看不到「确认执行」按钮了**——
明明"被拦"还留在会话记录里，按钮却没了，用户只能干瞪眼。

现在改成：**可确认的动作从会话记录里推**（`confirmable_actions`），
跨刷新、跨重启都在，而且**不能凭空伪造一个没被拦过的动作**去骗授权。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from ..authz import AuthDecision, AuditChain, Grant, GrantStore, authorize, in_hours, now_utc
from ..runtime.gates import AuthorizationGate

CONFIRMABLE_GATE = AuthorizationGate.name
"""只有**授权闸门**拦下的动作，确认执行才可能放行。

工具面 / 循环检测拦下的是结构性边界——批准它没用，重跑还是会被同一道闸拦住。
给用户一个点了没用的按钮比不给更糟，所以必须分清。
"""


def confirmable_actions(steps: Iterable[Mapping[str, Any]]) -> list[str]:
    """从过程记录里挑出「确认执行能解决」的动作。

    判据：这一步是"被拦"，且拦住它的是**授权闸门**。
    """
    found: list[str] = []
    for step in steps:
        if step.get("kind") != "denied":
            continue
        if step.get("gate") != CONFIRMABLE_GATE:
            continue
        tool = str(step.get("tool", ""))
        if tool and tool not in found:
            found.append(tool)
    return sorted(found)


def blocked_hard(steps: Iterable[Mapping[str, Any]]) -> list[str]:
    """被结构性闸门拦下的动作——**批准也没用**，要改配置的。"""
    found: list[str] = []
    for step in steps:
        if step.get("kind") != "denied":
            continue
        if step.get("gate") == CONFIRMABLE_GATE:
            continue
        tool = str(step.get("tool", ""))
        if tool and tool not in found:
            found.append(tool)
    return sorted(found)


class ApprovalBook:
    """签发一次性授权，并记住对应的码。"""

    def __init__(
        self,
        store: GrantStore,
        audit: AuditChain | None = None,
        *,
        resource: str = "employee",
        issuer: str = "人类（前台确认）",
        subject: str = "employee",
        hours: float = 1.0,
    ) -> None:
        self.store = store
        self.audit = audit
        self.resource = resource
        self.issuer = issuer
        self.subject = subject
        self.hours = hours
        self._codes: dict[str, str] = {}

    def sign(self, action: str, *, note: str = "") -> tuple[Grant, str]:
        """签一张**只覆盖这一个动作**的一次性授权。"""
        grant = Grant(
            issuer=self.issuer,
            subject=self.subject,
            actions=(action,),
            resources=(self.resource,),
            issued_at=now_utc(),
            expires_at=in_hours(self.hours),
            max_uses=1,
            note=note or f"前台确认执行：{action}",
        ).validate()

        record, code = self.store.issue(grant)
        self._codes[record.grant.grant_id] = code
        return record.grant, code

    def decide(self, action: str, resource: str = "") -> AuthDecision:
        """闸门调用它：**扫一遍手上有码的授权，看有没有覆盖这个动作的**。"""
        target = resource or self.resource
        for grant_id, code in list(self._codes.items()):
            record = self.store.get(grant_id)
            if record is None or record.is_revoked:
                continue
            if not (record.grant.allows_action(action) and record.grant.allows_resource(target)):
                continue
            return authorize(
                self.store,
                grant_id=grant_id,
                action=action,
                resource=target,
                code=code,
                actor=self.subject,
            )

        return AuthDecision(
            allowed=False,
            code="NO_ACTIVE_GRANT",
            reason=f"没有覆盖「{action} @ {target}」的有效授权",
        )

    def grant_ids(self) -> list[str]:
        return list(self._codes)
