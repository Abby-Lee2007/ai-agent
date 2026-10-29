# Probabilistic Decision Model Design

## Goal

Upgrade the current deterministic digital twin and heuristic wealth
recommendation into a probabilistic decision model that is constrained,
explainable, and auditable. No new banking business scenarios are added.

## Scope

### P1

- Replace the cash-flow point forecast as the decision input with a
  reproducible empirical distribution.
- Add a constrained portfolio optimizer with cash-buffer, drawdown,
  product-risk, horizon, liquidity, concentration, and budget constraints.
- Penalize the worst 5% portfolio outcome using a CVaR estimate.
- Add a central metric dictionary and data-quality report.

### P2

- Expose 50% and 90% cash-flow intervals in CLI, API payloads, and the H5
  forecast panel.
- Use the probability distribution, not a single point estimate, when
  sizing the cash buffer and investable budget.
- Add fairness, stability, and ablation analysis for the health score,
  risk rating, and optimized recommendation.

## Architecture

### Existing modules retained

- `finagent/analytics/cashflow.py` remains the deterministic baseline.
- `finagent/analytics/financial_health.py` remains the explainable health
  score.
- `finagent/analytics/product_risk.py` remains the transparent product
  risk estimator.
- `finagent/tools/wealth_tools.py` remains the recommendation entry point.
- `finagent/tools/insight_tools.py` remains the advisory tool layer.

### New modules

- `finagent/analytics/quality.py`
  - Owns `METRIC_DICTIONARY`.
  - Builds domain quality reports with missing rate, anomaly rate,
    freshness, and sample coverage.
  - Produces an overall gate status used by forecast and optimization.

- `finagent/analytics/probability.py`
  - Converts the deterministic forecast into seeded empirical paths.
  - Produces daily and terminal `P05/P25/P50/P75/P95` values.
  - Produces negative-balance probability and worst-5% statistics.
  - Enforces non-crossing quantiles.

- `finagent/analytics/optimizer.py`
  - Optimizes deterministic weights across a small candidate pool.
  - Uses hard constraints and a CVaR-penalized risk-adjusted objective.
  - Returns a no-feasible-portfolio fallback rather than relaxing hard
    constraints.
  - Exposes constraint checks and model assumptions in the payload.

- `finagent/evaluation/model_audit.py`
  - Runs fairness, stability, and ablation analyses.
  - Emits machine-readable JSON plus Markdown.

### Data flow

```text
transactions / cards / subscriptions / products
  -> quality report
  -> empirical cash-flow paths
  -> P05/P25/P50/P75/P95
  -> cash buffer from P05 stress
  -> constrained optimizer
  -> optimized allocation + audit report
```

## Decision Model

### Cash-flow distribution

- Lookback: 180 days, with the most recent 90 days used for variable
  spending.
- Simulation count: 400 paths by default.
- Random seed: deterministic hash of user, as-of date, horizon, and
  scenario inputs.
- Variable daily expense: bootstrap from observed daily non-fixed
  spending. If fewer than 10 observations exist, add dispersion based on
  data quality and widen the distribution.
- Salary amount: bootstrap from observed salary samples when available;
  otherwise use the deterministic estimate with a documented uncertainty
  band.
- Fixed events: subscriptions, scheduled tasks, credit-card repayment, and
  scenario purchases remain deterministic.
- Quantiles are sorted before output so `P05 <= P25 <= P50 <= P75 <= P95`.

### Cash-buffer rule

```text
P05 minimum balance is the liquidity stress point.
required_cash_buffer = essential_monthly_expense + max(0, -P05 minimum balance)
available_to_invest = max(0, liquid_assets - required_cash_buffer)
```

The optimizer receives the capped budget. It must never spend money needed
to satisfy the cash-buffer constraint.

### Optimizer

Decision variables are product weights and an optional cash weight in 10%
steps. Hard constraints:

- Sum of weights equals 1.
- Cash weight is at least the required buffer ratio when cash is part of
  the optimization.
- Product risk does not exceed the client level.
- Product term does not exceed the investment horizon.
- Products that are not liquid enough for the horizon are excluded.
- Single-product weight does not exceed 60%.
- Estimated portfolio drawdown does not exceed the client limit.

Objective:

```text
score = expected_return
        - risk_aversion * portfolio_volatility
        - cvar_penalty * worst_5pct_loss
        - transaction_cost
```

Estimated portfolio drawdown is a scenario proxy. Outputs must call it an
estimate and must not claim historical performance.

### No-feasible fallback

If no product combination satisfies the hard constraints, the optimizer
returns a 100% cash allocation with:

- `status = "no_feasible_portfolio"`
- the violated constraint that caused the fallback
- a recommendation to keep cash or revisit the horizon

## Quality Metrics

The metric dictionary records:

- metric id
- definition
- unit
- source domain
- direction
- acceptable threshold

Each quality domain records:

- record count
- missing rate
- anomaly rate
- latest update timestamp
- sample coverage
- status

Overall quality score uses completeness, coverage, freshness, and anomaly
rate. A critical failure blocks personalized optimization and returns a
cash-safe fallback.

## P2 Audit

### Fairness

Simulated personas vary age band, income band, and client risk level. The
audit reports maximum group differences for:

- financial-health score
- risk-rating level
- equity allocation share

The report labels these results as synthetic stress tests, not proof of
real-world regulatory fairness.

### Stability

- Re-run probability paths with several seeds.
- Perturb liquid balance and monthly income by +/-5%.
- Report max absolute change in health score, risk level, equity weight,
  and P05 minimum balance.

### Ablation

- Health score with each dimension removed.
- Risk rating with each scoring dimension removed.
- Recommendation with CVaR penalty removed.
- Recommendation with point cash-flow buffer instead of P05 buffer.

All audit runs are deterministic.

## Testing

- Quantiles are monotonic and repeated runs with the same seed match.
- Different seeds produce bounded but non-identical intervals.
- Quality reports include all required fields and critical warnings.
- Optimizer constraints are enforced independently.
- No-feasible cases return cash-safe fallback.
- Probability distribution changes the optimized equity allocation.
- Fairness, stability, and ablation reports contain every required section.
- Existing API and H5 tests remain green.

## Non-goals

- No real bank or market integration.
- No claim of realized investment performance.
- No guarantee that estimated max drawdown will not be exceeded.
- No replacement of the suitability questionnaire.
