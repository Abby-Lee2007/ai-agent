# Financial Data Quality Report

- As of: 2026-10-28
- Metric dictionary version: 2026-10-28
- Overall score: 99.0/100
- Status: good
- Blocks personalization: False

## Domain Quality

| Domain | Records | Missing rate | Anomaly rate | Updated at | Sample coverage | Status |
|---|---:|---:|---:|---|---:|---|
| transactions | 321 | 0.0% | 0.0% | 2026-10-28T21:03:01 | 81.1% | good |
| cards | 4 | 0.0% | 0.0% | 2026-10-28T21:03:01 | 100.0% | good |
| subscriptions | 6 | 0.0% | 0.0% | 2026-10-28T21:03:01 | 100.0% | good |
| products | 26 | 0.0% | 0.0% | 2026-10-28T21:03:01 | 88.5% | good |
| holdings | 3 | 0.0% | 0.0% | 2026-10-28T21:03:01 | 100.0% | good |

## Warnings

- None

## Metric Dictionary

| Metric | Definition | Unit | Source | Direction | Threshold |
|---|---|---|---|---|---:|
| transactions.sample_coverage | Distinct transaction days divided by the 90-day analysis window. | ratio | transactions | higher_is_better | 0.6 |
| transactions.missing_rate | Share of required transaction fields that are blank. | ratio | transactions | lower_is_better | 0.02 |
| transactions.anomaly_rate | Share of invalid, duplicate, or zero-value transactions. | ratio | transactions | lower_is_better | 0.03 |
| cards.missing_rate | Share of required card fields that are blank. | ratio | cards | lower_is_better | 0.02 |
| subscriptions.missing_rate | Share of required subscription fields that are blank or invalid. | ratio | subscriptions | lower_is_better | 0.05 |
| products.expected_return_coverage | Share of products with a positive scenario return or live market symbol. | ratio | products | higher_is_better | 0.9 |
| products.anomaly_rate | Share of products with invalid risk levels or missing source metadata. | ratio | products | lower_is_better | 0.02 |
| holdings.missing_rate | Share of holdings with missing product references. | ratio | holdings | lower_is_better | 0.02 |
