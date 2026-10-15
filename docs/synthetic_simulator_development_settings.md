# Synthetic simulator development settings

Decision date: 2026-10-15

## Purpose

Record working settings for continued simulator development.
These settings are not a formal convergence result or an empirically
validated configuration for Sri Lankan EV demand.

## Working settings

| Setting | Development choice |
|---|---|
| Study duration | 28 days |
| Warm-up duration | 7 days |
| Replicates | 100 |
| Base seed | 20261007 |
| Owner population | Current 60 ready synthetic owners |
| Main public-power case | reduced_test |
| Public-power comparison case | rated_ceiling_test |
| Scheduling | Existing chronological owner-overlap rejection |
| Same-day deferral | Separate sensitivity assumption |

These choices apply to future experiments. Existing runs retain their
original settings and provenance.

## Evidence

All reported scheduling, candidate-pairing and energy checks passed.

### Monte Carlo precision

The earlier 100-replicate public-power comparison had total daily-energy
relative MCSE of 0.326% for reduced power and 0.330% for rated power.

MCSE measures simulation sampling uncertainty conditional on the model.
It does not measure uncertainty in survey parameters or model assumptions.

### Warm-up sensitivity

Increasing warm-up from 4 to 7 or 14 days changed mean daily energy by
approximately -0.004178 kWh/day in both tested power cases.

The paired MCSE was approximately 0.019502 kWh/day.
There were six changed study-session acceptance decisions in each case,
all associated with T2. Aggregate results agreed at 7 and 14 days.

Seven days is retained as a conservative development choice.
Stationarity has not been established.

### Study-duration sensitivity

All duration cases used 7 warm-up days and 100 replicates.

| Power case | Days | Mean daily energy, kWh/day | Relative daily MCSE |
|---|---:|---:|---:|
| Reduced | 14 | 762.296767 | 0.427% |
| Reduced | 28 | 767.719608 | 0.302% |
| Reduced | 56 | 766.884520 | 0.216% |
| Rated | 14 | 767.283427 | 0.428% |
| Rated | 28 | 772.698129 | 0.309% |
| Rated | 56 | 771.952722 | 0.220% |

Compared with 28 days, the 56-day mean daily energy changed by:
- Reduced: -0.835088 kWh/day, approximately -0.109%.
- Rated: -0.745407 kWh/day, approximately -0.096%.

The largest absolute ensemble hourly mean-profile differences were
0.672 kW and 0.702 kW respectively.

The ensemble mean-profile peak occurred at hour 21 in all duration cases.

At 28 days, T3 relative daily MCSE was approximately 1.83%, and T4 was
approximately 1.55-1.56%. Context estimates have different precision.
Small total changes can conceal offsetting context changes.

No acceptance thresholds were specified before these experiments.
The selected settings are a practical development decision.

## Experiment references

Precision:
data/processed/synthetic/monte_carlo_precision_20261008T172738_898196Z

Reduced warm-up:
data/simulated/synthetic/warmup_sensitivity_20261015T090213_140055Z

Rated warm-up:
data/simulated/synthetic/warmup_sensitivity_20261015T090638_132962Z

Reduced duration:
data/simulated/synthetic/duration_sensitivity_20261015T100848_730221Z

Rated duration:
data/simulated/synthetic/duration_sensitivity_20261015T102039_667554Z

## Interpretation limits

- All current inputs and results are synthetic.
- The 110 deferred owners remain excluded from simulation.
- Public-power values and weights remain engineering test assumptions.
- Overlap rejection affects realised charging frequency.
- No district or national scaling has been established.
- No empirical calibration or independent validation has been completed.
- These experiments do not establish a production replicate count.
- CV stability alone is not a mean-precision stopping criterion.
- Experiments use different master generation horizons and random streams.
  Matching seed numbers alone do not make separate experiments paired.
  Their results must not be pooled as additional independent replicates.

## Next implementation stage

Review owner readiness, deferred-owner reasons and unresolved parameter
assumptions before expanding the simulated population.

Retain the existing engine and completed experiments as the development
reference while preparing that review.
