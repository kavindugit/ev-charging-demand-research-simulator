# PP1 conditional charging-destination pilot

## Scope

32 original synthetic study Car owners; 28 study days; 7 warm-up days; 10 saved replicates.
One energy/power case: bounded_uniform_test / reduced_test. Both saved generators are retained.
This step allocates existing simulated energy. It creates no new demand simulations, owners, parameter draws or trained models.

## Computed energy allocation

| Generator | Allocation case | Cohort kWh/day | Located kWh/day | Outside unresolved kWh/day |
|---|---|---:|---:|---:|
| poisson_control | residence_reference | 486.253 | 486.253 | 0.000 |
| availability_aware | residence_reference | 643.508 | 643.508 | 0.000 |
| poisson_control | lower_outside_fraction_test | 486.253 | 474.903 | 11.350 |
| availability_aware | lower_outside_fraction_test | 643.508 | 629.625 | 13.883 |
| poisson_control | higher_outside_fraction_test | 486.253 | 440.852 | 45.401 |
| availability_aware | higher_outside_fraction_test | 643.508 | 587.978 | 55.530 |

## Interpretation

- These values are chosen experimental settings, not survey estimates, fitted parameters, confidence bounds or adoption scenarios.
- Fractions apply only to non-home charging contexts T1, T3 and T4. T2 remains at the reported residence district.
- The same fraction applies across non-home contexts, hours, chargers and session energies within each answer category. These independence assumptions are unverified.
- A named most-common outside district receives the outside fraction in this simplified pilot. This concentration assumption does not establish that all outside charging occurs there.
- If no outside district is named, outside energy remains in OUTSIDE_UNRESOLVED. Not applicable does not force outside charging to zero.
- Fractions divide expected power and energy. Individual sessions are not randomly routed, and fractional allocation rows are not completed-session counts.
- This pilot retains the original 32 study Car owners and their reported residence districts. It does not create or route a national future fleet.

- Located district demand plus unresolved outside demand reproduces the original cohort at every hour and replicate.
- The unresolved row is an accounting category, not a 26th district. Located districts alone can sum below the cohort total.
- MCSE describes saved-replicate mean precision under a fixed policy. It excludes routing-policy uncertainty and population sampling uncertainty.
- T1/T2/T3/T4 retain survey context meanings. No commercial/residential/tourist/highway geographic-typology crosswalk is activated.

## Original model review flags retained

```json
{
  "owner_parameter_selections": 320,
  "calibration_review_selections": 6,
  "verification_outside_development_band": 6,
  "missing_battery_upper_flags": 60,
  "positive_battery_upper_flags": 0
}
```

Original completed-frequency agreement: False
Original warm-up stationarity established: False

## Stopping point

The destination sensitivity pilot is complete when the checks in the JSON report pass.
Real-data collection, refitting, scheduling validation, geographic-typology mapping, production precision experiments,
future-fleet destination allocation and N-BEATSx evaluation remain later research tasks.

## Recorded sources

- Geography: data/processed/synthetic/charging_geography_resolved_20261021T173245_486997Z
- Saved pilot: data/simulated/synthetic/availability_generator_pilot_20261019T081041_202887Z
- Class export: data/processed/synthetic/vehicle_class_profiles_20261020T210942_468959Z
- Policy: config/synthetic/charging_destination_policy_v1.json
