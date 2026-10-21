# PP1 charging-destination pilot: assumptions and scope

This is a conditional allocation experiment using existing simulated sessions.
It is a small bridge from residence-attributed demand to charging-location demand.
It is not the final geographic model or a national travel-flow estimate.

## Fixed inputs

- 32 original study owners labelled Car, within the common 60-owner cohort.
- Saved bounded-uniform energy / reduced-power availability pilot.
- Both saved generators: Poisson control and availability-aware.
- Ten saved replicates, 28 study days and seven warm-up days.
- Their original synthetic survey geography answers and resolved district IDs.
- The already exported Car profiles provide an independent reconciliation target.

The pilot does not rerun charging demand, select new parameters, create vehicles,
activate future fleets or train a model. It preserves the saved session timing,
power and study-window energy.

## Survey context definitions

| Code in saved session files | Meaning |
|---|---|
| T1 | Workplace, education, shopping or other routine non-home charging |
| T2 | Home charging |
| T3 | Tourism, leisure, hotel, restaurant or destination charging |
| T4 | Long-trip, highway or en-route charging |

The proposal's four geographic place typologies require a separate crosswalk.
Home charging does not by itself establish a suburban location. An en-route
charge does not establish a motorway location. No geographic place typology is
assigned by this pilot.

## Explicit experimental fractions

Fractions below apply to non-home contexts only. They are chosen study settings,
not fitted estimates. Lower/higher refers to outside charging allocation and has
no connection to the low/medium/high EV adoption scenarios.

| Q20 answer | Residence reference | Lower outside fraction | Higher outside fraction |
|---|---:|---:|---:|
| No charging away from home | 0% | 0% | 0% |
| Mostly within home district | 0% | 10% | 40% |
| Both within and outside | 0% | 25% | 75% |
| Mostly outside home district | 0% | 60% | 90% |

All T2 home demand remains at the owner's residence district. For T1/T3/T4,
the home-district fraction is one minus the outside fraction. The selected
fraction is held constant across those contexts, hours, chargers and session
energies. These are simplifying assumptions for sensitivity analysis.

If the owner named an outside district, the outside fraction goes there. Assigning
that entire fraction to one most-common district is another explicit simplification.
If the owner did not name an outside district, the outside fraction goes to
OUTSIDE_UNRESOLVED. This category retains demand whose geographic destination is
not identified. It is not a district and must be shown separately from the 25
districts.

In the actual Car survey cohort, the 19 Not applicable outside-district answers
include 16 owners reporting mostly-within-district away charging and three reporting
no away charging. Treating all 19 as home-only would be incorrect.

## Fractional allocation, not new session routing

For each saved non-home session, its hourly power contribution is multiplied by
the within/outside fractions. A session is not physically split or independently
resimulated. This is an expected power/energy allocation under a fixed policy.
Fractional rows must not be counted as completed sessions or EVs.

The same original replicate is used for every case. Differences are paired.
For every replicate, generator, context and hour:

    sum(25 located district profiles) + outside unresolved = original Car cohort

The cohort here is 32 study cars, not the national EV fleet. Located district
demand alone can be smaller than the cohort total because unresolved outside
demand is deliberately retained. The allocation does not alter cohort daily
energy or the cohort's mean-profile peak.

## Uncertainty and remaining work

MCSE is calculated across the ten saved replicates. It describes mean precision
conditional on the original cohort, simulator and fixed routing policy. It does
not include population representativeness, routing uncertainty, fitted-distribution
parameter uncertainty or real-world forecast error.

The original calibration, independent-verification and missing battery-comparison
flags remain visible. Passing allocation checks does not validate the demand
generator or establish warm-up stationarity.

After PP1, assess context-specific destinations and spatial proxy choices, resolve
the geographic place crosswalk, evaluate future-fleet transfers and determine
production precision requirements. Repeat fitting and validation when real
behaviour data are collected.
