# AI Banking Agent 产品化演进路线图

来源：`PRODUCT_INTEGRATION_AUDIT.md`  
原则：先闭合已有链路，再补 API/H5，最后做实验中心和整体信息架构。  
执行状态：仅规划，不进入开发。

## 一、总路线

```text
P0 修复断开的调用链
  ↓
P1 把已有后端成果接入 API/H5
  ↓
P2 建立实验成果中心和历史
  ↓
P3 优化整个 H5 信息架构
```

不得跳过 P0 直接做视觉页面。否则页面会继续承担“看起来有、实际上不可查询、不可恢复”的风险。

## 二、P0：让已有功能真正可用

### P0-01 为 Shadow Run 建立 application_id 关联

关联问题：INT-01、INT-10

修改文件：

- `finagent/agent/core.py`
- `finagent/risk/credit_risk/integration.py`
- `finagent/risk/credit_risk/audit.py`

实现要求：

- `_execute()` 从 `card.apply` 的 `result.data.application_id` 取正式申请号。
- 将 application_id、request_id 传入 Shadow 调用。
- 保持 business decision 不变。
- Shadow 失败时仍不影响业务。

依赖：

- 无，必须先做。

验收：

- Shadow off 不产生记录。
- Shadow on 每条记录带 application_id。
- 同一 request_id 重放不重复生成记录。
- 正式 decision、score、reference_limit 与 off 完全一致。

回归：

- Card tests。
- Business Shadow Integration tests。
- H-01 request lifecycle tests。

### P0-02 Shadow audit 持久化

修改文件：

- `finagent/data/sqlite.py`
- `finagent/api/service.py`
- `finagent/risk/credit_risk/integration.py`
- 必要时新增内部查询 Repository

当前状态：SQLite 表和领域 Repository 已实现，正式 Session 成功后写入；
HTTP 查询和 H5 展示仍保持关闭，等待认证与管理员角色完成。

实现要求：

- 只持久化白名单字段。
- 不保存 User、Card、Account、Transaction 原始对象。
- 不保存手机号、卡号、交易原文、商户名。
- 使用 `rule_impact=none`。
- 重复请求按 application_id + request_id + run_id 幂等。

依赖：

- P0-01。

验收：

- 服务重启后记录仍存在。
- 普通 Table 中无敏感业务原文。
- Shadow 异常也留下结构化 `shadow_error`。

### P0-03 修正现有信用卡正式结果信息

修改文件：

- `finagent/tools/card_tools.py`
- `finagent/agent/core.py`（仅在需要统一 reply data 时）
- `web/index.html`

实现要求：

- 正式 CardApplyTool 返回 `submitted_at`。
- 不引入 Shadow 数据。
- 结果卡显示可靠的正式提交时间。
- 提供“查看申请进度”的正式业务入口。

依赖：

- 可使用现有 `Card.application_history`。

验收：

- 真实申请结果卡出现提交时间。
- 申请进度可从结果卡进入。
- 非 card.apply 消息不变。

### P0-04 Shadow 配置可见、默认关闭

修改文件：

- `config.py`
- `.env.example`
- `run_api.py`
- 或独立实验配置模块

实现要求：

- 将 `FINAGENT_CREDIT_SHADOW_MODE` 纳入正式配置说明。
- 默认 off。
- 启动时记录“off/shadow”但不打印敏感信息。
- 修复 `.env` 预期加载问题，或明确只支持系统环境变量。

依赖：

- 无。

验收：

- 未配置时 off。
- 非法值启动失败或明确安全回退。
- 内部状态接口可报告当前模式。

## 三、P1：已有后端成果接入 API 和前端

### P1-01 新增内部信用实验查询 API

修改文件：

- `finagent/api/server.py`
- `finagent/api/service.py`
- 新增内部 DTO/Repository 文件

建议接口：

```text
GET /v1/internal/credit-risk/applications/{application_id}
GET /v1/internal/credit-risk/runs/{shadow_run_id}
GET /v1/internal/credit-risk/runs?application_id=...
```

要求：

- 管理员/本地实验权限。
- 普通 Session token 不足以访问。
- 不返回原始业务对象。
- 404、off、shadow_error、insufficient_data 语义清晰。

依赖：

- P0-01、P0-02、P0-04。

验收：

- 按 application_id 返回正式申请关联的实验记录。
- 重启后查询可重复。
- 未授权请求被拒绝。

### P1-02 新增内部实验视图数据模型

修改文件：

- 新增 `finagent/risk/credit_risk/run_contract.py` 或等价模块
- `finagent/api/service.py`

建议对象：

- ShadowRunSummary
- FeatureCoverageSummary
- DiagnosticSummary
- FormalApplicationReference

要求：

- 明确 `formal_business_result`。
- 明确 `research_shadow_result`。
- 不共享同一审批状态字段。

依赖：

- P0-02。

验收：

- API 序列化白名单测试。
- 正式决策与 Shadow 状态无法互相覆盖。

### P1-03 普通用户申请结果增强

修改文件：

- `web/index.html`
- 现有正式申请/时间线 API

实现内容：

- 申请编号。
- 正式状态。
- 正式提交时间。
- 正式参考额度。
- 申请进度入口。
- 用户友好的资料补充提示。

不实现：

- Feature coverage。
- Missing UCI fields。
- Diagnostics。
- Model version。

依赖：

- P0-03。

验收：

- 普通用户无法看到 Shadow 字段。
- 申请结果与正式审批一致。
- 刷新后可从正式业务接口恢复。

### P1-04 内部实验详情面板

修改文件：

- `web/index.html`
- 受控内部 API

实现内容：

- Shadow run 状态。
- Reliable / approximate / missing / policy blocked 数量。
- 三种 coverage ratio。
- `model_result_status=not_called`。
- 重复诊断 code 汇总。
- 正式 application_id。

依赖：

- P1-01、P1-02。

验收：

- off、insufficient_data、shadow_error 状态可区分。
- 不显示手机号、卡号、交易原文。
- `rule_impact=none` 固定展示。

## 四、P2：建立实验成果中心和实验历史

### P2-01 Experiment Center 入口

修改文件：

- `web/index.html`
- `finagent/nlu/capability_catalog.py`
- 内部权限配置

实现要求：

- 普通用户导航不出现。
- 本地开发/管理员模式显示。
- 建议作为独立内部 overlay，而不是混入普通“卡片管理”。

依赖：

- P1-01、P1-02、P1-04。

验收：

- 普通模式无入口。
- 内部模式能看到实验运行列表。
- 刷新后仍能加载历史。

### P2-02 实验历史列表与筛选

修改文件：

- 内部 API
- `web/index.html`

功能：

- 按 application_id。
- 按状态。
- 按时间。
- 按 feature_version。
- 分页。

依赖：

- P1-01。

验收：

- 历史查询稳定。
- 无敏感字段。
- 过滤条件与分页结果一致。

### P2-03 覆盖率与数据质量展示

修改文件：

- `web/index.html`
- 内部 API DTO

建议展示：

- Reliable / approximate / potential coverage。
- 23 项分层图。
- X6-X23 始终缺失说明。
- 数据限制 diagnostics。

禁止：

- 把 approximate 计入 reliable。
- 把 coverage 当审批分。
- 把 missing 补成 0。

依赖：

- P1-02。

验收：

- 真实 4.35% / 4.35% / 8.70% 数据可正确显示。
- 空值、0.0、未计算状态可区分。

### P2-04 按 application_id 串联正式时间线与 Shadow 历史

修改文件：

- API 服务
- `web/index.html`

用户/内部人员行为：

- 正式用户看正式时间线。
- 内部人员可跳转同一 application_id 的 Shadow runs。

依赖：

- P0-01、P0-02、P1-01。

验收：

- 同一申请有唯一正式引用。
- 多次 Shadow 运行按 run 保留。

### P2-05 审计导出

修改文件：

- 内部 API
- 可选导出脚本

要求：

- CSV/JSON 只含白名单字段。
- 不导出敏感业务原文。
- 不影响运行数据库。

依赖：

- P2-02。

验收：

- 导出结构与 API 一致。
- 权限控制有效。

## 五、P3：优化整体 H5 信息架构

### P3-01 区分普通用户与内部实验模式

修改文件：

- `web/index.html`
- `config.py`
- 内部认证/实验开关

要求：

- 普通模式：业务办理。
- 内部模式：实验中心、审计、数据质量。
- 两种模式不能靠隐藏 CSS 冒充权限。

依赖：

- P1/P2 全部后端权限完成。

验收：

- 普通用户无法直接通过 URL 打开内部实验数据。
- 内部模式有明确标识。

### P3-02 普通用户业务结果导航统一

修改文件：

- `web/index.html`

可优化：

- 申请结果卡 → 申请进度。
- 交易结果 → 交易详情。
- 上传结果 → 上传历史。
- 风险结果 → 处置计划。
- 理财结果 → 持仓。

不增加：

- 装饰性卡片。
- 无数据来源的指标。
- 未接入业务的后端概念。

依赖：

- P0/P1 正式业务链路。

验收：

- 每个结果卡都有明确下一步。
- 所有跳转有真实 API 和业务数据。
- 移动端无横向滚动。

### P3-03 实验状态可视化

修改文件：

- `web/index.html`

建议使用：

- 状态标签。
- 覆盖率条形图。
- 缺失特征分组。
- 时间线。

避免：

- 把研究模型包装成信用评分。
- 使用红绿灯暗示正式审批。
- 用“风险高/低”描述 coverage。

依赖：

- P2。

验收：

- Shadow 与正式审批视觉上完全分离。
- 状态色只表达实验运行状态。

## 六、推荐实施批次

### 批次 A：实验记录可追溯

- P0-01。
- P0-02。
- P0-04。

完成后才有资格做实验中心。

### 批次 B：正式申请体验补齐

- P0-03。
- P1-03。

完成后普通用户体验即可闭环。

### 批次 C：内部查询

- P1-01。
- P1-02。
- P1-04。

完成后运营和研发可以查看实验。

### 批次 D：实验中心

- P2-01。
- P2-02。
- P2-03。
- P2-04。
- P2-05。

### 批次 E：信息架构

- P3-01。
- P3-02。
- P3-03。

## 七、全局回归门槛

每个批次必须执行：

```powershell
.\.venv\Scripts\python.exe -B -m pytest tests/ml/test_business_credit_contract.py tests/ml/test_business_credit_materializer.py tests/ml/test_business_credit_shadow_integration.py tests/ml/test_credit_risk_service.py -q
```

```powershell
.\.venv\Scripts\python.exe -B -m pytest tests/test_api_http.py tests/test_request_lifecycle.py tests/test_card.py -q
```

```powershell
node --check scripts/ui_smoke.mjs
```

真实浏览器必须验证：

1. Shadow off。
2. Shadow on。
3. 正式申请完成。
4. 正式结果不变。
5. Shadow API 可查询。
6. 服务重启后记录仍在。
7. 普通用户看不到 Shadow。
8. 内部人员可以看到。
9. H5 刷新恢复行为正确。
10. 320/375/390/430 无横向滚动。

## 八、当前应停止的事情

在 P0/P1 完成前，不应：

- 接入 UCI 模型进行真实审批。
- 给普通用户展示 coverage。
- 把 Shadow 状态写进正式 decision。
- 继续增加新的风险模型。
- 用前端 mock 代替真实实验链路测试。
- 用“文件存在”证明成果已经产品化。

## 九、最终建议

第一优先级不是“做实验中心页面”，而是先让实验记录具备：

```text
application_id
request_id
持久化
可查询
可恢复
```

这五项完成后，实验中心才是真实产品，而不是展示壳。

本轮停止，等待确认后再选择批次实施。
