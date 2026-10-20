# Component 2 research pipeline specification

Version: 1.0 | Date: 2026-10-17
Researcher: HMKM Herath, IT23249684, SLIIT
Status: Implementation specification; production model decisions remain gated below.

## 1. Purpose and scope

Implement the research workflow using synthetic survey and interview records
until real consented respondent records are available. Every demand curve,
metric and comparison must be calculated by the research pipeline.

Retain 25 Sri Lankan districts, projection years 2026-2035, four place
typologies and Low/Medium/High adoption scenarios. Fleet scenarios are
versioned inputs owned by this component; Component 1 is not a dependency.
N-BEATSx remains a later research stage. UI work follows computed outputs.

Existing plan versions use different ML wording: surrogate and forecasting.
This specification preserves the later forecasting objective. Its time axis,
training units and evaluation design must be specified before training.
Evaluation against simulated targets measures performance against the model;
independent real-world forecast accuracy is a separate evidence requirement.

## 2. Provenance and real-data replacement

| Layer | During synthetic development | After real data collection |
|---|---|---|
| Survey/interviews | Synthetic respondent records | Consented observed records |
| Geography/public statistics | Genuine source snapshots where available | Updated genuine snapshots |
| Behaviour parameters | Derived from synthetic answers or declared assumptions | Re-estimated and reviewed using actual answers |
| Future fleet/behaviour | Declared scenarios | Declared scenarios informed by available evidence |
| Profiles/metrics | Computed simulation outputs | Recomputed simulation outputs |

Future result schemas must distinguish output_origin=SIMULATED,
behaviour_input_origin=SYNTHETIC or OBSERVED, and source/assumption IDs.
Do not rename existing SYNTHETIC fields or change archived files to REAL.
Unknown public inputs remain missing; placeholders require an explicit test
scenario and must not be presented as official statistics.

## 3. Existing development reference

Implemented: survey/interview preparation, feature encoding, rule scaffolding,
bounded owner parameters, power assumptions, session simulation, audits,
paired comparisons, precision diagnostics, warm-up and duration sensitivities.

The reported reference uses 60 ready owners out of 170 retained owners,
28 study days, 7 warm-up days and 100 replicates per power case. The remaining
110 owners remain deferred. This is a completeness-selected test pool.
Parameters are not empirically fitted; district/national demand is not ready.

Reference experiment folders:
- Reduced: data/simulated/synthetic/reference_sessions_20261015T120222_741489Z
- Rated: data/simulated/synthetic/reference_sessions_20261015T120528_035417Z
- Comparison: data/processed/synthetic/public_power_comparison_20261015T120711_366115Z
- Precision: data/processed/synthetic/monte_carlo_precision_20261015T120807_279895Z

Keep the engine and archived experiments as the development reference.
Production extensions receive new configurations, schemas and output folders.

## 4. Charging contexts and place typologies

| Legacy engine code | Charging context | Future explicit context label |
|---|---|---|
| T1 | Workplace / routine non-home | WORKPLACE_ROUTINE |
| T2 | Home | HOME |
| T3 | Destination | DESTINATION |
| T4 | En-route | EN_ROUTE |

Place typologies are separate: P1 Urban Commercial Hub, P2 Suburban
Residential Zone, P3 Tourist Destination, P4 Highway Transit Corridor.
P1-P4 are implementation identifiers for the original four place typologies.
The original plan's T1-T4 place labels must carry an explicit namespace.

A destination context is not automatically tourism, and en-route charging
does not by itself establish a highway location. Keep charging_context,
owner_home_district, charging_district and place_typology as separate fields.
Legacy records are preserved; adapters provide explicit context labels.

The spatial allocation method must record evidence, assumptions and any
conditional weights. A session's allocation fractions sum to one across
assigned locations and an explicit UNALLOCATED category. Never discard
unallocated energy or treat residence as an observed away-charging district.

## 5. Source-to-parameter contract

Use the implemented survey schema, rather than assuming the older draft
questionnaire exactly matches the collected files.

| Quantity | Available evidence / source | Production requirement |
|---|---|---|
| Weekly charging frequency | sessions_per_week_* and context_frequency_* features | Define completed-session versus attempted-start meaning |
| Charging start time | Context window_start_hour, window_end_hour, window_length_hours | Preserve midnight wrapping; choose an evidence-supported within-window model |
| Usual energy | usual_energy_kwh_* features and original answer | Respect finite/open bounds and unknown values |
| Battery and SOC | battery_kwh_*, start_soc_fraction_*, target_soc_fraction_* | Check compatibility; document usable versus nominal capacity |
| Distance since charging | distance_km_* features | Use only with a defined energy route and vehicle-consumption evidence |
| Home access/equipment | home_access_answer, home_equipment_answer, home_power_kw_* | Separate unknown detail from contradictory evidence |
| Charger type | Context charger_code and original answer | Define treatment of mixed/unknown types |
| Non-home power | Existing power configuration plus verified equipment evidence | Ratings, probabilities and derating factors need separate provenance |
| TOU behaviour | tou_meter_answer, intentional_delay_answer, reported timing | Check tariff evidence and avoid shifting already-delayed starts again |
| Residence/away location | home_district_answer, away_geography_answer, outside_district_answer | Identify what location information is actually supported |
| Interview rules | Full transcript passage, code and review decision | Preserve evidence links; no unreviewed numerical probabilities |
| Fleet size/composition | Verified public statistics plus declared projections | Record year, vehicle scope, stock/registration-flow meaning and missing coverage |
| Spatial features | District boundaries, OSM and SLTDA snapshots | Record extraction rules, date, units, coverage and classification method |

Each estimated parameter records parameter_id, conditioning group, source
fields, units, valid sample count, missing/censored count, estimation method,
uncertainty treatment and assumption/evidence IDs. Synthetic estimates remain
labelled synthetic. Interview rules record supporting and conflicting evidence.

## 6. Behavioural population construction

The ready-owner replay and a population parameter model serve different roles.
Do not copy the 60-owner composition into national fleet shares.
Use all valid answers for each parameter with their own denominators;
one unresolved field need not invalidate every other answer from an owner.
Keep original readiness results unchanged while adding this separate analysis.

Handle interval answers, open bounds, variable answers and missing data
explicitly. Do not automatically fit Normal/Beta/LogNormal distributions to
category midpoints. Preserve important relationships between frequency,
energy, battery size, charger type, home access and charging context.
Pool sparse groups or apply assumptions only with recorded justification and
sensitivity analysis. Do not manufacture district-specific behavioural fits.

Virtual-owner IDs are distinct from respondent IDs. Record the generation or
weighting method, supported vehicle classes and population coverage.
Keep battery-electric, plug-in hybrid and non-plug-in vehicle definitions
explicit; unsupported classes remain an identified coverage gap.

## 7. Energy, power and scheduling

Existing E/P durations use a delivered-energy load proxy. Production inputs
must identify whether kWh is battery-delivered or charger/grid-meter energy.
Apply conversion efficiency only when changing the energy boundary.

Select one primary session-energy route: reported charging energy, or a
battery/SOC-derived calculation with evidence-supported capacity. Other
quantities can support consistency checks or declared fallbacks. Do not add
trip-depletion energy to SOC-derived refill energy for the same session.
Vehicle capacity, acceptance limits, degradation and charging losses require
verified sources or explicit assumption status before affecting outputs.

Retain actual session durations and allocate energy over every overlapped
hour, including partial hours, midnight and study-boundary carry-in/out.
An arrival-hour count of charger ratings is not an energy allocation method.
Distinguish start windows, plug-in availability and active charging duration.

Resolve whether survey frequency means completed sessions or proposed starts.
If completed sessions are intended, demonstrate how the generator reproduces
that quantity despite overlap scheduling. Record proposed, accepted, rejected
and delayed counts. Same-day deferral remains a separate sensitivity.

Retain no-overlap, energy-balance and input-integrity checks. Do not add TOU
delay twice. Station queues/failures and urgency effects require their own
evidence and model design before activation.

## 8. Geography, allocation and fleet scenarios

Build a canonical 25-district registry with stable source IDs and alias rules.
Acquire genuine boundary and feature snapshots with source dates and hashes.
Check geometry, projected area/length units, duplicate features and coverage.
Missing OSM features do not prove zero activity. OSM features and tourism
statistics are allocation proxies, not measured charging shares or EV arrivals.

Keep raw features separate from allocation weights. Document normalization,
zero-denominator handling and alternative weighting choices. Do not convert
hotel capacity directly into EV arrivals without the required assumptions.

Fleet scenario rows identify district, year, adoption scenario, vehicle class,
fleet quantity, reference year, source ID, projection method and assumptions.
Distinguish operating fleet stock from new registrations; missing is not zero.
Future fleet quantities are projections, not observed future counts.

Generate owner charging behaviour jointly across contexts. Allocate resulting
charging activity spatially; do not create four independent copies of an EV.
Allocated district/place energy plus unallocated energy must conserve the
source total. Aggregate each replicate before computing national percentiles.
National percentile curves are not sums of district percentile curves.

Changes in yearly profile shape require stated drivers, such as fleet mix or
charging access. Fixed behaviour with changing fleet size provides a baseline;
random perturbations must not be used solely to create an apparent trend.

## 9. Experiment design and output definitions

The main scope has 25 x 10 x 3 = 750 district/year/adoption configurations,
each producing four place-typology profile groups: 3,000 main groups.
If the engine handles all place outputs jointly, R replicates require
750 x R configuration executions. Additional power/policy sensitivities are
separate experiments. Replicates are not additional observed respondents.

The current 28-day, 7-day warm-up, 100-replicate settings are development
choices. Define an annual scenario profile as a representative-period mean
unless an explicit calendar/seasonal sampling design is implemented.
Do not describe simulated future scenarios as measured historical years.

Before production, specify mean-precision targets for total and context/place
outputs and test representative small/large cases. Use MCSE or another justified
precision measure; CV stability alone is not a stopping rule. Benchmark actual
runtime and storage. No production replicate count or runtime is established.

Retain mean hourly power, daily energy, peak of the mean profile, all tied peak
hours, replicate count and hourly/daily MCSE. Record the percentile sampling
unit. Current bands describe replicate study-period mean profiles; they are
not quiet/worst individual days or simultaneous confidence bands.
Keep daily peaks and mean-profile peaks as different quantities.

## 10. Research result tables and checks

The future primary profile table is long-form: one row per run/configuration,
district, year, adoption scenario, power case, place typology and hour 0-23.
Include mean_power_kw, p05_kw, p50_kw, p95_kw, mcse_kw, n_replicates,
study_days, warmup_days, electrical_boundary, percentile_sampling_unit,
output_origin, behaviour_input_origin, parameter_version and source IDs.
Daily summaries store energy, peak value/hours and scheduling counts.
Provide explicit TOTAL and UNALLOCATED diagnostics without counting them
as additional members of the four-typology main grid.

Validation checks include complete case/hour grids, finite nonnegative powers,
consistent units, context/place/district energy balances and origin metadata.
Absent inputs produce explicit incomplete status; they do not produce invented
profiles. A supported zero-fleet case can legitimately produce a zero profile.

Save run settings, source hashes, code version, seeds and replicate summaries.
Retain existing CSVs. Define production storage before large runs; selected
session traces and complete aggregate evidence must remain reproducible.
Matching numeric seeds alone do not establish a paired experiment.

## 11. Sensitivity, forecasting and validation

Separate Monte Carlo sampling error, behavioural-input uncertainty, fleet
scenarios and structural assumptions. Sensitivities cover frequency meaning,
energy, timing, charging power, TOU, spatial weights and year-varying drivers.
Use paired comparisons only where input draws or shared histories are verified.

Before N-BEATSx training, define the time step, target, input window, forecast
horizon, available covariates and train/validation/test grouping. Keep the
2026-2031 training and 2032-2035 test intention subject to that design review.
Six annual observations and 24 hours within a day are different time axes.
Noise/jitter does not create independent years; grouped records must not leak
between splits. Choose hyperparameters without tuning on the final test set.

Compare against constant per-EV shape with fleet scaling and appropriate
training-only statistical baselines. A 2035 target cannot define a baseline
that claims to forecast from 2031. Decomposition does not by itself establish
causal effects or post-2035 accuracy. Model training is not the next task.

Report implementation verification, mean precision, external plausibility and
independent empirical validation separately. General feeder load, accommodation
and traffic proxies do not directly validate EV-only load magnitude. Where
independent charging observations are unavailable, empirical validation stays
pending. Reserve suitable independent observations before fitting when real
data becomes available; refit, rerun and reevaluate the pipeline afterwards.

## 12. Implementation order and completion conditions

1. This specification: record the scope, definitions and production gates.
2. District registry and source inventory: canonical districts, source coverage,
   extraction requirements and explicit missing inputs; no guessed fleet counts.
3. Behaviour parameter layer: survey mappings, reviewed interview rules,
   missing-data treatment and population-generation specification.
4. External-data processing: verified geometry, OSM/SLTDA features and fleet
   inputs; documented charging-context to place allocation.
5. Population/scenario engine: coherent virtual owners or justified weights,
   energy/power rules, completed-frequency checks and spatial conservation.
6. Pilot experiments: model checks, precision criteria and performance evidence.
7. Full simulation grid and controlled sensitivity experiments.
8. Forecast dataset/design review, baselines and N-BEATSx experiments.
9. Evidence/validation reports and result exports; Next.js integration later.

District production runs require reviewed population, spatial and energy
specifications, complete or explicitly coverage-limited inputs and passing
pilot checks. Creating this document does not satisfy those production gates.
Claim the completed stages demonstrated by evidence, and identify pending
field data, fitting, model training or validation accurately at PP1.
