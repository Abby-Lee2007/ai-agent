# AI Banking Agent 实验成果接入与产品化审计

审计日期：2026-10-30  
审计范围：`C:\Users\HUAWEI\Desktop\金融科技导论\ai_banking_agent`  
审计性质：产品架构、全栈调用链、数据持久化、H5 呈现专项审计  
执行边界：只读审计与方案设计，不修改业务代码、风控规则、模型或数据库

## 一、核心结论

当前项目并不是“只有聊天窗口的 Demo”，也不是“所有成果都已经产品化”。

更准确的产品形态是：

> 一个已经具备 6 类业务导航、9 个全局能力、56 个能力入口、多种专用业务面板的对话式银行 Agent 原型；核心业务流程已经接入，但信用风险实验成果主要停留在后端执行链，没有形成可查询、可持久化、可运营的实验产品。

最重要的判断：

1. 信用卡正式申请、风控、API/SSE、申请结果卡已经完整接入。
2. Business Contract、Materializer、Shadow Adapter、Shadow Service、Shadow Integration 已经被真实信用卡申请触发。
3. Shadow 结果只保存在 Agent 实例的内存数组中，没有 `application_id` 关联，没有查询 API，没有持久化，也没有 H5 展示。
4. Shadow 默认关闭；即使开启，普通用户也看不到，且当前页面没有内部实验入口。
5. UCI 研究模型和模型预测接口没有接入业务；这是正确的安全边界，但产品上目前也没有“研究模型未调用/仅研究”的可见状态。
6. 页面已有大量专用结果组件，不属于简单消息堆叠；真正缺失的是“内部实验成果中心”和“实验历史查询”。

## 二、审计方法与验证证据

### 2.1 静态追踪

沿以下方向逐层追踪：

- 环境配置与默认开关。
- Agent 初始化。
- 工具执行结束后的 Shadow 调用。
- Materializer、Contract、Adapter、Service。
- AuditRecord 生命周期。
- Session 持久化 payload。
- API 路由和 SSE 消息。
- H5 消息分发和专用组件。
- 相关单元、集成和浏览器测试。

### 2.2 实际执行

信用风险专项测试：

```text
109 passed
```

API、请求生命周期与卡片测试：

```text
71 passed, 3 subtests passed
```

真实浏览器信用卡申请流程：

- 在临时 SQLite 中启用 `FINAGENT_CREDIT_SHADOW_MODE=shadow`。
- 通过 H5 完成完整信用卡申请。
- 正式结果卡显示成功。
- 实际申请编号：`CARD000005`。
- 页面没有 Shadow、coverage、artifact、diagnostic 等内部内容。
- 页面没有实验成果中心入口。

一次真实 Shadow 执行结果：

```text
Shadow status: insufficient_data
可靠特征: 1
近似特征: 1
缺失特征: 20
政策阻断: 1
reliable coverage: 4.35%
approximate coverage: 4.35%
potential coverage: 8.70%
six_month_coverage_complete: false
model_result_status: not_called
重启后审计数量: 0
```

## 三、成果接入矩阵

说明：

- “已持久化”指业务结果写入 Session、SQLite 或可恢复服务端状态。
- “已展示在 H5”指普通用户或当前 Demo 页面可见。
- 当前状态只使用任务规定的七种状态。

| 成果 | 已实现 | 已调用 | 已持久化 | 已接入 API | 已展示在 H5 | 当前状态 |
|---|---|---|---|---|---|---|
| Business Contract v0.3 | ✅ | ✅ | ❌ | ❌ | ❌ | 仅后端实现 |
| Business Feature Materializer v0.1 | ✅ | ✅ | ❌ | ❌ | ❌ | 仅后端实现 |
| Business Shadow Adapter | ✅ | ✅ | ❌ | ❌ | ❌ | 仅后端实现 |
| Credit Risk Shadow Service v0.1 | ✅ | ✅ | ❌ | ❌ | ❌ | 仅后端实现 |
| Shadow Integration | ✅ | ✅ | ❌ | ❌ | ❌ | 配置关闭 |
| Shadow AuditRecord | ✅ | ✅ | ❌ | ❌ | ❌ | 仅后端实现 |
| 信用卡正式申请 | ✅ | ✅ | ✅ | ✅ | ✅ | 完整接入 |
| 正式申请时间线 | ✅ | ✅ | ✅ | ✅ | ✅ | 完整接入 |
| RiskEngine 正式规则 | ✅ | ✅ | 部分 | ✅ | 通过 workflow/trace 间接展示 | 完整接入 |
| Agent 工具调用 | ✅ | ✅ | ✅ | ✅ | ✅ | 完整接入 |
| API 与 SSE | ✅ | ✅ | N/A | ✅ | ✅ | 完整接入 |
| H5 专用业务结果组件 | ✅ | ✅ | N/A | ✅ | ✅ | 部分接入 |
| H-01 请求幂等与页面恢复 | ✅ | ✅ | ✅ | ✅ | ✅ | 部分接入 |
| UCI 研究模型推理 | ✅ | ❌ | ❌ | ❌ | ❌ | 代码存在但未接入 |
| Legacy Credit Feature Builder | ✅ | ❌ | ❌ | ❌ | ❌ | 代码存在但未接入 |
| 通用后端审计日志 | ✅ | ✅ | ✅ | ❌ | ❌ | 仅后端实现 |
| 概率现金流与情景分析 | ✅ | ✅ | 非必要 | ✅ | ✅ | 完整接入 |
| 约束优化与模型质量审计 | ✅ | ✅ | 部分 | ✅ | ✅ | 完整接入 |
| 市场与官方宏观数据 | ✅ | ✅ | 缓存 | ✅ | ✅ | 完整接入 |
| Sandbox 交易生命周期 | ✅ | ✅ | ✅ | ✅ | ✅ | 完整接入 |
| 订阅生命周期 | ✅ | ✅ | ✅ | 间接 | 间接 | 完整接入 |
| 按 application_id 查询 Shadow | ❌ | ❌ | ❌ | ❌ | ❌ | 尚未实现 |
| Shadow 实验历史 | ❌ | ❌ | ❌ | ❌ | ❌ | 尚未实现 |
| 实验成果中心 | ❌ | ❌ | ❌ | ❌ | ❌ | 尚未实现 |
| 普通用户与内部实验权限分层 | ❌ | ❌ | ❌ | ❌ | ❌ | 尚未实现 |

### 矩阵证据索引

| 成果 | 文件与函数 | 代码证据 |
|---|---|---|
| Business Contract | `finagent/risk/credit_risk/business_contract.py` 的 `BusinessCreditFeatureSnapshot`、`FeatureAvailability` | 构造对象时校验 feature 状态、coverage、月份周期和 diagnostics |
| Materializer | `finagent/risk/credit_risk/materializer.py` 的 `BusinessFeatureMaterializer.materialize()` | 构造 Snapshot，提取 `current_credit_limit`、近似 `verified_age`，其余按 missing/policy blocked 记录 |
| Shadow Adapter | `finagent/risk/credit_risk/business_shadow_adapter.py` 的 `adapt_business_credit_snapshot()` | 将 validated snapshot 映射为 `BusinessShadowAssessment`，不升级 approximate |
| Shadow Service | `finagent/risk/credit_risk/service.py` 的 `evaluate_business_shadow()` | 调用 Materializer、Adapter，生成 `BusinessCreditShadowResult` 和 AuditRecord |
| Shadow Integration | `finagent/risk/credit_risk/integration.py` 的 `from_env()`、`observe_credit_application()` | 默认 off；shadow 时调用 Service，并追加内存 audit |
| Shadow Trigger | `finagent/agent/core.py` 的 `_execute()`、`_observe_credit_application_shadow()` | `card.apply` 返回 `kind=credit` 后触发；异常不改变 AgentReply |
| Shadow Audit | `finagent/risk/credit_risk/audit.py` 的 `CreditRiskAuditRecord` | 白名单字段、`rule_impact=none`、无 application_id |
| 正式信用卡申请 | `finagent/tools/card_tools.py` 的 `CardApplyTool._apply_credit_card()` | 创建正式 Card、Account、application_history 和 AgentReply data |
| 正式申请时间线 | `Card.application_history`；`AgentSessionService.get_profile_settings()`；`web/index.html:renderCardEditors()` | 正式时间线进入 Session 快照，通过 profile API 展示在卡片设置中 |
| RiskEngine | `finagent/risk/engine.py` 的 `check_transfer()`、`check_operation()` | Agent 在工具执行前统一评估，正式工具也有二次检查 |
| Agent 工具调用 | `finagent/agent/core.py` 的 `_execute()` | registry 白名单、参数绑定、trace、事务 checkpoint |
| API / SSE | `finagent/api/server.py` 的 `do_POST()`、`_stream_message()` | `/v1/messages`、SSE start/message/done/error，以及请求状态路由 |
| H5 专用组件 | `web/index.html` 的 `appendMessage()` 和各 `render*()` | 按 intent 分发信用卡、账单、预测、理财、健康、事件等组件 |
| H-01 | `web/index.html:sendMessage()`；`AgentSessionService.send_message()`；`/v1/requests` | 稳定 request_id、原子 claim、状态查询和页面恢复 |
| UCI 模型 | `ml/credit_risk/predict.py` | 文件与 artifact 存在，但 Agent、API、H5 均未调用 |
| Legacy Feature Builder | `finagent/risk/credit_risk/feature_builder.py` | 测试和 Service 默认参数仍可用，但当前 Agent 使用 Business Materializer |
| 通用审计 | `finagent/data/store.py:log()`；`finagent/data/sqlite.py:_sync_store_projection()` | Store audit 进入 Session 和 `audit_logs`，无读取 API |
| 概率现金流 | `finagent/analytics/probabilistic_cashflow.py`；`renderForecastPanel()` | 模型结果进入 reply data 并渲染图表、区间和质量指标 |
| 约束优化与模型审计 | `finagent/analytics/portfolio_optimizer.py`、`model_audit.py`；`renderWealthAnalyticsPanel()` | Agent 工具调用、API SSE、H5 面板均存在 |
| 市场与官方数据 | `finagent/data/market.py`、`official_sources.py`；行情弹窗 | API 和 H5 均已消费来源、时间与降级状态 |
| Sandbox 生命周期 | `finagent/data/transaction_lifecycle.py`；交易详情 API/H5 | 生命周期状态持久化并展示 |
| 订阅生命周期 | `finagent/agent/subscription_lifecycle.py`；`renderSubscriptionContent()` | 自动续费状态进入 Agent 回复和订阅面板 |
| application_id Shadow 查询 | 无 | API、Service、AuditRecord 均无 application_id 查询路径 |
| 实验历史 / 实验中心 | 无 | H5 和 capability catalog 无实验中心入口 |

## 四、逐项真实调用链

### 4.1 信用卡正式申请

```text
H5 能力入口或自然语言
→ POST /v1/messages 或 /v1/messages/stream
→ AgentSessionService.send_message()
→ BankingAgent.handle()
→ IntentParser / Planner
→ RiskEngine._assess_step()
→ 用户确认
→ BankingAgent._execute()
→ CardApplyTool.run()
→ CardApplyTool._apply_credit_card()
→ 创建正式 Card、Account 和 application_history
→ AgentReply.data
→ SSE message
→ web appendMessage()
→ renderCreditApplicationResult()
```

关键位置：

- `finagent/api/service.py:send_message()`
- `finagent/agent/core.py:_execute()`
- `finagent/tools/card_tools.py:CardApplyTool.run()`
- `finagent/tools/card_tools.py:_apply_credit_card()`
- `web/index.html:renderCreditApplicationResult()`

结论：正式流程完整接入。

实际结果卡包含：

- 申请编号。
- 正式预审决策。
- 参考额度。
- 用户友好后续提示。

实际缺口：

- `reply.data` 中没有 `submitted_at`。
- `reply.data` 中没有 `application_history`。
- H5 组件支持提交时间，但实际申请结果卡没有显示。
- 时间线需要在“资料与卡片设置”中查看，而不是直接从申请结果卡进入。

### 4.2 正式申请时间线

```text
CardApplyTool
→ Card.application_history
→ DataStore snapshot
→ SQLite session payload / cards projection
→ GET /v1/profile
→ profile.cards[*].application_history
→ renderCardEditors()
→ 卡片设置中的申请时间线
```

结论：后端持久化和 H5 展示均存在，状态为“完整接入”。

### 4.3 Shadow Credit Risk 完整链路

```text
信用卡工具执行成功且返回 kind=credit
→ BankingAgent._execute() 设置 credit_application_completed=True
→ BankingAgent._observe_credit_application_shadow()
→ BusinessCreditShadowIntegration.observe_credit_application()
→ CreditRiskService.evaluate_business_shadow()
→ BusinessFeatureMaterializer.materialize()
→ BusinessCreditFeatureSnapshot 构造时执行 Contract v0.3 校验
→ adapt_business_credit_snapshot()
→ BusinessShadowAssessment
→ CreditRiskAuditRecord
→ BusinessCreditShadowIntegration._audits 内存列表
→ 停止
```

关键位置：

- `finagent/agent/core.py:3288-3289`
- `finagent/agent/core.py:3343-3356`
- `finagent/risk/credit_risk/integration.py:75-110`
- `finagent/risk/credit_risk/service.py:246-364`
- `finagent/risk/credit_risk/materializer.py:71-376`
- `finagent/risk/credit_risk/business_shadow_adapter.py:37-91`
- `finagent/risk/credit_risk/audit.py:22-64`

重要事实：

- Shadow 在请求线程中同步执行，不是异步后台任务。
- 它位于正式工具执行之后、AgentReply 返回之前。
- Shadow 异常被捕获，不会改变正式业务结果。
- 当前没有模型调用。
- 当前没有数据库写入。

### 4.4 Shadow 审计记录

```text
CreditRiskAuditRecord
→ BusinessCreditShadowIntegration._audits
→ recent_audits()
→ 仅测试代码或服务内部可访问
```

未发生：

- 未写入 `DataStore.audit`。
- 未写入 `audit_logs`。
- 未写入 `Card.application_history`。
- 未进入 `AgentReply.data`。
- 未进入 SSE。
- 未进入 API。
- 未进入 H5。

### 4.5 通用业务审计

```text
Tool / Agent
→ DataStore.log()
→ Store.audit
→ Session snapshot
→ SQLite audit_logs projection
→ 停止
```

结论：有持久化，但没有查询 API 或审计查看页面，状态为“仅后端实现”。

### 4.6 H-01 请求幂等与恢复

```text
H5 sendMessage()
→ 每个用户操作生成一次 request_id
→ SSE /v1/messages/stream
→ AgentSessionService.send_message()
→ SQLite idempotency claim
→ Agent 执行一次
→ completed / processing / outcome_unknown
→ GET /v1/requests/{request_id}
→ H5 recoverPendingRequest()
→ 原结果恢复或安全提示
```

结论：

- 普通消息与 H5 页面恢复已接入。
- 外部渠道映射重启恢复仍有缺口。
- 当前状态为“部分接入”。

### 4.7 概率现金流与约束优化

```text
用户问题
→ Agent intent
→ forecast.cashflow / wealth.optimizer
→ 概率模型或优化器
→ AgentReply.data
→ SSE
→ renderForecastPanel / renderWealthAnalyticsPanel
→ 图表、区间、模型质量、压力测试
```

结论：算法、API、SSE 和 H5 图表已贯通，状态为“完整接入”。

### 4.8 市场与官方数据

```text
能力入口
→ Agent 工具或 API
→ 公开/持牌行情适配器
→ 来源和新鲜度信息
→ SSE
→ 行情弹窗或消息面板
```

结论：完整接入；无持牌源配置时明确降级。

### 4.9 Sandbox 交易生命周期

```text
资金工具
→ Sandbox 执行
→ 生命周期状态
→ Transaction
→ Session snapshot
→ SSE
→ 交易详情和生命周期展示
```

结论：完整接入。

## 五、信用风险实验专项结论

### 5.1 Shadow 默认状态

默认关闭：

```text
FINAGENT_CREDIT_SHADOW_MODE 未设置
→ mode=off
```

关键证据：

- `integration.py:from_env()` 默认值为 `"off"`。
- `test_business_shadow_defaults_off_from_environment()` 明确验证。

产品缺口：

- 该环境变量没有出现在 `.env.example`。
- 当前项目没有自动加载 `.env`。
- 普通用户和运营人员都无法在页面上看到当前开关状态。

### 5.2 开启后是否真正执行

是。

在临时环境设置 `FINAGENT_CREDIT_SHADOW_MODE=shadow` 后，通过真实信用卡申请流程确认：

- 正式申请成功。
- Shadow 审计记录增加 1 条。
- `model_result_status=not_called`。

但该执行只对同一个 Agent 实例的内部测试代码可见。

### 5.3 可提取字段

实际覆盖：

- Reliable：`current_credit_limit`。
- Approximate：`verified_age`。
- Policy blocked：`gender`。
- Missing：`education_level`、`marital_status` 和全部 18 个六个月历史字段。

实际比例：

- Reliable：1/23 = 4.35%。
- Approximate：1/23 = 4.35%。
- Potential：2/23 = 8.70%。

### 5.4 数据限制

- Materializer 不生成正式账单周期。
- 普通交易不会被当成账单。
- 信用卡还款交易即使存在，也只产生限制诊断，不会构造 X6-X23。
- `customer_type` 在 Agent 集成中被硬编码为 `unknown`。
- 当前没有新客户/老客户的可靠业务来源。
- 性别被政策阻断，不参与信用特征。

### 5.5 是否生成真实实验记录

是，但只在内存。

每次开启 Shadow 的信用卡申请会生成一个 `CreditRiskAuditRecord`。

它不是完整实验快照：

- 不含 `application_id`。
- 不含 `request_id`。
- 不含业务用户引用。
- 不含 feature snapshot。
- 不含 source manifest。
- 不写入数据库。

### 5.6 服务重启后是否保留

否。

实测：

- 申请完成后内存审计数量：1。
- 重启并恢复同一 Session 后数量：0。

### 5.7 是否能按 application_id 查询

不能。

- AuditRecord 没有 `application_id` 字段。
- API 没有信用风险实验查询路由。
- H5 没有查询入口。
- 当前 `/v1/requests/{request_id}` 只查询请求生命周期，不是信用实验记录。

### 5.8 H5 是否能查看实验状态和覆盖

不能。

实际浏览器结果：

- 正式信用卡结果卡出现。
- 页面没有 Shadow 状态、coverage、diagnostic 或实验历史入口。
- `web/index.html` 中没有任何 Shadow/credit-risk audit 消费逻辑。

### 5.9 是否区分正式审批与 Shadow

后端区分正确：

- Shadow 不进入 AgentReply。
- Shadow 不进入 SSE。
- Shadow 不改变 decision、score、reference_limit。
- Shadow 异常不影响正式结果。

H5 也正确：

- 只展示正式申请结果。
- 不把 coverage 当审批理由。

当前问题不是“混用”，而是“Shadow 完全没有内部产品出口”。

### 5.10 UCI 模型状态

- Pipeline 和预测代码存在。
- Shadow v0.1 明确不加载 artifact。
- 不调用 `predict_one()`。
- `model_version` 和 `artifact_sha256` 保持 `None`。

产品上应明确写“研究模型未调用”，不能把当前 coverage 页面称为模型预测结果。

## 六、H5 产品呈现审计

### 6.1 左侧导航

实际加载：

- 6 个业务分类。
- 9 个全局能力。
- 47 个分类动作。
- 总计 56 个动作。

分类：

1. 智能转账
2. 账单分析
3. 理财操作
4. 卡片管理
5. 订阅管理
6. 人际礼物

全局能力：

- 财务数字孪生
- 主动财务管家
- 财务健康分
- 官方宏观数据
- A 股市场行情
- 主动事件中心
- 账务对账
- 每日简报
- 全部能力

动作类型：

- 52 个 prompt 类型。
- 3 个 panel 类型。
- 1 个 upload 类型。

审计结论：

- 业务导航与后端工具目录基本一致。
- 未发现纯装饰性、没有任何点击处理的按钮。
- 这不等于 56 个动作都完成了真实端到端测试。
- 当前不存在“实验成果”分类或入口。

### 6.2 专用展示组件

H5 已具备多种专用组件，不是简单文本堆叠：

- 信用卡申请结果卡。
- 上传质量与人工核对表。
- 异常交易面板。
- 现金流与概率区间图。
- 理财推荐和对比。
- 理财风险分析。
- 财务健康分。
- 主动财务管家。
- 风险处置 playbook。
- 账务对账。
- AA 收款状态。
- 订阅列表。
- 礼物方案。
- 资料与卡片时间线。
- 工作流条。

### 6.3 当前 UI 形态

当前 H5 是：

> “对话主轴 + 左侧能力导航 + 专用业务面板 + 设置/事件/行情弹窗”的混合信息架构。

不是单纯的聊天消息堆叠。

仍然缺失：

- 内部实验成果中心。
- Shadow 运行历史。
- 数据覆盖率历史。
- 申请 ID 与实验记录关联。
- 普通用户与内部管理员视图隔离。

### 6.4 只有入口没有完整功能的情况

本次没有发现“完全没有业务实现”的导航按钮。

但存在“后端有成果，前端无入口”的反向问题：

- Shadow 执行状态。
- Shadow audit。
- 字段覆盖率。
- 缺失字段诊断。
- 实验历史。
- 通用审计日志。
- UCI 研究模型未调用状态。

也存在“有组件但没有接上真实数据”的细节：

- 信用结果卡支持提交时间，但真实 `CardApplyTool` 返回数据没有 `submitted_at`。
- 信用结果卡不提供正式申请时间线入口；时间线需要进入资料设置或额外提问。

## 七、数据持久化缺口

### 已持久化

- 正式信用卡申请和 `application_history`。
- 正式账户、卡片、交易。
- Session history/transcript。
- 请求幂等状态。
- 通用 `DataStore.audit` 和 `audit_logs`。
- 上传批次和导入交易。

### 未持久化

- Shadow `CreditRiskAuditRecord`。
- BusinessCreditFeatureSnapshot。
- BusinessShadowAssessment。
- Shadow 覆盖率历史。
- Shadow 与 application_id 的映射。
- 实验模式配置快照。

### 产品后果

- 页面刷新或服务重启后无法展示实验历史。
- 无法回答“某笔信用卡申请当时 Shadow 看到了什么”。
- 无法比较不同版本的 feature coverage。
- 无法运营或复盘 Shadow 数据质量。
- 无法证明模型始终未被调用，除非保留运行记录。

## 八、推荐产品化架构

### 8.1 普通用户界面

应该展示：

- 正式申请编号。
- 正式申请状态和下一步。
- 正式预审结果、参考额度。
- 申请资料完整度，但只使用业务资料清单口径。
- 需要补充的材料。
- 申请时间线。

不应该展示：

- Shadow status。
- Feature coverage。
- 缺失的 UCI X6-X23 字段。
- Artifact hash。
- Diagnostic code。
- 模型版本。

普通用户看到的“数据不足”应是：

> “信用卡申请资料还需补充”

而不是：

> “23 项特征只覆盖 8.7%”。

### 8.2 内部实验管理界面

建议作为独立的内部 Experiment Center：

- 默认不出现在普通用户左侧导航。
- 通过管理员权限或本地实验开关启用。
- 可按 application_id 查询。
- 可查看 Shadow 状态、coverage、缺失字段和 diagnostics。
- 可查看 `model_result_status=not_called`。
- 可明确标记“研究实验，不参与审批”。

不应展示：

- 手机号。
- 完整卡号。
- 交易原文。
- 商户明细。
- 用户身份信息。

### 8.3 推荐查询对象

未来可建立：

```text
CreditRiskShadowRun
- shadow_run_id
- application_id
- request_id
- feature_version
- mode
- status
- model_name
- model_version
- artifact_sha256
- reliable/approximate/missing/policy blocked counts
- coverage ratios
- six_month flags
- diagnostics summary
- evaluated_at_utc
- created_at
- rule_impact = none
```

不保存原始业务对象；只保存脱敏引用和白名单字段。

### 8.4 推荐 API

内部 API 建议：

```text
GET /v1/internal/credit-risk/applications/{application_id}
GET /v1/internal/credit-risk/runs?application_id=...
GET /v1/internal/credit-risk/runs/{shadow_run_id}
```

要求：

- 独立管理员认证或本地实验开关。
- 与普通用户 Session 身份隔离。
- 严格响应 DTO。
- 默认关闭。

不需要把 Shadow 放进 `/v1/messages` 回复。

### 8.5 推荐 H5

不建议把实验数据塞进普通信用卡申请结果卡。

建议新增一个独立的内部覆盖层：

- “实验运行”
- “字段覆盖”
- “数据质量”
- “诊断”
- “运行历史”

申请结果卡可增加一个仅后台可见的引用入口：

> 查看内部实验记录

普通用户模式不渲染。

## 九、已有成果的产品价值判断

### 应面向普通用户

- 正式信用卡申请结果。
- 正式申请进度。
- 现金流预测。
- 财务健康。
- 主动事件。
- 账单和消费分析。
- 转账、理财、订阅、卡片管理。
- 请求恢复状态。

### 应面向内部运营/实验人员

- Business Contract。
- Materializer。
- Shadow Adapter。
- Shadow Service。
- Shadow Integration。
- Shadow AuditRecord。
- Feature coverage。
- Data diagnostics。
- UCI 模型是否加载/调用的研究状态。

### 应完全保持研究态

- UCI 23 项模型。
- Balanced Logistic Regression。
- 模型 artifact。
- 模型概率。
- 研究用 feature contract。

## 十、接入问题清单

### INT-01 Shadow 没有 application_id

- 现状：只有一个匿名 AuditRecord。
- 影响：无法按正式申请查询。
- 应优先修复。

### INT-02 Shadow audit 只存在内存

- 现状：重启即丢失。
- 影响：无法形成实验记录和历史。
- 应优先修复。

### INT-03 没有信用风险查询 API

- 现状：只有请求生命周期查询。
- 影响：前端无法读取。
- 应在持久化模型确定后修复。

### INT-04 没有 H5 实验入口

- 现状：普通用户和内部人员都看不到。
- 影响：实验成果无法展示，也无法运营。
- 应建立内部模式。

### INT-05 Shadow 开关无产品状态

- 现状：仅环境变量，默认 off，且 `.env` 当前不会自动加载。
- 影响：开启状态不可见，容易出现“以为开了其实没开”。
- 应增加明确的内部状态和启动诊断。

### INT-06 正式结果卡缺少提交时间

- 现状：H5 支持 `submitted_at`，真实工具不返回。
- 影响：结果卡信息不完整。
- 应补正式业务字段，不使用 Shadow 数据。

### INT-07 正式申请时间线不在结果卡链路内

- 现状：卡片设置中可见，申请结果卡没有后续入口。
- 影响：用户需要额外操作或提问。
- 建议在正式结果卡增加“查看申请进度”。

### INT-08 通用审计无产品出口

- 现状：写数据库，无查询 API 或页面。
- 影响：可追溯性只对开发者可见。
- 应区分普通请求审计与内部实验审计。

### INT-09 UCI 模型状态不可见

- 现状：`not_called` 仅出现在内部 AuditRecord。
- 影响：评审或运营无法确认模型没有影响审批。
- 内部实验中心应明确展示“未调用”。

### INT-10 缺少真实端到端测试

- 现状：ML 测试主要直接调用 Agent；UI Smoke 用信用卡 fixture。
- 影响：关闭或开启 Shadow 的完整 API + H5 链路证据不足。
- 应新增真实浏览器端到端测试。

## 十一、推荐验证场景

未来产品化后应验证：

1. Shadow off：完整申请后没有实验记录。
2. Shadow shadow：完整申请后恰好生成一条脱敏记录。
3. 记录包含 application_id 和 request_id。
4. application_id 可通过内部 API 查询。
5. 重启后仍可查询。
6. 普通用户 H5 不显示 Shadow。
7. 内部管理员 H5 可见实验结果。
8. Shadow 前后正式 decision、score、reference_limit 完全一致。
9. 模型加载次数为 0。
10. 数据库中没有 artifact hash、手机号、卡号、交易原文。
11. 重复请求不会生成重复 Shadow 记录。
12. 同一 application_id 多次评估按 run 区分，不覆盖历史。

## 十二、最终判断

### 已真正产品化的部分

- 正式信用卡申请。
- 申请时间线及资料设置。
- 转账、账单、理财、卡片、订阅、礼物等主要业务流程。
- 概率现金流、约束优化和模型质量展示。
- 市场与官方数据。
- API/SSE 和部分请求恢复。

### 已接到后端但未形成产品的部分

- Business Contract。
- Materializer。
- Shadow Adapter。
- Shadow Service。
- Shadow Integration。
- Shadow AuditRecord。

### 只存在于代码/测试的部分

- UCI 模型推理接入。
- Shadow 历史查询。
- 实验成果中心。
- 内部实验管理模式。

结论：

> 当前信用风险实验的算法与后端治理完成度较高，但产品完成度较低。下一步不应继续扩展模型，而应先把“申请 → Shadow → 持久化 → 查询 → 内部展示”这条已有链路闭合成可运营产品。

本轮只生成审计文件，不修改业务代码。等待确认后再进入产品化实施。
