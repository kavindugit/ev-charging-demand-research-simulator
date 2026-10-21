# Vehicle-class scope and crosswalk review

## Working decision

Retain the survey vehicle classes as separate behavioural groups. Do not substitute the mixed-cohort mean for a motor-car-only profile. Use the matching class row and its complete selected-owner denominator when preparing a conditional scenario. This package does not fill or activate the fleet scenario contract.

| Survey label | Common-cohort owners | Proposed DMT fuel-table group | Status |
|---|---:|---|---|
| Car | 32 | Motor cars | candidate_requires_declared_assumption |
| Motorcycle / scooter | 4 | Motor Cycles | candidate_requires_declared_assumption |
| Three-wheeler | 5 | 3 Wheelers | candidate_requires_declared_assumption |
| SUV / crossover | 18 | Unresolved | registration_class_unresolved |
| Van | 0 | Unresolved | registration_class_unresolved |
| Other | 1 | Unresolved | registration_class_unresolved |

The proposed correspondences are research inferences from category labels, not verified owner-level registration classifications. The proposed assumption IDs are references for future declarations; this package does not declare them accepted or activate them.

## Evidence and scope

The DMT statistics distinguish Motor cars, Dual Purpose Vehicles, Motor Cycles and tricycle groupings. The fuel table separates 3 Wheelers from Motor Tricycle Van. Its parser group names are internal extraction keys, not statutory vehicle-class codes. Published category presence does not by itself verify a survey-to-registration-class assignment.

- https://dmt.gov.lk/images/2026/Vehicle_Population_2014-2025.pdf
- https://dmt.gov.lk/images/2026/New_Registrations_of_Motor_Vehicles_by_Type_of_Fuel_-_2025.pdf

The local DMT snapshot and fleet-contract manifests are hash-bound inputs. DMT numerical cells and their existing review flags are not modified. No DMT vehicle count is used in a demand calculation here.

## Rules before fleet use

- Confirm class membership or explicitly declare a proxy mapping assumption, its scope and sensitivity treatment.
- Keep SUV/crossover, Van and Other separate until supported mapping evidence is available. A Van label is not a Motor Tricycle Van code.
- Confirm the powertrain scope separately. Survey class labels and DMT Electric, Pet/Ele and Die/Ele columns do not independently establish a verified BEV/PHEV behavioural cohort.
- Supply an explicit stock/count basis, year/scenario counts and district allocation evidence or declared assumptions. Annual new registrations are not an established active electric fleet stock.
- Retain calibration, verification, battery, vehicle-limit and connector limitations. Class decomposition does not resolve them.
- Keep behavioural contexts T1-T4 distinct from geographic place groups P1-P4, and owner residence distinct from charging destination.

## Interpretation

The six-case tables retain their original replicate counts. They are not pooled as additional independent replicates or expanded owner samples. Per-study-vehicle values remain conditional on the synthetic cohort and source model. Shared replicate profiles are linked for future covariance-aware aggregation; class MCSEs must not be added. Classes without selected owners receive no invented demand profile.

No new simulations, random draws, fitting, imputation, owner exclusions, fleet counts or geographic demand scaling are performed. The separate open-upper extension cohort is not merged into this common-cohort package.
