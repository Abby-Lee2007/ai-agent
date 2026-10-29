# 概率现金流、约束优化、数据质量与公平性审查报告

日期：2026-10-29
范围：当前本地工作区 `ai_banking_agent`
原则：结论以实际代码、独立计算和测试为准，不把 README 或演示满分当作生产证明。

## 一、结论摘要

审查开始时，代码库中不存在概率现金流、CVaR、组合优化器、统一数据质量闸门或
六个客户的公平性审计实现。旧模型只有一条确定性现金流路径，理财“组合”主要按适合度分数
分配比例，`portfolio_risk_summary` 还是没有权重的简单平均。

修复前虽然已有 `513` 个测试通过，但测试没有覆盖上述能力，因此测试通过不能证明功能存在。

本轮已补齐这些能力，并修复审查中继续发现的四个模型错误：

1. 现金流起点可能重复计算绑定同一账户的多张卡；
2. “最差 5% 路径”曾混用最低余额筛选和终点损失筛选；
3. 产品风险矩阵中同资产与自身的相关系数曾取 `0.65` 而非 `1.0`；
4. 组合波动曾使用产品波动简单平均，现已改为按权重和相关性计算。

最终结果：全量 `532` 个测试通过、`1` 个环境相关测试跳过，全包行覆盖率 `83%`。

## 二、真实调用链

### 1. 概率现金流

1. 对话意图 `forecast.cashflow`
2. `CashflowForecastTool.run`：`finagent/tools/insight_tools.py`
3. `build_cashflow_forecast`：`finagent/analytics/cashflow.py`
4. `simulate_cashflow_paths`：`finagent/analytics/probabilistic_cashflow.py`
5. `summarize_probability_paths`：计算终点、路径最低点、逐日分位、负余额概率和两组尾部
6. H5 `renderForecastPanel`：`web/index.html`

关键位置：

- `finagent/analytics/cashflow.py:119`
- `finagent/analytics/cashflow.py:542`
- `finagent/analytics/probabilistic_cashflow.py:58`
- `finagent/analytics/probabilistic_cashflow.py:235`
- `web/index.html:4488`

### 2. 数据质量与约束优化

1. `wealth.recommend`
2. `build_data_quality` 先做执行层闸门
3. 生成候选产品及风险指标
4. `optimize_within_constraints` 做 5% 权重网格搜索
5. `verify_portfolio_solution` 独立重算全部硬约束
6. H5 展示优化组合、数据质量和风险指标

关键位置：

- `finagent/tools/wealth_tools.py:316`
- `finagent/tools/wealth_tools.py:816`
- `finagent/analytics/portfolio_optimizer.py:317`
- `finagent/analytics/portfolio_optimizer.py:179`
- `web/index.html:3699`

### 3. 财务健康与公平性

- `build_financial_health` 调用概率现金流，并附带统一数据质量结果：
  `finagent/analytics/financial_health.py`
- 六客户审计调用财务健康、风险问卷和约束优化：
  `finagent/analytics/fairness.py:65`
- 命令行入口：`scripts/audit_fairness.py`

## 三、已确认缺陷与处理

### P1：概率现金流功能缺失

证据：修复前 `cashflow.py` 只构建一条 `series`，没有随机过程、分位数、尾部路径或负余额概率。

影响：无法回答 P05/P95、路径级破产概率和极端损失问题。

处理：新增 600 条固定种子模拟路径；分开终点余额与全周期最低余额分布；加入终点、整周期、
逐日三种负余额概率；加入流动性尾部和终点损失尾部两组 CVaR5。

### P1：起点余额可能重复计算

证据：旧实现直接累加所有有效借记卡/虚拟卡余额。两张卡绑定同一账户时会重复计入。

处理：按 `account_id` 去重；没有关联账户时才回退到卡余额。

位置：`finagent/analytics/cashflow.py:166`

### P1：最差 5% 路径统计口径混用

证据：合成路径测试曾得到两个不同候选：按最低余额最差的路径与按终点损失最差的路径。

影响：同一个“CVaR 尾部”可能来自不同路径集合，解释错误。

处理：

- `worst_liquidity_tail`：按全周期最低余额升序；
- `worst_tail`：按终点损失降序；
- 两个集合分别计算，不相互替代。

位置：`finagent/analytics/probabilistic_cashflow.py:178`

复现：`tests/test_probabilistic_cashflow.py:23`

### P1：理财约束优化缺失

证据：旧 `_quantitative_plan` 按适合度分数归一化，不检查集中度、CVaR、现金缓冲、
期限或不可行解；组合风险摘要使用无权重平均。

处理：

- 新增长仓、现金缓冲、风险等级、期限、起投金额、单产品集中度、压力回撤和 CVaR5 硬约束；
- 目标函数同时惩罚 CVaR、费率和集中度；
- 无可行组合返回 100% 现金；
- 每个结果由独立复核器重算，失败则回退现金。

位置：`finagent/analytics/portfolio_optimizer.py:317`

### P1：统一数据质量与执行闸门缺失

证据：旧 `financial_health.data_quality` 只有样本计数，没有缺失率、异常率、时效、
覆盖率或阻断状态；OCR 质量是另一套独立字段。

处理：新增交易、卡片、订阅、产品、持仓五域评分；关键域缺失、过期、异常率过高或覆盖率
不足时，将总分封顶 49 并阻断个性化理财配置。

位置：`finagent/analytics/data_quality.py:226`
执行闸门：`finagent/tools/wealth_tools.py:316`

### P1：同资产相关系数错误

证据：单产品 100% 权重测试中，模型估算波动为 `3.22%`，而产品自身波动是 `4%`。

根因：同类别相关系数读取为 `0.65`。

处理：相同资产类别相关系数固定为 `1.0`，组合波动改为
`sqrt(w'Σw)`。

位置：`finagent/analytics/product_risk.py:69`

### P2：可重复性和审计字段不足

处理：所有随机过程只使用局部 `random.Random(seed)`；审计包含模型版本、项目版本、
Python 版本、种子、模拟次数、参数、输入指纹、宏观数据状态和复现边界。

位置：`finagent/analytics/probabilistic_cashflow.py:220`
现金流审计：`finagent/analytics/cashflow.py:614`

### P2：公平性证据缺失

处理：新增六个合成客户和单变量实验，覆盖年龄、收入 ±5%、流动资金 ±5%、
保守/激进问卷、短/长投资期限、低/高现金缓冲。

位置：`finagent/analytics/fairness.py:65`

## 四、数学定义

### 概率现金流

- 经验分位：排序后位置 `(n-1)q`，相邻值线性插值。
- `terminal_balance`：每条路径最后一日余额的分布。
- `minimum_balance`：先对每条路径取全周期最小值，再对路径最小值求分布。
- 终点负余额概率：`期末余额 < 0` 的路径数 / 总路径数。
- 整周期负余额概率：路径最小值 `< 0` 的路径数 / 总路径数。
- 逐日负余额概率：当日余额 `< 0` 的路径数 / 总路径数。
- 尾部数量：`ceil(模拟次数 × 5%)`，至少 1 条。
- 终点 CVaR5：最差终点损失 `max(期初余额 - 期末余额, 0)` 的条件均值。

变动支出：

- 使用近 90 天每日非固定支出均值 `μ` 和标准差 `σ`；
- Gamma 形状 `k=(μ/σ)^2`，尺度 `θ=σ^2/μ`；
- 无足够样本时退化为确定性均值，并在数据质量和警告中标明。

### 约束优化

目标值：

```text
预期持有期收益
- risk_aversion × CVaR5损失
- 0.25 × 费率拖累
- 0.01 × 集中度HHI
```

`0.6` 是无量纲系数：每 1 个百分点的 CVaR 损失扣除 0.6 个百分点目标值。

CVaR：

```text
CVaR5_loss = max(volatility × φ(z0.95) / 0.05 - expected_return, 0)
```

这是正态收益假设下的解析 Expected Shortfall，不是历史组合 CVaR。

硬约束：

- 权重和为 100%，权重非负；
- 现金缓冲下限；
- 单产品权重上限；
- 客户风险等级上限；
- 投资期限；
- 产品压力回撤上限；
- 产品起投金额；
- 组合 CVaR5 上限。

CVaR 同时作为硬上限和软惩罚，但用途不同：硬上限保证适当性，软惩罚用于候选排序。
压力回撤与 CVaR 使用不同输入，前者来自产品情景回撤加权，后者来自收益/波动解析式。

### 数据质量

单域分数：

```text
100 × (
  0.45 × 完整率
  + 0.20 × 时效
  + 0.15 × (1 - 异常率)
  + 0.20 × 覆盖率
)
```

域权重：

```text
交易 30% + 卡片 20% + 订阅 10% + 产品 25% + 持仓 15%
```

关键域存在空样本、关键字段缺失率 > 20%、异常率 > 30%、时效 < 0.25 或覆盖率 < 50% 时，
总分封顶 49，禁止个性化执行。

`completeness=1, freshness=0.95, anomaly=0, coverage=1` 的独立计算为：

```text
100 × (0.45 + 0.19 + 0.15 + 0.20) = 99.0
```

## 五、独立实验

### 已知路径分布

五条合成路径验证：

- 终点 P50 为 `20`；
- 路径最低余额 P50 为 `0`；
- 终点负余额概率 `20%`；
- 整周期至少一次负余额概率 `40%`；
- 首日负余额概率 `40%`，次日 `20%`；
- 流动性最差 5% 平均最低余额 `-20`；
- 终点损失 CVaR5 为 `105`，来自终点损失最差路径，而不是最低余额路径。

### 固定种子

相同序列、均值、标准差、250 次模拟和种子重复运行，完整结果字典逐项相等。

### 尾部敏感性

固定其他参数，只把日常支出标准差从 `5` 提高到 `60`，
最低余额 `P50-P05` 的尾部宽度显著增大。

### 风险厌恶

固定候选、期限、现金和全部约束，只把风险厌恶从 `0` 提高到 `3`，
优化后组合 CVaR 不增加。

### CVaR 消融

只切换 `include_cvar_penalty`，其余参数完全一致；关闭惩罚后组合 CVaR 不低于开启时。

### 公平性

六个合成客户运行两次结果一致。收入下降/上升 5% 时健康分分别约 `-2.2/+2.3`；
年龄增加 10 岁和流动资金 ±5% 在当前样例中未造成风险等级跳变。
这只是有限敏感性证据，不是公平性证明。

## 六、界面与回归

- 原现金流面板标题“未来 90 天现金流”保持；
- 原确定性指标、消息顺序和桌面左侧栏保持；
- 新增 50% 和 90% 分位带、分位表、负余额概率和两组尾部；
- 模型数据质量与 OCR 质量使用不同字段和不同标题；
- 1440x1000 与 319x844 自动化通过；
- 页面无横向滚动，消息、优化表和输入区均在视口内；
- 约束优化界面显示资产权重、CVaR、回撤、风险厌恶和硬约束复核状态。

产物位于 `ui-artifacts-audit/`。

## 七、测试与覆盖率

```text
python -m unittest discover -s tests -q
532 tests passed, 1 skipped

Agent 意图评测：16/16
Agent 副作用评测：4/4
Python 3.9 AST：73 files OK
```

覆盖率：

```text
TOTAL 83%
probabilistic_cashflow.py 92%
portfolio_optimizer.py 89%
data_quality.py 83%
fairness.py 99%
cashflow.py 87%
product_risk.py 83%
wealth_tools.py 83%
```

## 八、仍存风险与证据不足

1. Gamma 日支出模型仍是独立同分布假设，没有刻画商户、周末、季节和家庭事件相关。
2. 优化器 CVaR 使用正态解析近似，不是历史路径或厚尾分布的组合 CVaR。
3. 压力回撤使用产品情景回撤的加权近似，不是逐日路径共同模拟出的最大回撤。
4. 优化器最多检查 4 个候选产品，使用 5% 权重网格，不保证全局数学最优。
5. 数据质量中演示数据可能得到 100 分，因此代码明确返回
   `is_production_data_quality=false`；100 分只表示当前快照完整、新鲜和覆盖达标。
6. 六个合成客户无法证明真实群体公平性，也没有受保护群体标注数据。
7. 同环境复现要求相同业务快照、固定时钟、相同官方宏观缓存和相同 Python 版本；
   外部行情或宏观刷新会改变输入。
8. 真实银行、券商、支付和持牌行情的生产级幂等、补偿事务与供应商回执仍需真实环境验证。

## 九、下一阶段五项优先改进

1. 用真实账单训练日支出分布，并加入周末、商户类别和收入发放的块相关。
2. 用历史情景法或厚尾分布计算组合 CVaR，并与正态 ES 做回测。
3. 将现金流概率与组合优化联动：以流动性缺口概率动态约束风险预算。
4. 增加正式优化器状态、候选依赖版本和数据版本锁，实现跨环境回放。
5. 使用真实脱敏群体数据做公平性统计检验，而不是只依赖合成客户。
