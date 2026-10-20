# Synthetic behavioural simulator pilot close-out

Decision date: 2026-10-20

Status: Pilot comparison block complete; district production readiness pending.

## Scope and development decisions

Retain the common cohort of 60 synthetic owners for comparisons. All 170
source owner records remain in the audit. Original readiness remains 60 ready
and 110 deferred; component support for six additional open-upper-energy
owners does not change that readiness or the current comparison cohort.
Keep that six-owner extension separate and unpromoted to the common baseline.

Retain bounded_uniform_test with reduced_test as the development reference.
Retain conditional_gamma_test, conditional_lognormal_test and
rated_ceiling_test as sensitivity cases. This is a reference convention,
not empirical model selection or a claim that uniform energy is most accurate.

Retain both generators: poisson_control demonstrates proposal/overlap effects;
availability_aware is a candidate generator with internal idle calibration.
Do not promote pooled frequency agreement into owner/context validation.

Settings for these six pilots: 28 study days, 7 warm-up days, 10 replicates,
base seed 20261007, 4096 calibration events and 4096 independent verification
events per selected owner vector. Preserve existing code, parameter banks,
seeds and archived outputs; future changes receive new experiment records.

## Data and sampling meaning

behaviour_input_origin=SYNTHETIC; output_origin=SIMULATED.

Results are calculated by the pipeline from synthetic behavioural inputs.
Public-power ratings, weights and derating factors remain engineering tests.

Each replicate includes every common-cohort owner once. Select uniformly from
the retained draw indices within each owner; do not flatten retained rows into
a pool that gives owners with larger banks more weight. Shared indices preserve
linked frequency, home power and energy-case draws.

The finite parameter bank is conditioned on common time/window screening
across energy and power cases. Necessary screens do not establish joint
schedulability or completed-frequency agreement. The completeness-selected
cohort and its vehicle/context composition are not national population shares.

## Reported daily-energy precision

Availability-aware results; figures below are rounded from the saved exports.

| Energy case | Power case | Mean kWh/day | MCSE kWh/day | Relative MCSE | Completions / target |
|---|---|---:|---:|---:|---:|
| Uniform | Reduced | 1026.534 | 8.739 | 0.851% | 100.082% |
| Gamma | Reduced | 1002.184 | 9.008 | 0.899% | 100.035% |
| Lognormal | Reduced | 995.434 | 9.004 | 0.904% | 100.007% |
| Uniform | Rated | 1026.235 | 8.230 | 0.802% | 99.988% |
| Gamma | Rated | 1002.490 | 8.857 | 0.883% | 100.016% |
| Lognormal | Rated | 996.091 | 8.888 | 0.892% | 100.026% |

Poisson-control total completions/target were 77.788-78.232%, with total daily
relative MCSE of 1.319-1.407% across these cases.

MCSE describes conditional simulation sampling error for the saved experiment
design. It does not include uncertainty from fitting distributions to a new
respondent sample, missing evidence, fleet assumptions or model structure.
No precision acceptance threshold was specified before these pilots.
These numbers do not establish convergence or a production replicate count.

## Paired sensitivity findings

Availability-aware contrasts; positive differences mean comparison minus reference.

| Power | Contrast | Daily difference kWh/day | Paired MCSE kWh/day |
|---|---|---:|---:|
| Reduced | Gamma - uniform | -24.349899 | 1.057316 |
| Reduced | Lognormal - uniform | -31.100069 | 1.076448 |
| Reduced | Lognormal - gamma | -6.750170 | 0.141484 |
| Rated | Gamma - uniform | -23.745180 | 0.928857 |
| Rated | Lognormal - uniform | -30.144274 | 0.876604 |
| Rated | Lognormal - gamma | -6.399094 | 0.249211 |

Energy-case effects are resolved relative to their paired sampling errors in
this conditional design. They do not identify the empirically correct family.

Availability-aware rated-minus-reduced daily-energy differences were:

- Uniform: -0.298367 kWh/day; paired MCSE 0.752322.
- Gamma: +0.306352 kWh/day; paired MCSE 0.950606.
- Lognormal: +0.657429 kWh/day; paired MCSE 1.125453.

Those power contrasts do not clearly resolve a daily-energy effect at this
pilot size. They do not establish equivalence or make power irrelevant to
duration or profile shape. Each availability case recalibrates its idle
parameter, so comparisons include that policy response.

MCSE is computed across whole-cohort paired replicate differences. Owners,
hours, calibration events and reused conditional repeats are not independent
additional population replicates. Availability serial marks are paired where
shared; calendar starts and session counts may change across cases.

## Remaining flags and their treatment

Counts below apply separately to each listed energy/power case. A selection is
an owner/replicate vector, not a new respondent. Affected-owner sets overlap
across cases and must not be added to obtain a unique-owner total.

| Energy | Power | Selections | Calibration review | Verification outside 5% | Owners with either flag |
|---|---|---:|---:|---:|---:|
| Uniform | Reduced | 600 | 8 | 9 | 7 |
| Gamma | Reduced | 600 | 6 | 8 | 6 |
| Lognormal | Reduced | 600 | 6 | 8 | 6 |
| Uniform | Rated | 600 | 8 | 9 | 7 |
| Gamma | Rated | 600 | 6 | 8 | 6 |
| Lognormal | Rated | 600 | 6 | 8 | 6 |

The 5% verification band is a development diagnostic, not a confidence interval
or an empirical accuracy threshold. Keep all flagged selections in results.
Do not remove owners, redraw parameters or relax windows based on these outcomes.

T4 completed/target ratios were approximately 90.6-91.4% in these ten-replicate
pilots despite pooled totals near 100%. Owner/context frequency agreement and
initialization stationarity remain unresolved. The earlier frozen verification
of the 512-event pilot did not reproduce its initial T4 deficit, but that
verification froze the earlier idle values; it does not independently verify
the new 4096-event calibrations. Earlier engine warm-up and precision findings
also do not automatically transfer to a changed generator.

## Current physical and population limits

The E/P engine uses a constant-power delivered-energy load proxy. Resolve
battery-delivered versus charger/grid-meter energy before production scaling;
apply efficiency only when converting a defined electrical boundary.

Vehicle acceptance limits, battery/SOC compatibility, dwell availability,
charging curves and station queues are not established by no-overlap checks.
Owner energy is fixed within a selected owner/replicate and reused across
contexts under the current test assumption.

No independent empirical validation, representative national population model,
district charging allocation or national demand forecast is established.
No N-BEATSx model is trained or selected by this close-out.

## Close-out and next implementation stage

Close this six-case pilot comparison block and preserve it as development
evidence. Continue verified geographic/public-input preparation next, beginning
with district boundary source, version, licence, coverage and district-code
matching. The existing 25-district registry and source inventory are the base.

Keep owner residence, charging context, charging district and place typology
separate. Preserve unallocated activity; do not infer observed away-charging
locations from residence or treat missing public features as zero.

Before district production runs, specify supported population construction,
electrical boundary and spatial allocation; define owner/context frequency,
precision and initialization verification criteria for the chosen generator;
then run the necessary targeted checks. Population coverage, fleet scenarios
and external data remain explicit inputs rather than consequences of this pilot.

## Evidence references

- Population model: data/processed/synthetic/behaviour_population_model_20261018T100655_764852Z
- Parameter bank: data/processed/synthetic/window_screened_parameters_20261018T143723_795339Z
- Uniform power pair: data/processed/synthetic/availability_power_comparison_20261019T091220_208860Z
- Gamma power pair: data/processed/synthetic/availability_power_comparison_20261020T045329_631364Z
- Lognormal power pair: data/processed/synthetic/availability_power_comparison_20261020T070714_704756Z
- Energy comparison: data/processed/synthetic/availability_energy_comparison_20261020T082323_361584Z
- Earlier frozen verification: data/simulated/synthetic/availability_verification_20261019T061036_870196Z

The comparison report records source pilot folders, source/code hashes,
settings and export hashes. Six pilot folders represent three energy cases
times two power cases, each with ten paired control/availability replicates.
They are not district configurations or independent national populations.

Comparison report SHA256: ec8353031c430a6c9cf2dd92592760db7d1aed4c7afe8e0c6d9cf15da33cb1c9
