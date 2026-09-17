# resident-employee

> 给业务系统配**常驻 AI 员工**的框架。本仓库是其中负责「**AI 能做什么**」的部件：授权与审计。
>
> An open framework for resident AI employees in business systems — this repo is the
> authorization & audit half: humans sign, agents verify, nothing is self-granted.

**一句话核心**：`--yes` 是**约定**（AI 自己就能把参数加上），签名是**物理**（AI 没有私钥，数学上造不出）。

---

## 它解决什么

给 AI 员工放权，绕不开一个问题：**闸门凭什么拦得住它？**

绝大多数做法是「在代码里检查一个标志位」——比如要求命令带 `--yes`。问题是：**AI 完全可以自己把 `--yes` 加上**。拦住它的不是代码，是它守规矩。这不是安全，是礼貌。

本部件把闸门换成两种**它绕不过去**的东西：

| 档 | 机制 | AI 读到凭据会怎样 | 需要服务端存档 |
|---|---|---|---|
| **② 一次性授权码 + 审计哈希链** | 执行器持码，每次使用消耗一次 | **能自己把闸门打开** —— 所以码必须放在 AI 读不到的地方 | 要 |
| **③ 人类私钥签名的令牌** | Ed25519 验签 | **读到了也造不出签名** | 不要，有公钥就能验 |

**「读得到就能用」 vs 「读得到也造不出」——这就是 ②→③ 的全部意义。**

规范建议**从 ② 起步**（零依赖、内部够用），要「对外举证 / 不可抵赖」再上 ③。**②→③ 架构不用改**，把授权码换成签名令牌即可。

## 授权书必须写清六样

| 字段 | 含义 | 缺了会怎样 |
|---|---|---|
| 谁签的 | 人类身份 | 出了事无法追责 |
| 给谁 | 员工／AI 身份 | 别人捡到就能用 |
| 做什么 | 动作白名单 | 权限无限大 |
| 对什么 | 资源白名单 | 越权 |
| 到什么时候 | 有效期 | 永久有效 |
| 多少次 | 次数上限 | 一次授权刷到底 |

**缺一个就是不合格设计**——`Grant.validate()` 会直接抛 `GrantError`，不给"凑合能用"的机会。

## 快速开始

```bash
pip install -e .                 # ② 档：零依赖
pip install -e ".[signed]"       # ③ 档才需要 cryptography
```

人类侧签发：

```python
from resident_employee.authz import AuditChain, Grant, GrantStore, in_hours, now_utc

audit = AuditChain("data/audit.jsonl")
store = GrantStore("data/grants.json", audit)

grant = Grant(
    issuer="张三",                    # 谁签的
    subject="key-pool-operator",      # 给谁
    actions=("add-alias", "set-strategy"),   # 做什么
    resources=("api-key-pool",),             # 对什么
    issued_at=now_utc(),
    expires_at=in_hours(24),
    max_uses=None,                    # 有效期内次数不限（低风险档：一天签一次）
)
record, code = store.issue(grant)
print(code)      # ← 明文码只在这一刻出现，档案里只存哈希
```

执行器侧校验：

```python
from resident_employee.authz import authorize

decision = authorize(
    store, grant_id=record.grant.grant_id,
    action="add-alias", resource="api-key-pool",
    code=code,          # ← 码由执行器持有，AI 不持有
)
if not decision.allowed:
    raise PermissionError(decision.brief())
```

跑一遍看效果（含签名档与篡改检测）：

```bash
python examples/demo.py
```

## 命令行（人类侧）

非技术用户不用写 Python：

```bash
resident-authz status                       # 一条命令看清健康
resident-authz check                        # 自证可用（临时目录跑完整生命周期，不碰真实数据）
resident-authz issue --issuer 张三 --subject key-pool-operator \
    --actions add-alias,set-strategy --resources api-key-pool --hours 24
resident-authz list --all
resident-authz audit --event denied         # 只看被拒的
resident-authz verify-chain                 # 审计链被改过会报错并指出第几条
resident-authz revoke <编号> --yes          # 高风险
```

**分级由代码强制**，不靠使用者自觉：

| 级别 | 命令 | 闸门 |
|---|---|---|
| 只读 | `status` `list` `audit` `verify-chain` `check` | 直接可用 |
| 低风险写 | `issue` | 自动备份后执行（备份落 `<base>/backups/`） |
| **高风险写** | `revoke` | **必须 `--yes`**，否则**退出码 2** 并打印确认提示 |

退出码：`0` 成功 / `1` 出错 / **`2` 拒绝执行**（未取得人类确认）。
**与"出错"分开**，调用方（包括 AI）能据此区分"你没被授权"和"命令写错了"。

> 真跑过的验收（2026-09-17）：
> `revoke` 不带 `--yes` → 退出码 2，且**档案一个字节都没动**（仍显示"有效"）；
> 带 `--yes` → 撤销成功并留下备份；`verify-chain` 在审计被事后篡改时报错。

## 常驻运行时（v0.1）

CLI 是员工的「手」，运行时是「人」——一个常驻进程，带着**专属系统提示 + 跨会话记忆 + 每次动作前的闸门**。

```python
from resident_employee.runtime import Employee, EmployeeRunner
from resident_employee.runtime.adapters.claude_agent_sdk import ClaudeAgentSDKEngine

employee = Employee.from_file("examples/employee.example.json").validate()
runner = EmployeeRunner(
    employee, ClaudeAgentSDKEngine(),
    allowed_tools=("Read", "Bash"), max_steps=20, audit=audit_chain,
)
result = await runner.run_turn("看看系统健康", on_event=print_event)   # on_event = 过程可见
```

员工定义就是规范要求的**六段式**（我管的系统 / 常用动作 / 权限边界 / 这个系统的坑 / 排障顺序 / 输出要求），
缺一段直接报错——不给"凑合能用"的机会。另有一段**通用铁律**由运行时写死注入（不猜接口、做完验证、
别人数据只读、不许绕过闸门……），免得每个员工定义抄一遍还抄漏。

**闸门**（对应故障预案，逐条落地）：

| 故障 | 闸门 | 内建还是自建 |
|---|---|---|
| 选错工具 | 工具面收窄——只发它该有的那几把 | 自建（薄薄一层） |
| 参数构造出错 | 必填 / 禁用参数 | 自建 |
| **调用循环** | 同工具同参数的指纹计数 | **必须自建——官方没有** |
| 越权动作 | 接 `authz`，拒绝理由回灌给模型 | 自建 |
| 步数失控 | `max_steps` → SDK `max_turns` | 内建 |
| 花费失控 | `max_cost_usd` → SDK `max_budget_usd` | 内建 |
| 卡死 | 超时 | 自建（官方无内建超时） |

**引擎搁在接口后面**（`ports.AgentEngine`）。运行时核心不认识任何 SDK，因此：

- 自己的逻辑（闸门、循环检测、成本、审计、事件流）**用假引擎全量测** —— 不花钱、不起进程、不依赖网络
- 换引擎只改 `adapters/` 里一个文件

跑看看：

```bash
python examples/demo_employee.py --fake           # 离线，不联网不花钱，看运行时自己的行为
python examples/demo_employee.py "看看系统健康"    # 真引擎（会真起进程、真花 token）
```

`--fake` 那次会演一遍「正常读 → 越权写被拦 → 汇报」，并在结尾打出**审计哈希链**——
每一次放行和拒绝都在链上。

## 它拦住什么（真跑过的验收单）

| 场景 | 结论 |
|---|---|
| 授权码不对 | `BAD_CODE` |
| 授权过期 | `EXPIRED` |
| 已撤销 | `REVOKED` |
| 一次性令牌用第二次 | `EXHAUSTED` |
| 动作不在白名单 | `ACTION_NOT_ALLOWED` |
| 资源不在白名单 | `RESOURCE_NOT_ALLOWED` |
| 令牌被改一个字符 | `BAD_CODE`（签名验不过） |
| 用别人的私钥签 | `BAD_CODE` |
| 审计记录被事后修改 | 哈希链校验**报错并指出第几条** |
| 审计记录被抽掉一条 | 哈希链校验报错 |

**被拒的也记审计**——只记成功的审计是没用的，想知道「谁试过碰不该碰的」恰恰要记被拒的那些。

## 三个补丁一个都不能少

签名令牌**天生不可撤销**——签出去就在有效期内一直有效。少任何一个补丁，这套就是纸门：

1. **短有效期**（`expires_at` 本身就短）
2. **撤销名单**（执行前查一次，`verify_signed(revoked_ids=...)`）
3. **高风险用一次性**（`max_uses=1` + nonce 记录，`used_nonces=...`）

> 第 3 条有个必须说清的地方：签名令牌是**自带的凭据**，「用过几次」这个状态**没地方存**。
> 所以 `max_uses > 1` 的签名令牌本库**明确拒绝**（而不是假装支持）——
> 要用多次就签 `max_uses=None` 的长期令牌，或走 ② 档（次数由执行器记）。
> **无状态的凭据 + 有状态的防重放**，这是自带凭据的必然代价。

## 私钥放哪

丢了 = 权限体系崩；被偷 = 别人以你的名义授权，**比密码泄露严重一个量级**。

| 方案 | 强度 | 适合 |
|---|---|---|
| 硬件密钥 | 最高 | 长期、正式场景 |
| 系统钥匙串 / TPM | 中高 | 本机自用 |
| 手机 App + 指纹/人脸 | 中高、最易用 | **非技术用户，推荐** |

**绝不允许**：明文私钥文件放在项目目录、提交进 git、跟着 `.env` 走。
最不该做的就是把它存成一个文件放在代码旁边——那样等于没签。

## 框架地图

| 部件 | 管什么 | 状态 |
|---|---|---|
| [`sql-guard`](https://github.com/kxc7558/sql-guard) | **能看见什么**（数据可见范围） | ✅ 已发布，独立安装 |
| **`authz`**（本仓库） | **能做什么**（授权 + 审计） | ✅ v0.1 |
| **运行时**（本仓库） | 把员工跑起来（闸门 + 事件流 + 审计） | ✅ v0.1，147 用例 / 94% 覆盖 |
| 网页前台 | 照 ChatGPT 那一档做，过程可见 | ⬜ 未开始（事件流已备好，接上即可） |

## 边界（别误会）

- **它不是执行器。** 本库只判「这次操作有没有被授权」，不执行操作。执行是调用方的事。
- **② 档的强度取决于码放哪。** 码和执行者在同一侧，AI 若能读到配置里的码就等于没闸门。**闸门的钥匙不能挂在门上。** 要连执行器也信不过，就上 ③。
- **审计哈希链防的是「事后篡改」，不是「事前伪造」。** 有写权限的人可以整条链重算——要防这个，得把链尾哈希定期对外公布或签名。
- **不做密钥管理。** 本库给 `generate_keypair()`，但私钥存哪是你的事（见上）。

## 开发

```bash
pip install -e ".[dev]"
python -m pytest --cov=resident_employee    # 46 用例，覆盖率须 ≥80%
python examples/demo.py
```

## License

MIT — 见 [LICENSE](LICENSE)。
