# AI Banking Agent

一个使用 Python 标准库实现的智能银行代理原型，包含对话式银行业务、账单识别、
风险控制、概率现金流、收益与风险分析、约束组合优化、数据质量审计和 H5 页面。

> 本项目是赛事与教学 Demo。账户、余额、流水、银行卡、持仓、交易和卡片申请均为模拟数据，
> 不接入真实银行、征信、支付或证券交易系统，不能用于真实资金业务。

## 1. 当前版本

- 项目版本：`0.2.0`
- Python：3.9+
- 核心运行：仅使用 Python 标准库
- 前端：单文件 `web/index.html`
- 数据持久化：SQLite 会话快照 + JSON 领域快照
- 已注册工具：61 个
- 最近全量测试：557 个通过，1 个跳过
- 页面回归：1440×1000 桌面端、319×844 移动端通过

## 2. 已实现能力

### 对话与 Agent

- 规则意图识别，可选接入 OpenAI 兼容的大模型接口。
- LLM Tool Calling 使用工具白名单；大模型不能改写多步骤执行计划。
- 规则解析出的金额、日期、账户、银行卡和收款人等硬参数不会被模型静默覆盖。
- 支持缺槽追问、暂停、恢复、取消、二次确认和模拟短信验证。
- 工作流记录 Agent 阶段、工具参数摘要、执行状态和失败原因。
- 大模型不可用或调用失败时回退到本地规则解析。

### 银行业务

- 账户余额、银行卡列表、客户画像。
- 外部转账、本人银行卡互转、定时转账、AA 收款。
- 储蓄卡绑定、储蓄卡开户、信用卡申请、卡片激活状态和生命周期。
- 信用卡额度、账单、还款、冻结、解冻、挂失、补卡和交易开关。
- 卡号、手机号、密码在对话历史和工具轨迹中脱敏。
- 本人卡互转同时更新转出卡、转入卡、关联账户和流水。

### 账单识别与分析

- 支持图片、TXT、CSV、XLSX、XLSM。
- 图片优先尝试可选多模态模型，其次使用 Windows 内置 OCR。
- 支持字段级置信度、人工校正、金额守恒检查、去重和确认后导入。
- 支持消费分类、月度/年度报告、关键词检索和异常交易分析。
- 上传文件限制为 8MB，并检查扩展名与文件内容是否匹配。

### 订阅、礼物与跨场景

- 订阅识别、清单、涨价提示、取消、恢复、周期变更和续费提醒。
- 生日礼物问答、具体商品推荐、禁忌过滤和“换一批”。
- 外部市场礼物使用确定的估算市场价；淘宝最终价格和库存不在系统内保证。
- 生日订单按“锁定资金 → 预约任务 → 到期模拟扣款”执行。
- 生日问答会自动新增或更新“关心的人”资料。
- 生日只有月日时保存为“生日月日”，不伪造出生年份。
- 药物过敏等关键词会进入禁忌，不会被当作礼物兴趣。

### 概率现金流

- 默认 600 条固定种子模拟路径。
- 日常非固定支出使用历史均值、标准差拟合 Gamma 抽样。
- 工资事件使用历史变异和缺发概率扰动。
- 订阅、信用卡还款、定时任务和用户情景事件视为确定性事件。
- 分别计算终点余额与全周期最低余额的 P05/P25/P50/P75/P95。
- 分别返回终点负余额、整条路径至少一次负余额和逐日负余额概率。
- 分别提供最低余额尾部与终点损失尾部，避免混用两组路径。
- 输出 CVaR5、输入指纹、模型版本、Python 版本、种子和模拟次数。

### 理财产品与约束优化

- 风险测评包含 6 道计分题和 1 道一致性校验题。
- 产品数据同时支持演示参数和公开市场数据。
- 公开行情支持腾讯财经、东方财富等来源，并标记行情时间、缓存和新鲜度。
- 产品分析包含收益、年化波动、最大回撤、Sharpe、均线趋势、费率和流动性。
- 理财推荐输出两层兼容结构：
  - `portfolio_optimization`：5% 权重网格、硬约束、独立复核器。
  - `optimization`：10% 权重网格的兼容实现。
- 硬约束包括风险等级、期限、现金缓冲、起投金额、单产品集中度、压力回撤和 CVaR5。
- 无可行组合时返回现金安全方案，不自动放宽约束。
- 推荐结果不构成投资建议，也不是收益承诺。

### 数据质量与模型审计

- 交易、银行卡、订阅、产品和持仓分别计算：
  - 必填字段完整率
  - 数据时效
  - 异常率
  - 样本覆盖率
- 关键数据为空、过期、异常率过高或覆盖率不足时，个性化理财执行会被阻断。
- 演示数据可能得到很高的快照质量分，但这不代表真实银行数据质量。
- OCR 识别质量与模型数据质量使用不同字段，互不覆盖。
- 提供：
  - `scripts/run_quality_report.py`
  - `scripts/run_model_audit.py`
  - `scripts/audit_fairness.py`

### 页面与接口

- H5 对话页面、资料与卡片设置、上传历史、交易详情和工作流详情。
- 会话令牌、可选的网页访问口令、请求限流和响应安全头。
- SQLite 会话持久化、版本号乐观锁和请求幂等。
- API 与企业微信/沙箱回调适配。
- 回调必须配置签名密钥；未配置时默认拒绝。

## 3. 实际调用链

```text
用户输入
  -> IntentParser
  -> Planner
  -> 可选 LLM Tool Calling
  -> 槽位校验
  -> 风控/二次确认/身份验证
  -> Tool.run()
  -> 账户、流水、卡片、目标或任务更新
  -> SQLite 会话快照
  -> AgentReply / H5 结构化展示
```

概率现金流调用链：

```text
forecast.cashflow
  -> build_cashflow_forecast()
  -> simulate_cashflow_paths()
  -> summarize_probability_paths()
  -> 终点/最低余额分位、负余额概率、CVaR5、审计信息
```

理财推荐调用链：

```text
wealth.recommend
  -> 风险测评校验
  -> 数据质量闸门
  -> 产品筛选与公开行情增强
  -> optimize_within_constraints()
  -> verify_portfolio_solution()
  -> H5 组合、约束和风险展示
```

## 4. 快速开始

### 环境

- Python 3.9 或更高版本
- Windows 图片 OCR 需要系统自带 OCR 能力
- 页面回归脚本需要 Chrome 或 Edge

默认不需要安装第三方 Python 包。

### 启动网页服务

```powershell
python run_api.py --host 127.0.0.1 --port 8000
```

打开：

```text
http://127.0.0.1:8000
```

需要局域网访问时：

```powershell
python run_api.py --host 0.0.0.0 --port 8000
```

### 启动命令行

```powershell
python run.py
```

固定演示日期：

```powershell
python run.py --today 2026-11-08
```

重新生成演示数据：

```powershell
python run.py --fresh
```

### 配置环境变量

复制 `.env.example` 为 `.env`，或直接配置系统环境变量。

常用配置：

| 变量 | 作用 | 默认 |
|---|---|---|
| `FINAGENT_LLM_MODE` | `auto` / `on` / `off` | `auto` |
| `FINAGENT_LLM_API_KEY` | OpenAI 兼容接口密钥 | 空 |
| `FINAGENT_LLM_BASE_URL` | 大模型接口地址 | DeepSeek 兼容地址 |
| `FINAGENT_LLM_MODEL` | 模型名 | `deepseek-chat` |
| `FINAGENT_LLM_TOOL_CALLING` | 是否启用受控 Tool Calling | `true` |
| `FINAGENT_MARKET_MODE` | `live` / `off` | `live` |
| `FINAGENT_WEBHOOK_SECRET` | Webhook HMAC 密钥 | 空，回调默认拒绝 |
| `FINAGENT_WEB_PASSWORD` | 网页/API 访问口令 | 空 |
| `FINAGENT_ENFORCE_SESSION_TOKENS` | 强制会话令牌 | `1` |
| `FINAGENT_RATE_LIMIT_PER_MINUTE` | 单 IP 每分钟请求上限 | `120` |
| `FINAGENT_DB_PATH` | SQLite 文件路径 | `runtime_data/finagent.db` |
| `FINAGENT_SCHEDULER_INTERVAL_SECONDS` | 定时任务扫描间隔 | `30` |

可选持牌行情代理：

```text
FINAGENT_LICENSED_MARKET_URL=
FINAGENT_LICENSED_MARKET_KEY=
FINAGENT_LICENSED_MARKET_NAME=
FINAGENT_LICENSED_MARKET_TIMEOUT=8
```

未配置持牌代理时，只使用公开数据源，并明确标记数据来源与时效。

## 5. 数据真实性边界

| 数据 | 当前实现 |
|---|---|
| 账户、余额、银行卡、流水 | 程序生成的演示数据 |
| 理财持仓、交易、卡片申请 | 沙箱模拟 |
| 订阅、礼物商户、预约订单 | 模拟计划和模拟扣款 |
| A 股/ETF/债券行情 | 腾讯财经公开接口 |
| 场外基金净值 | 东方财富公开接口 |
| 人民银行、中国债券、世界银行宏观数据 | 官方公开页面/API 或附带快照 |
| 淘宝礼物价格 | 模型估算，不是实时淘宝成交价 |
| 信用审批、征信、争议、冻结 | 本地模拟流程 |

系统不会把无法取得的实时行情替换成伪造的“实时”数据；无法取得时会明确标记不可用。

## 6. HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/health` | 健康检查 |
| `GET` | `/v1/capabilities` | 能力目录 |
| `POST` | `/v1/sessions` | 创建会话并返回 `session_token` |
| `POST` | `/v1/messages` | 发送消息 |
| `POST` | `/v1/messages/stream` | SSE 消息流 |
| `POST` | `/v1/forecast/simulate` | 现金流情景模拟 |
| `GET` | `/v1/profile` | 资料、卡片和目标 |
| `PATCH` | `/v1/profile` | 修改资料 |
| `POST` | `/v1/cards` | 手工添加演示卡 |
| `PATCH` | `/v1/cards/{id}` | 修改卡片设置 |
| `DELETE` | `/v1/cards/{id}` | 删除无余额卡 |
| `GET` | `/v1/uploads` | 上传批次 |
| `POST` | `/v1/uploads` | 上传账单 |
| `DELETE` | `/v1/uploads/{id}` | 删除上传批次 |
| `GET/PATCH` | `/v1/receipts/pending` | 待核对 OCR 结果 |
| `GET` | `/v1/transactions/{id}` | 交易生命周期 |
| `GET` | `/v1/events` | 主动事件中心 |
| `POST` | `/v1/events/action` | 处理、稍后提醒或忽略事件 |
| `GET` | `/v1/market/overview` | 市场指数概览 |
| `GET` | `/v1/market/status` | 行情来源和状态 |
| `GET` | `/v1/official-data` | 官方宏观数据 |
| `POST` | `/v1/official-data/refresh` | 刷新官方宏观数据 |
| `GET` | `/v1/sandbox/status` | 沙箱执行状态 |
| `POST` | `/v1/sandbox/callbacks` | 签名回调 |
| `POST` | `/v1/channels/wecom/events` | 企业微信事件适配 |

创建会话后，后续请求需携带：

```http
X-Session-Token: <session_token>
```

访问口令开启时还需携带：

```http
X-Access-Token: <FINAGENT_WEB_PASSWORD>
```

## 7. 风控与安全

- 转账、还款、理财申购、卡片冻结/挂失等操作必须二次确认。
- 达到阈值的操作需要模拟短信验证码。
- 付款卡操作需要 6 位交易密码。
- 正在等待短信验证码时，执行前会重新检查卡片状态和风险条件。
- 同一请求使用 `request_id` 防止重复执行。
- 工作流包含失败回滚，多步骤执行中途失败会恢复账务快照。
- 卡片交易密码只保存演示哈希；短信验证码为固定演示值。
- 会话令牌只保存 SHA-256 哈希。
- 回调使用 HMAC 签名；没有签名密钥时默认拒绝。
- 上传原始文件识别后删除，不保存原始账单文件。
- 联系人完整银行卡号不持久化，只保留尾号和掩码。

这些措施用于本地 Demo 的安全边界演示，不等同于生产银行安全体系。

## 8. 测试与评测

全量测试：

```powershell
python -m unittest discover -s tests -q
```

最近结果：

```text
Ran 557 tests
OK (skipped=1)
```

Agent 评测：

```powershell
python scripts/evaluate_agent.py
python scripts/evaluate_agent.py --cases evals/agent_side_effects.json
```

当前结果：

- 意图/文本评测：16/16
- 副作用评测：4/4

数据质量与模型审计：

```powershell
python scripts/run_quality_report.py
python scripts/run_model_audit.py
python scripts/audit_fairness.py
```

页面回归：

```powershell
node scripts/ui_smoke.mjs http://127.0.0.1:8000 ui-artifacts
```

Python 3.9 语法检查：

```powershell
python -m compileall -q finagent run_api.py scripts
```

## 9. 目录结构

```text
ai_banking_agent/
├── run.py
├── run_api.py
├── config.py
├── start.cmd
├── start_web.cmd
├── web.cmd
├── web/
│   └── index.html
├── finagent/
│   ├── agent/
│   ├── analytics/
│   ├── api/
│   ├── data/
│   ├── evaluation/
│   ├── integrations/
│   ├── nlu/
│   ├── risk/
│   ├── tools/
│   └── vision/
├── data/
│   └── official_data_snapshot.json
├── evals/
│   ├── agent_cases.json
│   └── agent_side_effects.json
├── evidence/
├── samples/
├── scripts/
├── tests/
├── USER_GUIDE.md
└── AUDIT_REPORT_2026-10-29.md
```

## 10. 已知限制

- 不使用真实银行、支付、征信或券商接口。
- 账户、流水和理财持仓主要是演示数据。
- 外部礼物价格是估算值，不是淘宝实时价格。
- 市场数据依赖公开接口，可能超时、限流、缺失或延迟。
- 风控阈值和风险问卷为原型参数，不是监管审批模型。
- 概率现金流是透明情景模型，没有长期概率校准。
- CVaR 使用正态解析近似，不是历史组合路径 CVaR。
- 优化器使用有限候选和离散权重点，不保证全局最优。
- 推荐响应暂时保留两套优化结果以兼容旧接口，二者方法和权重步长不同。
- 六客户公平性审计只能提供有限合成案例证据，不能证明真实群体公平。
- 默认不加密本地 SQLite/JSON 文件；部署到共享环境前应补充磁盘和密钥保护。
- OCR 对复杂截图仍可能出错，必须人工校正后才能导入。
- 本机可重复不保证跨 Python 版本、跨浮点平台或外部数据刷新后逐位一致。

## 11. 免责声明

本项目仅用于赛事演示、教学和本地测试。所有资金、账户、交易、卡片、持仓和审批结果
均为模拟结果，不构成投资建议、信用评估、银行服务承诺或任何真实交易依据。
