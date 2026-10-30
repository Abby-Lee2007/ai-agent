# Shadow 实验成果持久化与产品接入实施计划

阶段：2（SQLite 持久化闭环已实现，查询 API/H5 继续关闭）  
日期：2026-10-30  
状态：计划完成，尚未修改业务代码

## 一、阶段 0 结论

当前链路已经能执行 Shadow 评估，但还没有形成可追踪、可持久化、可查询的实验记录。

现有真实链路：

```text
信用卡申请
→ CardApplyTool 返回正式 application_id
→ BankingAgent._execute()
→ BankingAgent._observe_credit_application_shadow()
→ BusinessCreditShadowIntegration.observe_credit_application()
→ CreditRiskService.evaluate_business_shadow()
→ BusinessFeatureMaterializer.materialize()
→ Business Contract v0.3 校验
→ Shadow Adapter
→ CreditRiskAuditRecord
→ Agent 内存数组
→ 停止
```

当前缺失：

- application_id 关联。
- request_id 关联。
- 稳定 shadow_run_id。
- SQLite 持久化。
- 按 application_id、request_id、shadow_run_id 查询。
- 内部查询 API。
- 内部 H5 实验中心。
- 正式身份认证和内部角色授权。

## 二、工作区与数据基线

### 2.1 Git 工作区

当前分支：`main`

工作区已有大量未提交修改和未跟踪文件，包括：

- 前端 `web/index.html`。
- API、Agent、SQLite 模块。
- `finagent/risk/credit_risk/`。
- `tests/ml/`。
- 之前的审计报告和产品化路线图。

实施原则：

- 不 revert、不覆盖已有未提交修改。
- 每次阶段开始前重新检查 `git diff`。
- 修改限定在阶段声明范围。
- 阶段完成不自动提交 Git。

### 2.2 正式数据库

默认路径：

```text
C:\Users\HUAWEI\Desktop\金融科技导论\ai_banking_agent\runtime_data\finagent.db
```

阶段 0 基线：

- 文件大小：174,129,152 bytes。
- LastWriteTime：2026-10-30 16:19:17。
- SHA-256：

```text
86645E003230AD55B4D6182A29E51BC8BAB553039D59AB65CEAB5F20EC373EDE
```

注意：

- 该数据库只用于现状识别，不作为开发测试库。
- 后续所有迁移、写入、故障注入使用临时 SQLite。
- 不在正式库上执行 ALTER、INSERT、DELETE 或重复键测试。
- 不自动清理现有 `finagent.db-shm` / `finagent.db-wal`。

## 三、现有 Shadow 模块

| 文件 | 职责 |
|---|---|
| `finagent/risk/credit_risk/business_contract.py` | Business Feature Contract v0.3 和不可变 Snapshot 校验 |
| `finagent/risk/credit_risk/materializer.py` | 从 User/Card/Account/Transaction 构造业务特征 Snapshot |
| `finagent/risk/credit_risk/business_shadow_adapter.py` | 将 Snapshot 转为 Shadow Assessment，不升级 approximate |
| `finagent/risk/credit_risk/service.py` | 执行 Materializer/Adapter，生成 AuditRecord |
| `finagent/risk/credit_risk/audit.py` | 不可变、脱敏的 CreditRiskAuditRecord |
| `finagent/risk/credit_risk/integration.py` | 环境开关、Agent 适配、内存 audit 队列 |
| `finagent/agent/core.py` | 正式信用卡申请完成后触发 Shadow |

## 四、真实 Shadow 调用链与数据来源

### 4.1 request_id 来源

真实 H5 请求：

```text
web/index.html sendMessage()
→ 每次用户操作生成一次 request_id
→ POST /v1/messages/stream
→ AgentSessionService.send_message(... request_id)
```

关键位置：

- `web/index.html:sendMessage()`
- `finagent/api/server.py:_stream_message()`
- `finagent/api/service.py:send_message()`

当前缺口：

- `AgentSessionService.send_message()` 持有 request_id。
- 调用 `record.agent.handle(text)` 时没有把 request_id 传入 Agent。
- 因此 Shadow Integration 无法知道触发请求号。

实施方向：

- 不使用时间或随机值伪造 request_id。
- 由 Service 将真实 request_id 显式传入 Agent 执行上下文。
- 推荐兼容式签名：`BankingAgent.handle(text, *, request_id: str = "")`。
- 直接 Agent 测试也必须显式传 request_id 才能验证持久化实验链路。

### 4.2 application_id 来源

正式信用卡申请工具在结果 data 中返回：

```text
application_id
card_id
decision
reference_limit
kind=credit
```

关键位置：

- `finagent/tools/card_tools.py:CardApplyTool._apply_credit_card()`
- `finagent/agent/core.py:_execute()`

当前 `_execute()` 已经知道：

- 工具是否成功。
- `kind=credit`。
- `result.data.application_id`。

但 `_observe_credit_application_shadow()` 当前只接收 `ToolContext`，没有接收 application_id 或 request_id。

实施方向：

- 仅在正式工具成功且存在合法 application_id 时生成 Shadow Run。
- 工具失败、没有 application_id 或 kind 不是 credit 时不生成记录。
- 已正式处理但决策为“未通过”的申请仍有合法 application_id，可以记录该正式申请对应的 Shadow 运行；不得把它改写成“通过申请”。

### 4.3 触发位置

当前：

```text
BankingAgent._execute()
→ 正式工具成功
→ credit_application_completed=True
→ _observe_credit_application_shadow()
→ 构造 AgentReply
→ AgentSessionService._persist_session()
```

注意顺序风险：

- Shadow 当前在正式 Session 持久化之前执行。
- 如果 Shadow 立即写 SQLite，而随后正式 Session 保存失败，会出现“Shadow 记录存在，但正式申请未持久化”的孤儿实验记录。

推荐设计：

1. Agent 内完成 Shadow 评估。
2. 生成带 application_id/request_id 的待持久化 Shadow Run。
3. 正式 Session 保存成功后，由 Service 把待持久化 Shadow Run 写入同一 SQLite 数据库。
4. Shadow 写入失败只记录错误状态，不改变 AgentReply。
5. 进程在业务保存前中断：不产生 Shadow 成功记录。
6. 进程在业务保存后、Shadow 写入前中断：允许 Shadow 记录缺失，但必须日志可见；Shadow 不是正式审批依据。

不能简单把 Shadow 写入正式业务事务中，因为 Shadow 必须与审批隔离。

## 五、SQLite 与事务机制现状

统一数据访问层：

- `finagent/data/sqlite.py:SQLiteRepository`

当前机制：

- 构造时调用 `_init_schema()`。
- `_connection()` 上下文在正常退出时 commit，关闭连接。
- `PRAGMA journal_mode=WAL`。
- Session 使用 `version` 做乐观并发控制。
- Session payload 和领域投影在同一个 connection/commit 中写入。
- `idempotency` 使用 `INSERT OR IGNORE`、`BEGIN IMMEDIATE` 和唯一键。
- 当前没有单独 migration 版本表。
- 现有 schema 只在发现 `sessions.version` 缺失时做一次 `ALTER TABLE`。

复用原则：

- Shadow 数据使用同一个 `SQLiteRepository` 和同一个 DB 文件。
- 不建立第二套 Shadow SQLite。
- 不修改正式业务表结构。
- 阶段 2 只新增独立 `credit_shadow_runs` 表和索引。

## 六、推荐 Shadow Run 数据模型

建议新增独立领域对象，而不是把所有字段塞进 AuditRecord：

```text
CreditRiskShadowRun
- shadow_run_id
- application_id
- request_id
- owner_ref
- session_id（可选，默认不保存）
- status
- model_status
- reliable_coverage
- approximate_coverage
- potential_coverage
- reliable_feature_count
- approximate_feature_count
- missing_feature_count
- policy_blocked_count
- history_complete
- created_at
- completed_at
- schema_version
- error_code
- persistence_status
- missing_features
- approximate_features
- policy_blocked_features
- diagnostics
- rule_impact = none
```

隐私限制：

- 不保存 User/Card/Account/Transaction 对象快照。
- 不保存手机号、完整卡号、交易原文、交易 ID、商户名称。
- `owner_ref` 使用不可逆内部引用，用于服务端权限校验。
- `session_id` 只在确实无法通过正式 application/owner 关系鉴权时才保存。
- Audit diagnostics 继续使用 code/feature/count/severity，不使用自由文本。

## 七、Shadow Run ID 与重复保护

推荐：

- `shadow_run_id` 使用 `SHADOW-<uuid4hex>`。
- 阶段 1 验证后的逻辑幂等键：

```text
UNIQUE(application_id, request_id)
```

- 同一正式 request_id 重复到达时，现有 H-01 会让正式工具不重复执行，因此 Shadow 也不会重复生成。
- 若内部仍发生重复调用，数据库按唯一键返回已有记录，不插入第二条。
- 同一 application_id 如果未来有新的真实 request_id，应保留为新的 run，不覆盖历史。
- `schema_version` 不进入逻辑唯一键。它描述实验记录/特征契约版本，不应让同一 `request_id` 在代码升级或重试后再次生成记录。
- 如需在新的实验版本下重跑，必须发起新的真实 request_id，并保留为同一 application 的新 run。
- 当前 v0.1 每个正式 application/request 只有一次 Shadow 触发。如果未来同一请求需要并行运行多个实验 profile，应新增 `profile_id` 并把它加入唯一键，而不是复用 `schema_version`。

不能：

- 用时间戳猜 application_id。
- 用用户姓名猜申请关系。
- 用随机 request_id 代替真实请求号。
- 用 `INSERT OR REPLACE` 覆盖历史实验。

## 八、阶段 1 设计：身份关联

阶段 1 不写数据库，只建立完整身份对象和调用传递。

修改范围预计：

- `finagent/agent/core.py`
- `finagent/risk/credit_risk/integration.py`
- `finagent/risk/credit_risk/audit.py` 或新增 Shadow Run 契约文件
- `finagent/api/service.py`
- `tests/ml/test_business_credit_shadow_integration.py`
- 必要时新增独立测试文件

核心改动：

1. Service 将真实 request_id 传入 Agent。
2. `_execute()` 在 card.apply 成功后提取正式 application_id。
3. 将 application_id、request_id 传给 Integration。
4. Integration 生成稳定 Shadow Run 对象。
5. Shadow off 时不生成。
6. 无 request_id、无 application_id、工具失败时不伪造记录。
7. 重复 request_id 的处理在阶段 1 先以现有 H-01 和内存幂等测试验证。

不在阶段 1：

- 不建表。
- 不新增 API。
- 不改 H5。
- 不调用模型。

## 九、阶段 2 设计：SQLite 持久化

预计修改：

- `finagent/data/sqlite.py`
- `finagent/risk/credit_risk/integration.py`
- `finagent/api/service.py`
- 新增 Shadow Run Repository/DTO 模块
- 新增 `tests/ml/test_business_credit_shadow_persistence.py`

Schema 方案：

```sql
CREATE TABLE IF NOT EXISTS credit_shadow_runs (
    shadow_run_id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    owner_ref TEXT NOT NULL,
    status TEXT NOT NULL,
    model_status TEXT NOT NULL,
    reliable_coverage REAL,
    approximate_coverage REAL,
    potential_coverage REAL,
    reliable_feature_count INTEGER NOT NULL,
    approximate_feature_count INTEGER NOT NULL,
    missing_feature_count INTEGER NOT NULL,
    policy_blocked_count INTEGER NOT NULL,
    history_complete INTEGER,
    schema_version TEXT NOT NULL,
    diagnostics_json TEXT NOT NULL,
    missing_features_json TEXT NOT NULL,
    approximate_features_json TEXT NOT NULL,
    policy_blocked_features_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    error_code TEXT NOT NULL DEFAULT '',
    persistence_status TEXT NOT NULL,
    rule_impact TEXT NOT NULL DEFAULT 'none',
    UNIQUE(application_id, request_id)
);
```

索引：

- `shadow_run_id` 主键。
- `application_id`。
- `request_id`。
- `created_at`。

事务与写入：

- 业务 Session 保存成功后由 Service 调用 Shadow Run flush。
- Shadow 写入使用独立事务。
- 写入失败抛出内部持久化错误并由 Integration/Service 转成结构化状态和日志。
- Shadow 失败不能让正式 AgentReply 失败。
- 不把 Shadow 行写进 Session payload。
- 只在成功获得 Shadow 结果后写；不写伪成功记录。
- 重复唯一键读取已有行，不覆盖。

查询能力：

- `load_shadow_run(shadow_run_id)`
- `list_shadow_runs_by_application(application_id)`
- `list_shadow_runs_by_request(request_id)`

阶段 2 会明确测试：

- 重启后读取。
- 重复写入。
- SQLite 写失败。
- 正式审批不被 Shadow 故障影响。

## 十、阶段 3 安全前置条件

当前身份认证结论：

- API 使用共享访问口令和 Session Token。
- Session Token 没有角色语义。
- 没有管理员/内部运营角色。
- `/v1/sessions` 仍接受客户端指定 `user_id`。
- 健康审计 BUG-001 尚未修复。

因此：

**阶段 3 的内部查询 API 在用户身份认证、管理员角色和所有者隔离完成前不得开放。**

阶段 3 前置门槛：

1. 用户身份由服务端可信来源决定。
2. 普通用户不能指定其他人的 user_id。
3. 存在内部运营/管理员角色。
4. 服务端按角色和 owner_ref 执行授权。
5. 不依赖前端隐藏按钮。
6. 不允许通过 application_id 枚举跨用户数据。
7. 500/403/404 不泄漏数据库或内部堆栈。

如果用户不希望在当前阶段修身份认证：

- 不开放 HTTP 实验 API。
- 不开放 H5 实验中心。
- 只保留本地开发/测试内部对象。

## 十一、阶段 4 H5 设计

前置：

- 阶段 3 安全门槛完成。
- 有内部角色。
- 有查询 API。

推荐产品位置：

- 独立“信用风险实验中心”覆盖层或内部视图。
- 不作为普通用户聊天按钮。
- 不把实验记录塞入普通聊天气泡。

普通用户可见：

- 正式申请状态。
- 正式决策。
- 正式额度。
- 正式申请时间线。
- 资料补充提示。

内部人员可见：

- Shadow 开关。
- Shadow run 状态。
- coverage。
- missing/approximate/policy blocked 数量。
- 六个月完整性。
- `model_status=not_called`。
- 最新执行时间和历史列表。

普通用户隐藏：

- application coverage。
- diagnostics。
- artifact hash。
- model version。
- 研究模型状态。

## 十二、阶段 5 端到端验收

测试分三层：

### 单元/契约

- Shadow Run ID。
- application_id/request_id 关联。
- 状态和 coverage 映射。
- 白名单序列化。

### 服务/数据库

- SQLite 创建和查询。
- 唯一约束。
- 重复写入。
- 重启恢复。
- 写入失败隔离。

### HTTP/H5

- 内部授权可以查询。
- 普通用户被拒绝。
- 跨用户 application_id 被拒绝。
- H5 实验中心加载、空状态、错误状态、权限不足。
- 桌面和移动端。

所有测试使用临时数据库。

## 十三、测试组织

当前组织：

- `tests/ml/`：6 个信用风险测试文件。
- 根 `tests/`：51 个业务/API/数据/UI 相关测试文件。
- 共收集 724 tests。

预计新增：

- Stage 1：`tests/ml/test_shadow_run_identity.py` 或扩展现有 integration test。
- Stage 2：`tests/ml/test_shadow_run_persistence.py`。
- Stage 3：`tests/test_shadow_api_security.py`。
- Stage 4：扩展 `scripts/ui_smoke.mjs` 或独立实验中心浏览器测试。

不删除现有测试。

## 十四、分阶段交付安排

| 阶段 | 目标 | 前置 | 是否可立即开始 |
|---|---|---|---|
| 0 | 现状和安全准备 | 无 | 已完成 |
| 1 | application_id/request_id/shadow_run_id 关联 | 阶段 0 | 已完成 |
| 2 | SQLite 持久化和查询底层 | 阶段 1 | 已完成（Repository/Service 内部） |
| 3 | 内部查询 API | 阶段 2 + 身份/RBAC 修复 | 当前受安全阻塞 |
| 4 | 内部 H5 实验中心 | 阶段 3 | 当前受安全阻塞 |
| 5 | 完整 E2E | 阶段 4 | 待阶段 4 |

## 十五、阶段 1 最小实施方案

如果用户确认进入阶段 1，计划按以下顺序：

1. 增加 Shadow Run 不可变契约对象。
2. 为 `BankingAgent.handle()` 增加向后兼容的显式 request_id。
3. `_execute()` 捕获正式 application_id。
4. `_observe_credit_application_shadow()` 接收 application_id/request_id。
5. Integration 生成唯一 shadow_run_id。
6. 保持当前内存 audit，供阶段 1 测试检查。
7. Shadow off、工具失败、缺失 ID 都采用明确的“不生成”路径。
8. 增加 6 类身份关联测试。
9. 只运行信用风险相关测试与卡片回归。
10. 完成后停止，不创建 SQLite 表。

## 十六、Stage 0 未执行事项

- 未修改任何业务代码。
- 未修改数据库。
- 未执行数据库迁移。
- 未新增 API。
- 未修改 H5。
- 未调用模型。
- 未执行全量测试；仅执行测试收集，724 tests collected。
- 未进入阶段 1。

阶段 0 完成，等待确认。

## 十七、阶段 2 实现记录

状态：SQLite 持久化闭环已实现，尚未开放 HTTP API 或 H5。

### 表

`SQLiteRepository._init_schema()` 以 `CREATE TABLE IF NOT EXISTS` 增加：

```text
credit_shadow_runs
```

关键约束：

```text
PRIMARY KEY(shadow_run_id)
UNIQUE(application_id, request_id)
```

索引：

- `(application_id, created_at DESC)`
- `(request_id, created_at DESC)`

表只保存脱敏 Run 契约和必要的查询身份，不保存 User、Card、Account、
Transaction 原始对象，不保存手机号、交易原文或完整账户信息。

### Repository

- 底层 SQLite 方法位于 `finagent/data/sqlite.py`。
- 领域 Repository 位于 `finagent/risk/credit_risk/run_repository.py`。
- 支持按 `shadow_run_id`、`application_id`、`request_id` 查询。

### 写入顺序

```text
Shadow 计算
→ Run 进入 Integration 待持久化队列
→ 正式 AgentReply 构造
→ AgentSessionService._persist_session() 成功
→ flush_pending()
→ SQLite credit_shadow_runs
```

如果正式 Session 保存失败：

```text
discard_pending_persistence(
    reason=business_session_not_saved
)
```

不会把 Shadow 写入正式业务提交失败的请求。

### 异常策略

- Shadow Service 异常：生成并持久化 `shadow_error` Run。
- Shadow SQLite 写入异常：保留内存 `ShadowPersistenceFailure` 和错误日志，不改变正式回复。
- 唯一约束竞争：读取并返回已存在的规范 Run，不覆盖历史。
- 不同 Integration 实例：依赖 SQLite 唯一约束，不依赖进程内锁。

### 安全边界

- 未注册 Shadow HTTP 查询路由。
- 未修改 H5。
- 未调用 UCI 模型。
- 未修改 RiskEngine、CardApplyTool、额度或正式审批结果。
- 未在正式数据库执行迁移或测试。
