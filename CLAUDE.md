# resident-employee — 项目宪法

> 这个文件给 AI 读。人读 [README.md](README.md)。

## 这是什么

给业务系统配**常驻 AI 员工**的框架。本仓库是其中负责「**AI 能做什么**」的部件：**授权与审计**。

核心命题：`--yes` 是**约定**（AI 自己就能加上），签名是**物理**（没有私钥，造不出）。

## 目录结构（以及对四层架构的偏离说明）

本项目**不套用** `api/service/db/shared` 四层——它是个**库**，不是应用：没有请求入口、没有业务编排。
按 `standard-and-deviation.md` 留痕：**场景** = 独立可复用的库；**为何标准不适用** = 四层是给"有请求入口、有存储、有编排"的应用准备的；
**替代** = 按职责分文件；**谁定的** = claude-code，2026-09-17。

```
src/resident_employee/authz/
├── grant.py    授权书（六字段 + 校验 + 规范化签名载荷）
├── audit.py    审计哈希链（JSONL 追加，改一条后面全对不上）
├── store.py    授权档案（签发/撤销/消费；只存码的哈希）
├── verify.py   ② 档校验流程（先认证后授权，七道检查）
├── signed.py   ③ 档签名令牌（Ed25519，人类持私钥）
└── cli.py      人类侧命令行（分级授权由代码强制）
tests/          60 用例，覆盖率 93%
examples/       可跑的 demo（含篡改检测）
verify_sdk.py   常驻运行时的可行性实测（见文末）
```

## 铁律（改代码前先读）

1. **拒绝也要记审计**。只记成功的审计是没用的——想知道"谁试过碰不该碰的"，恰恰要记被拒的。
2. **② 档的明文码只出现一次**（`store.issue` 的返回值）。任何时候都不许把明文码落盘、写日志、进错误信息。
3. **授权码比较用 `hmac.compare_digest`**，不许用 `==`——后者按字节短路，能靠计时差猜码。
4. **签名载荷必须走 `canonical_json`**（键排序）。不规范化的话，同一个对象在不同实现里序列化出的字节不同，签名会莫名验不过。
5. **六字段缺一不可**。`Grant.validate()` 必须抛错，不许为了方便放行空白名单。
6. **不许把执行能力塞进本库**。本库只判"有没有被授权"，不执行操作——执行是调用方的事。混进来就再也说不清边界了。

## 关键坑（都真踩过 / 已用测试固化）

- **`frozen=True` 的 dataclass 含 dict 字段 → 不可哈希**。不能用 `set()` 去重，会抛 `TypeError: unhashable type: 'dict'`（sql-guard 踩过同一个坑）。本库靠"按编号取"规避。
- **`cryptography` 必须是可选导入**。② 档标称零依赖，硬导入会让核心装不上。缺依赖时 `_require_crypto()` **明确报错**，不许静默降级——降级成"验不过"会让人以为签名被伪造了。
- **签名令牌无法自行计数使用次数**。`max_uses > 1` 的签名令牌本库**明确拒绝**（而不是假装支持）——要用多次就签 `max_uses=None` 或走 ② 档。
- **审计文件要追加，不能重写**。审计会一直长，每次重写全文迟早出事。档案文件（grants.json）用"写临时文件再 `replace`"保证原子性。
- **ISO 时间不带时区时按 UTC 处理**，不猜本地时区——猜错会让有效期整体偏 8 小时。
- **`note` 不进签名载荷**。它是给人看的备注，改了不该导致签名失效。
- Windows 控制台默认 GBK，跑脚本要 `PYTHONIOENCODING=utf-8`；pip 装依赖带 `-i https://pypi.tuna.tsinghua.edu.cn/simple`。

## 怎么跑

```bash
pip install -e ".[dev]" -i https://pypi.tuna.tsinghua.edu.cn/simple
python -m pytest --cov=resident_employee     # 46 用例，覆盖率须 ≥80%
PYTHONIOENCODING=utf-8 python examples/demo.py
```

**改完必须真跑**，不能只看"保存成功"。

## 待用户跑的验证（我做不了）

`verify_sdk.py` 实测 Agent SDK 能否驱动本机中转站（`http://127.0.0.1:15721`）。
**这一步被权限拦下了**——它会再起一个 Claude Code 实例，属于当前会话不该自作主张的动作。请手动跑：

```bash
cd d:/resident-employee && python verify_sdk.py
```

两关：纯文本、工具调用（后者是命门——驱动的是 `glm-5.3-flash`，扛不住工具调用的话常驻运行时这条路要换）。

## 已知缺口

- [ ] 常驻员工运行时（等 `verify_sdk.py` 的结果）
- [ ] 网页前台（"签授权书"与"点确认执行"是同一个动作，见规范）
- [ ] 授权档案的 SQLite 后端（当前 JSON，量大后要换）
- [ ] **三件套缺 agent 定义与手册**——按 `digital-employee.md`，交付一个系统要配
      「数字员工 agent + 操作 CLI + 手册」。CLI（`cli.py`）已就位，agent 定义未写
- [ ] 英文 README
- [ ] CI

## 相关规范

作者知识库里的规范（随仓库逐步开源）：
**授权规范**（六字段 / 四级签发 / 三补丁 / 落地阶梯）、**数据可见范围**（四层防线 / 模型边界）、
**Text-to-SQL**（七条铁律 / 校验四步）。另有独立仓库
[`sql-guard`](https://github.com/kxc7558/sql-guard) 实现读侧那一格。
