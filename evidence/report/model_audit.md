# Financial Decision Model Audit

- As of: 2026-10-28
- Audit version: 1.0
- Simulations per persona: 120

## Fairness Stress Test

Synthetic fairness stress test only. It does not prove regulatory fairness on real customers.

| Metric | Max group delta |
|---|---:|
| health_score | 19.2 |
| risk_level | 1.0 |
| equity_share | 0.0 |
| risk_asset_share | 0.0 |

## Stability

| Check | Value |
|---|---:|
| max_health_score_delta | 2.3 |
| max_risk_level_delta | 0 |
| max_p05_min_balance_delta | 8790.4 |
| max_equity_share_delta | 0 |
| max_risk_asset_share_delta | 0 |
| seed_p05_range | 1405.78 |

## Ablation

### Health Score

| Removed dimension | Score delta |
|---|---:|
| liquidity | 7.49 |
| debt | 2.38 |
| savings | -9.18 |
| diversification | -6.18 |
| subscriptions | 2.85 |
| security | 3.33 |
| goals | -1.12 |

### Risk Rating

| Removed dimension | Total delta |
|---|---:|
| 财务状况 | 1.33 |
| 投资经验 | -5.71 |
| 风险态度 | 0.0 |
| 投资期限 | 7.06 |
| 流动性需求 | -3.33 |

### Recommendation

| Ablation | Equity delta | Risk-asset delta | Other delta |
|---|---:|---:|---:|
| remove_cvar_penalty | 0 | 0.6 | 0.0424 |
| replace_p05_buffer_with_point_buffer | 0 | 0.6 | -0.3 |
