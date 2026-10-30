# Credit Risk Dataset Data Dictionary

## Dataset

- Name: Default of Credit Card Clients
- Source: UCI Machine Learning Repository
- Dataset ID: 350
- Records: 30,000
- Features: 23
- Target: Y

## Feature Groups

| Features | Description | Suggested Type |
|---|---|---|
| X1 | Credit limit | Numerical |
| X2 | Gender | Categorical |
| X3 | Education | Categorical |
| X4 | Marital status | Categorical |
| X5 | Age | Numerical |
| X6-X11 | Historical repayment status | Categorical |
| X12-X17 | Monthly bill statement amounts | Numerical |
| X18-X23 | Previous payment amounts | Numerical |

## Special Values

- X3 contains values 0, 5 and 6 that require further investigation.
- X4 contains value 0 that requires further investigation.
- X6-X11 contain values -2 and 0 whose meanings require further investigation.

## Data Quality Findings

- Total records: 30,000
- Missing values: 0
- Duplicate feature rows: 56
- Duplicate feature combinations: 52
- Conflicting duplicate combinations: 21

## Target Distribution

- Y = 0: 23,364 (77.88%)
- Y = 1: 6,636 (22.12%)

## Preprocessing Policy

- Preserve the original raw data.
- Do not delete duplicate records without additional justification.
- Do not delete special category values without additional investigation.
- Investigate alternative preprocessing strategies during model experiments.