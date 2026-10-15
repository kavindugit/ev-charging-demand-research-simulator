# Synthetic simulator development reference

Recorded: 2026-10-15

## Scope

Audited synthetic development reference for 60 ready owners.
All 170 owners remain represented in the readiness audit; 110 remain deferred.

This milestone completes the bounded synthetic simulator verification phase.
It does not establish empirical validation, national representativeness,
stationarity or a production Monte Carlo stopping rule.

## Settings

| Setting | Value |
|---|---|
| Study duration | 28 days |
| Explicit warm-up | 7 days |
| Replicates per power case | 100 |
| Base seed | 20261007 |
| Scheduling | Chronological owner-overlap rejection |
| Main power case | reduced_test |
| Comparison power case | rated_ceiling_test |

## Reference results

| Metric | Reduced | Rated |
|---|---:|---:|
| Mean daily energy, kWh/day | 761.646673 | 766.757818 |
| Daily-mean MCSE, kWh/day | 2.526935 | 2.556160 |
| Relative daily-mean MCSE | 0.332% | 0.333% |
| Peak of ensemble mean profile, kW | 43.355220 | 43.675737 |
| Ensemble mean-profile peak hour | 21 | 21 |
| Pooled study-start rejection | 22.97% | 22.50% |

All 200 replicate audits passed.
All 100 power pairs matched, covering 131311 candidates including warm-up.

Rated-minus-reduced mean daily energy:
5.111145 kWh/day, or approximately 0.671%.

Paired daily-energy difference MCSE:
0.277698 kWh/day.

These errors describe simulation sampling uncertainty conditional on the
fixed synthetic inputs and model assumptions.

## Reference output folders

Reduced batch:
data/simulated/synthetic/reference_sessions_20261015T120222_741489Z

Rated batch:
data/simulated/synthetic/reference_sessions_20261015T120528_035417Z

Paired comparison:
data/processed/synthetic/public_power_comparison_20261015T120711_366115Z

Precision assessment:
data/processed/synthetic/monte_carlo_precision_20261015T120807_279895Z

Profile figures:
data/processed/synthetic/profile_figures_20261015T120808_424083Z

Precision figures:
data/processed/synthetic/precision_figures_20261015T120905_255790Z

Readiness review:
data/processed/synthetic/readiness_review_20261015T114333_031760Z

## Supporting decisions

- synthetic_simulator_development_settings.md
- synthetic_readiness_review_decisions.md

The readiness decision document corrects the interpretation of eleven
equipment-detail uncertainties that the review labelled as conflicts.
Their owner eligibility remains unchanged.

## Remaining research work

Real-data preparation, empirical parameter estimation and independent
validation remain separate tasks.

Population expansion, spatial scaling and AI surrogate or forecasting
development require their own documented inputs and evaluation designs.

Further sensitivity experiments are optional future work unless a new
failure or modelling concern justifies them.
