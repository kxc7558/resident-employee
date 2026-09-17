"""闸门：在每个动作**发生之前**拦住它。

对应规范 `outsourced-employee.md` 的故障预案。**每一条都有明确归属**：

| 故障 | 闸门 | 内建还是自建 |
|---|---|---|
| 选错工具 | `ToolScopeGate`（工具面收窄） | 自建（薄薄一层） |
| 参数构造出错 | `ToolSchemaGate`（必填/禁用参数） | 自建 |
| **调用循环** | `LoopBreakerGate`（同工具同参数的指纹计数） | **必须自建——官方没有** |
| 越权动作 | `AuthorizationGate`（接 authz） | 自建（接已有件） |

步数、成本、超时不在这里——它们是**整轮**的闸，由 `runner` 管。

## 为什么拒绝理由要写得那么啰嗦

拒绝理由**会回灌给模型**（Agent SDK 的 `can_use_tool` 语义）。所以每个 `deny`
都必须说清「哪一条不允许」+「改成什么」。写成 "denied" 的话模型只能瞎猜，
然后换个姿势再撞一次——那就从"拦住"退化成"陪着它刷步数"。
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence

from .ports import GateDecision

# 模型可以自由读、不需要授权的工具（规范里的「只读免签」）
DEFAULT_FREE_TOOLS: frozenset[str] = frozenset({"Read", "Glob", "Grep", "WebSearch", "WebFetch"})


def canonical_args(args: Mapping[str, Any]) -> str:
    """参数的规范化表示，用来做「同一个调用」的指纹。

    必须排序 + 压紧空白，否则同一组参数因键序不同会被当成两次不同调用，
    循环检测就漏了。
    """
    return json.dumps(dict(args), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def fingerprint(tool: str, args: Mapping[str, Any]) -> str:
    return f"{tool}::{canonical_args(args)}"


class BaseGate:
    """闸门基类。带 `name` 是为了审计能定位到「是哪道闸拦的」。"""

    name = "gate"

    async def check(self, tool: str, args: Mapping[str, Any]) -> GateDecision:  # pragma: no cover - 抽象
        raise NotImplementedError


class ToolScopeGate(BaseGate):
    """工具面收窄：**只发它该有的那几把**。

    这是"选错工具"最有效的第一道——不是等它挑错了再拦，
    而是让它根本没有别的工具可挑。
    """

    name = "工具面"

    def __init__(self, allowed: Iterable[str] | None) -> None:
        self.allowed = frozenset(allowed) if allowed else frozenset()

    async def check(self, tool: str, args: Mapping[str, Any]) -> GateDecision:
        if not self.allowed:
            return GateDecision.allow(self.name)
        if tool in self.allowed:
            return GateDecision.allow(self.name)
        return GateDecision.deny(
            f"工具 {tool} 不在你的工具面里。可用的只有：{'、'.join(sorted(self.allowed))}。"
            f"请用这些工具里的某一个完成，不要改用别的。",
            self.name,
        )


class ToolSchemaGate(BaseGate):
    """参数形状：必填缺了、出现禁用参数 → 拒，并说清缺什么。

    只做**结构性**校验（在不在、有没有），不做业务校验——业务校验属于系统自己，
    应当由 CLI 那边做。

    Args:
        required: ``{工具名: 必须出现的参数名集合}``
        forbidden: ``{工具名: 不允许出现的参数名集合}``
    """

    name = "参数形状"

    def __init__(
        self,
        required: Mapping[str, Iterable[str]] | None = None,
        forbidden: Mapping[str, Iterable[str]] | None = None,
    ) -> None:
        self.required = {k: frozenset(v) for k, v in (required or {}).items()}
        self.forbidden = {k: frozenset(v) for k, v in (forbidden or {}).items()}

    async def check(self, tool: str, args: Mapping[str, Any]) -> GateDecision:
        need = self.required.get(tool, frozenset())
        missing = sorted(name for name in need if name not in args or args[name] in ("", None))
        if missing:
            return GateDecision.deny(
                f"调用 {tool} 缺必填参数：{'、'.join(missing)}。"
                f"请补齐后重试，不要用空值或猜测的默认值。",
                self.name,
            )

        banned = sorted(name for name in self.forbidden.get(tool, frozenset()) if name in args)
        if banned:
            return GateDecision.deny(
                f"调用 {tool} 带了禁用参数：{'、'.join(banned)}。请去掉它们。",
                self.name,
            )
        return GateDecision.allow(self.name)


class LoopBreakerGate(BaseGate):
    """调用循环检测。**官方没有这个东西，必须自建。**

    同一个「工具 + 参数」重复出现超过上限就拒。这拦的是 agent 的经典死法：
    拿着同样的入参反复敲同一个工具，每轮都拿到同样的结果，然后继续敲。

    上限给 3 而不是 1：允许一次正常的重试（比如第一次超时了），
    第 4 次就说明它没有在读结果，只是在刷。
    """

    name = "循环检测"

    def __init__(self, max_repeats: int = 3) -> None:
        if max_repeats < 1:
            raise ValueError(f"max_repeats 必须 ≥1，收到 {max_repeats}")
        self.max_repeats = max_repeats
        self._counts: Counter[str] = Counter()

    def reset(self) -> None:
        self._counts.clear()

    def counts(self) -> dict[str, int]:
        return dict(self._counts)

    async def check(self, tool: str, args: Mapping[str, Any]) -> GateDecision:
        key = fingerprint(tool, args)
        self._counts[key] += 1
        times = self._counts[key]

        if times <= self.max_repeats:
            return GateDecision.allow(self.name)

        return GateDecision.deny(
            f"你已经用完全相同的参数调用 {tool} {times} 次了，结果不会变。"
            f"停下，换一条路：读一下上一次的返回内容、换一个工具，"
            f"或者直接告诉用户你卡在哪。",
            self.name,
        )


class AuthorizationGate(BaseGate):
    """越权动作：每次（非免签的）工具调用都要有授权。

    它拿到的 `decide` 通常是接 `authz.authorize` 的适配函数——
    **授权码在执行器这侧**（也就是这里的宿主进程），模型拿不到。
    规范里那句「闸门的钥匙不能挂在门上」说的就是这件事。

    Args:
        decide: ``async (action, args) -> 有 .allowed / .reason 的对象``。
        free: 免签工具（只读类，规范里的「只读免签」）。
        resource: 这些动作作用在什么资源上（进授权书白名单的那个值）。
    """

    name = "授权"

    def __init__(
        self,
        decide: Callable[[str, Mapping[str, Any]], Awaitable[Any]],
        *,
        free: Iterable[str] = DEFAULT_FREE_TOOLS,
        resource: str = "",
    ) -> None:
        self.decide = decide
        self.free = frozenset(free)
        self.resource = resource

    async def check(self, tool: str, args: Mapping[str, Any]) -> GateDecision:
        if tool in self.free:
            return GateDecision.allow(self.name)

        verdict = await self.decide(tool, args)
        if getattr(verdict, "allowed", False):
            return GateDecision.allow(self.name)

        reason = getattr(verdict, "reason", "没有授权")
        code = getattr(verdict, "code", "")
        return GateDecision.deny(
            f"动作 {tool} 未获授权（{code}）：{reason}。"
            f"**不要换成别的说法、拆成几步或改用其他工具再来一次**——"
            f"那属于绕过。请把这件事做不了的原因告诉用户，请他去签发授权。",
            self.name,
        )


class GateChain:
    """把若干闸门串起来。**第一个拒绝的说了算。**

    顺序有意为之：先看工具面（最便宜的判断），再看参数形状，
    再看循环，最后才查授权（最贵的，可能落盘）。
    便宜的判断放前面，能省掉后面的开销。

    每一次结论都留着（`decisions`），供审计和「过程可见」用。
    """

    def __init__(self, gates: Sequence[BaseGate]) -> None:
        self.gates = list(gates)
        self.decisions: list[tuple[str, Mapping[str, Any], GateDecision]] = []

    async def __call__(self, tool: str, args: Mapping[str, Any]) -> GateDecision:
        for gate in self.gates:
            decision = await gate.check(tool, args)
            if not decision.allowed:
                self.decisions.append((tool, dict(args), decision))
                return decision
        final = GateDecision.allow("链末")
        self.decisions.append((tool, dict(args), final))
        return final

    def reset(self) -> None:
        self.decisions.clear()
        for gate in self.gates:
            if isinstance(gate, LoopBreakerGate):
                gate.reset()

    def denials(self) -> list[tuple[str, GateDecision]]:
        return [(tool, d) for tool, _, d in self.decisions if not d.allowed]


def build_default_chain(
    *,
    allowed_tools: Iterable[str] | None = None,
    required_args: Mapping[str, Iterable[str]] | None = None,
    forbidden_args: Mapping[str, Iterable[str]] | None = None,
    max_repeats: int = 3,
    decide: Callable[[str, Mapping[str, Any]], Awaitable[Any]] | None = None,
    free_tools: Iterable[str] = DEFAULT_FREE_TOOLS,
) -> GateChain:
    """按规范组装推荐的一道链。

    这是给调用方的**默认答案**——四个闸门就是故障预案里那四条，
    顺序也按"便宜的先判"排好。传了 `decide` 才装授权闸门。
    """
    gates: list[BaseGate] = [ToolScopeGate(allowed_tools)]
    if required_args or forbidden_args:
        gates.append(ToolSchemaGate(required_args, forbidden_args))
    gates.append(LoopBreakerGate(max_repeats))
    if decide is not None:
        gates.append(AuthorizationGate(decide, free=free_tools))
    return GateChain(gates)
