# Probabilistic Decision Model Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add reproducible cash-flow quantiles, a constrained portfolio optimizer, data-quality reporting, and P2 fairness/stability/ablation audits.

**Architecture:** Keep the existing deterministic forecast and recommendation flow. Add a probability layer, a quality layer, an optimizer, and an audit layer, then connect them through the existing tools and H5 payloads.

**Tech Stack:** Python 3 standard library, existing `unittest` suite, existing zero-dependency H5.

**Spec:** `docs/superpowers/specs/2026-10-28-probabilistic-decision-model-design.md`

## Global Constraints

- Do not add third-party runtime dependencies.
- Do not add new banking business scenarios.
- All simulated outputs must be labelled as estimates.
- All randomized calculations must accept and report a deterministic seed.
- Existing public response fields must remain backward compatible.
- New production behavior must follow TDD: test first, verify red, implement, verify green.

---

### Task 1: Cash-Flow Probability Layer

**Files:**
- Create: `finagent/analytics/probability.py`
- Create: `tests/test_probability.py`
- Modify: `finagent/analytics/__init__.py`

**Interfaces:**
- Consumes: `DataStore`, provider registry, `Clock`, user id, scenario parameters matching `build_cashflow_forecast`.
- Produces: `build_probability_forecast(...) -> dict` with `quantiles`, `daily_quantiles`, `terminal_quantiles`, `min_balance_quantiles`, `negative_probability`, `worst_5pct`, `scenario_count`, `random_seed`, and `method`.

**Steps:**

- [x] **Step 1: Write failing tests**

```python
def test_quantiles_are_monotonic_and_seeded(self):
    first = build_probability_forecast(..., simulations=120, seed=7)
    second = build_probability_forecast(..., simulations=120, seed=7)
    self.assertEqual(first["terminal_quantiles"], second["terminal_quantiles"])
    self.assertLessEqual(first["terminal_quantiles"]["p05"], first["terminal_quantiles"]["p50"])

def test_distribution_contains_daily_bands(self):
    result = build_probability_forecast(..., simulations=80, seed=9)
    self.assertIn("p25", result["daily_quantiles"][0])
    self.assertIn("p95", result["daily_quantiles"][0])
```

- [x] **Step 2: Run tests and verify missing module failure**

Run: `python -m unittest tests.test_probability -v`

- [x] **Step 3: Implement bootstrap paths**

Use `random.Random(seed)`, empirical daily variable-expense samples, salary samples, and the deterministic event list. Sort all quantiles after simulation.

- [x] **Step 4: Run probability tests**

Run: `python -m unittest tests.test_probability -v`

### Task 2: Metric Dictionary And Quality Report

**Files:**
- Create: `finagent/analytics/quality.py`
- Create: `tests/test_quality.py`
- Modify: `finagent/analytics/__init__.py`

**Interfaces:**
- Produces: `METRIC_DICTIONARY`, `build_data_quality_report(store, providers, clock, user_id) -> dict`.
- Quality report shape: `overall_score`, `status`, `domains`, `warnings`, `as_of`.

**Steps:**

- [x] **Step 1: Write failing quality tests**

```python
def test_quality_report_contains_required_metrics(self):
    report = build_data_quality_report(store, providers, clock, "u_001")
    self.assertIn("overall_score", report)
    for domain in report["domains"].values():
        self.assertIn("missing_rate", domain)
        self.assertIn("anomaly_rate", domain)
        self.assertIn("updated_at", domain)
        self.assertIn("sample_coverage", domain)
```

- [x] **Step 2: Run red test**

Run: `python -m unittest tests.test_quality -v`

- [x] **Step 3: Implement metric dictionary and domain aggregations**

Cover transactions, cards, subscriptions, products, and holdings. Include freshness using ISO timestamps and coverage relative to 180 days.

- [x] **Step 4: Run green test**

Run: `python -m unittest tests.test_quality -v`

### Task 3: Constrained Portfolio Optimizer

**Files:**
- Create: `finagent/analytics/optimizer.py`
- Create: `tests/test_optimizer.py`
- Modify: `finagent/analytics/__init__.py`

**Interfaces:**
- Produces: `optimize_portfolio(products, budget, client_risk, horizon_days, cash_buffer, drawdown_limit, seed, probability_forecast) -> dict`.
- Result includes `status`, `weights`, `allocations`, `expected_return`, `volatility`, `cvar_5pct`, `estimated_drawdown`, `constraints`, `objective_score`, and `assumptions`.

**Steps:**

- [x] **Step 1: Write failing optimizer tests**

```python
def test_optimizer_respects_risk_and_weight_caps(self):
    result = optimize_portfolio(...)
    self.assertLessEqual(max(item["risk_level"] for item in result["allocations"]), 3)
    self.assertLessEqual(max(item["weight"] for item in result["allocations"]), 0.6)

def test_optimizer_falls_back_to_cash_when_infeasible(self):
    result = optimize_portfolio(..., client_risk=1, drawdown_limit=0.001)
    self.assertEqual(result["status"], "no_feasible_portfolio")
    self.assertEqual(result["weights"], {"cash": 1.0})
```

- [x] **Step 2: Run red tests**

Run: `python -m unittest tests.test_optimizer -v`

- [x] **Step 3: Implement grid search and constraints**

Use 10% weight steps, cap candidate count, check product risk, term, liquidity, minimum amount, concentration, budget, and scenario drawdown.

- [x] **Step 4: Run green tests**

Run: `python -m unittest tests.test_optimizer -v`

### Task 4: Tool Integration

**Files:**
- Modify: `finagent/tools/insight_tools.py`
- Modify: `finagent/tools/wealth_tools.py`
- Modify: `tests/test_insights.py`
- Modify: `tests/test_wealth.py`

**Interfaces:**
- Cash-flow tool adds `probability` and `quality` to `reply.data`.
- Wealth tool adds `optimization` and `cashflow_distribution` to `reply.data`.

**Steps:**

- [x] **Step 1: Write failing integration tests**

```python
def test_cashflow_tool_returns_50_and_90_intervals(self):
    reply = self.agent.handle("预测我未来90天的现金流")
    self.assertIn("p05", reply.data["forecast"]["terminal_quantiles"])
    self.assertIn("p95", reply.data["forecast"]["terminal_quantiles"])

def test_wealth_recommendation_uses_probability_forecast(self):
    reply = self.agent.handle("我有5万块，稳健一点怎么配置")
    self.assertIn("optimization", reply.data)
    self.assertIn("p05_min_balance", reply.data["cashflow_distribution"])
```

- [x] **Step 2: Run red tests**

Run: `python -m unittest tests.test_insights tests.test_wealth -v`

- [x] **Step 3: Connect quality, probability, and optimizer**

Use P05 minimum balance to compute the required cash buffer. Preserve old response fields. Return a cash-safe allocation if quality or feasibility blocks optimization.

- [x] **Step 4: Run green tests**

Run: `python -m unittest tests.test_insights tests.test_wealth -v`

### Task 5: P2 Model Audit

**Files:**
- Create: `finagent/evaluation/model_audit.py`
- Create: `scripts/run_model_audit.py`
- Create: `tests/test_model_audit.py`
- Create: `evidence/results/.gitkeep`
- Create: `evidence/report/.gitkeep`

**Interfaces:**
- Produces: `run_model_audit(...) -> dict`.
- JSON sections: `metadata`, `fairness`, `stability`, `ablation`.
- Markdown writer: `write_model_audit_report(report, path) -> Path`.

**Steps:**

- [x] **Step 1: Write failing audit tests**

```python
def test_audit_has_all_required_sections(self):
    report = run_model_audit(...)
    self.assertEqual(set(report), {"metadata", "fairness", "stability", "ablation"})
    self.assertIn("equity_share", report["fairness"]["metrics"])

def test_audit_is_deterministic(self):
    self.assertEqual(run_model_audit(...), run_model_audit(...))
```

- [x] **Step 2: Run red tests**

Run: `python -m unittest tests.test_model_audit -v`

- [x] **Step 3: Implement persona, perturbation, and ablation checks**

Use synthetic personas from seeded data. Include health score, risk rating, and equity allocation differences. Label fairness as a stress test.

- [x] **Step 4: Run green tests**

Run: `python -m unittest tests.test_model_audit -v`

### Task 6: H5, Documentation, And Full Verification

**Files:**
- Modify: `web/index.html`
- Modify: `README.md`
- Modify: `USER_GUIDE.md`
- Modify: `finagent/analytics/__init__.py`

**Steps:**

- [x] **Step 1: Add interval band rendering to the existing cash-flow chart**

Draw P05-P95 as a light band, P25-P75 as a darker band, P50 as the main line, and keep the deterministic line for comparison.

- [x] **Step 2: Add optimizer and quality summaries to the existing panels**

Show cash buffer, P05 minimum balance, expected return, CVaR, estimated drawdown, and constraint status. Do not add a new business page.

- [x] **Step 3: Document model assumptions and non-guarantees**

State that all output is simulated, that drawdown is an estimate, and that fairness is a synthetic stress test.

- [x] **Step 4: Run all tests**

Run: `python -m unittest discover -s tests -t .`

- [x] **Step 5: Run the audit script and UI smoke test**

Run: `python scripts/run_model_audit.py`

Run the existing UI smoke test against a local server if Node.js is available.
