# Synthetic availability-generator development settings

Decision date: 2026-10-19

## Development decision

Use 4096 calibration events and 4096 independent verification events
per owner/replicate selection for the next availability-generator pilots.

Pass --calibration-events 4096 explicitly. The existing script default
remains unchanged.

This is a practical development choice made after reviewing experiments.
It is not a formal convergence result, production precision criterion,
empirical validation, or approval for national demand scaling.

The availability-aware generator remains a candidate for expanded
energy/power comparisons. Retain the Poisson control and archived
development references.

## Current scope and settings

- Behavioural inputs: synthetic.
- Computed outputs: simulated.
- Cohort: common_energy_cases.
- Owners per replicate: 60.
- Energy case: bounded_uniform_test.
- Power case: reduced_test.
- Study duration: 28 days.
- Warm-up duration: 7 days.
- Pilot replicates: 10.
- Base seed: 20261007.
- Calibration events per selection: 4096.
- Independent verification events per selection: 4096.

The current 60-owner cohort is selected for component completeness.
It does not represent Sri Lankan fleet composition. Original readiness,
the 170 retained owner records, and unresolved-owner evidence remain
separate from these conditional experiments.

## Generator interpretation

Selected weekly rates are treated as model targets for completed charges.

The generator chooses the next context using joint-rate shares.
After the previous charge ends, it adds an exponential idle gap and
samples a start within the remaining part of the next permitted window.

Waiting for the chosen context can occur even when another context
has an earlier window. This changes the marginal timing distribution
from the original uniform-window Poisson proposal model.

Internal calibration adjusts the idle-gap mean toward a finite-prefix
throughput proxy. It does not fit new owner behaviour or establish
a stationary charging process.

Owner energy and home power remain fixed within a selected replicate.
Non-home power is sampled per session from the recorded test law.
Energy allocation retains partial hours and study-boundary carry-in/out.

## Calibration-length evidence

Both pilots have identical input manifests and selected-parameter exports.
The checked Poisson control aggregate counts and daily energy also agree.

| Metric | 512-event configuration | 4096-event configuration |
|---|---:|---:|
| Verification events per selection | 512 | 4096 |
| Selections outside the verification band | 116 | 9 |
| Total completed charges / target | 99.97% | 100.08% |
| T4 completed charges / target | 89.51% | 91.08% |
| Mean daily energy, kWh/day | 1024.602 | 1026.534 |

Both calibration and verification prefix lengths changed.
The flag-count change therefore does not isolate calibration error alone.

Study random streams were reused across the two configurations.
Their study results are dependent and must not be pooled as additional
independent replicates. The two T4 shortfalls are not independent
confirmations of a persistent context bias.

## Independent frozen verification

The frozen verification applies to the original 512-event calibration.

It retained ten saved parameter groups and repeated the charging process
ten times per group. These are 100 conditional study batches, not
100 new independent parameter samples.

T4 completed charges reached 100.75% of target.
Its independent long-prefix rate reached 100.01% of target.
The earlier T4 shortfall did not persist in that check.

These results do not independently verify the newly recalibrated
4096-event idle settings.

## Remaining review cases

| Calibration status in the 4096-event pilot | Selections | Verification flags |
|---|---:|---:|
| proxy_target_matched | 592 | 2 |
| zero_idle_proxy_below_target_review | 8 | 7 |

Zero-idle calibration reviews: 8 selections,
involving 5 distinct owners.

Affected owner IDs: SYN-SUR-004, SYN-SUR-055, SYN-SUR-073, SYN-SUR-166, SYN-SUR-170

Keep these selections and their achieved frequencies visible.
Do not remove owners, redraw parameters, reduce energy, increase power,
or relax windows merely to obtain a target match.

A zero-idle rate deficit is a limitation of the selected parameters
under this timing policy and finite-prefix diagnostic. It is not proof
that the original owner answers are physically impossible.

The 0.2% calibration tolerance and 5% verification band are numerical
development diagnostics, not confidence intervals or empirical
accuracy thresholds. Aggregate agreement can conceal owner-level gaps.

## Remaining limits

- The selected parameter bank is conditioned on the recorded time and
  window screens and their intersection across candidate cases.
- Necessary capacity checks do not establish joint schedulability.
- Start windows do not establish vehicle dwell or plug-in availability.
- The seven-day warm-up has not established stationarity for this generator.
- Calibration and verification sampling error remain distinct from
  uncertainty in behaviour, distribution fits and missing data.
- No production replicate count has been established.
- No district or national scaling or empirical validation is established.

## Next work

Expand matched comparisons across the three energy cases and two public
power cases on the common 60-owner cohort.

Keep the six-owner open-upper extension as a separate comparison cohort.

Check selected-index pairing, context and owner frequency residuals,
energy, hourly profiles, Monte Carlo error, runtime and storage before
increasing experiment size.

Population representation, spatial allocation, fleet scenarios and
production precision criteria remain necessary before district runs.

## Experiment references

512-event pilot:
data/simulated/synthetic/availability_generator_pilot_20261019T051414_753771Z

Frozen verification of the 512-event pilot:
data/simulated/synthetic/availability_verification_20261019T061036_870196Z

4096-event pilot:
data/simulated/synthetic/availability_generator_pilot_20261019T081041_202887Z

Parameter bank:
data/processed/synthetic/window_screened_parameters_20261018T143723_795339Z
