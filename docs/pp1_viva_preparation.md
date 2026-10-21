# Component 2: PP1 explanation and practice guide

Researcher: HMKM Herath, IT23249684. Supervisor: Prof. Samantha Rajapaksha.

Viva: 23 October 2026. Preparation day: 22 October 2026.

Use the generated PP1_RESULTS.md and JSON report for the final pilot numbers.
The statements below distinguish completed prototype work from remaining research.

## A three-minute opening explanation

My component investigates how EV charging creates a 24-hour electricity-demand
profile and how those profiles can be examined across districts, charging contexts
and future fleet scenarios in Sri Lanka.

I have developed and exercised a survey-to-simulation prototype using synthetic
behaviour responses while preparing for real data collection. It checks the raw
responses, preserves their source links, encodes categorical answers as numerical
constraints and separates usable evidence from unresolved answers.

Instead of turning every answer range into a single midpoint, I prepared candidate
probability models and owner-conditioned parameter draws. I then linked charging
frequency, usual energy, charging time windows and charger-power assumptions at
the owner level. I retained incomplete records in audit tables rather than silently
filling or removing them.

The simulation constructs charging sessions and aggregates their contribution
to each hour. An early control generator lost completed charges when proposed
sessions overlapped for the same owner. I investigated that discrepancy using
time-budget checks, start-window checks and paired scheduling experiments. An
availability-aware generator was then tested alongside the control. Its aggregate
frequency results improved in the development pilots, although owner-level flags,
stationarity and empirical validation remain open.

I also prepared public administrative, geographic, tourism and vehicle-registration
references with traceable sources. Conditional Car fleet scenarios and district
scaling outputs demonstrate how these parts connect. Those scenario counts are
research assumptions rather than established future fleet forecasts.

The final PP1 pilot examines where the saved Car charging demand could be allocated
under explicit geographic assumptions. Demand with no identified outside destination
remains visible as unresolved. Hourly energy-accounting checks ensure allocation
does not create demand.

The current contribution is a working, auditable research prototype and a set of
conditional experiments. Real responses will be checked, parameters refitted,
cohort readiness reassessed and simulations rerun. Validation and the proposed
N-BEATSx evaluation follow those steps.

## 1. Explain the data pipeline

1. Raw input inspection checks files, sheets, expected questions and data origin.
2. Survey preparation separates owners, four owner-context records and recent
   session reports. The synthetic source retained 170 owners and 680 contexts.
3. Feature encoding translates answers into ranges, bounds, categories and
   uncertainty states while retaining the raw answers.
4. Population evidence preserves interval, open-ended and unresolved information.
5. Parameter candidates and owner-level models describe the information available.
6. Component assembly links energy, frequency, timing and power by owner ID.
7. Physical/scheduling diagnostics flag incompatible parameter combinations.
8. Simulation generates sessions, then integrates their power into hourly profiles.
9. Comparisons, precision diagnostics and geography/fleet transfers generate
   derived outputs with explicit scope and provenance.

Synthetic interview preparation and candidate extraction were also developed.
Candidate passages or automated codes are not automatically reviewed evidence.
Do not say all transcript facts have been validated or numerically parameterized.

## 2. Know these quantities

| Quantity | Meaning in the current work |
|---|---|
| 170 | Retained synthetic survey owners |
| 680 | Four context records per owner |
| 60 | Common owners with complete components across all three energy cases |
| 6 | Separate open-upper-energy extension owners; not added to the common-case comparisons |
| 32 | Car owners within the common 60-owner cohort |
| 100 per power case | Older reference-session batch replicate count |
| 10 per current case | Current availability-pilot saved replicate count |
| Six energy/power cases | Three energy assumptions times two power assumptions |
| Two generators | Paired Poisson control and availability-aware processes |
| 750 | 25 districts times ten years times three adoption scenarios |
| 3,000 | Planned 750 configurations times four geographic place-profile groups |

The six current cases contain 60 paired replicate comparisons. If counting both
generators separately, that is 120 generator-specific trajectories in those six
batches. Cases share owners and random streams: do not pool them as 120 independent
replicates. Earlier development and verification runs are separate experiments;
do not invent a grand total without a run inventory.

Parameter test draws, accepted sessions, derived profiles and simulation replicates
are different counting units. Scaling to 7,239 reference registration-cohort vehicles
did not independently simulate 7,239 different vehicle histories. The derived
district-profile counts are not independently simulated Sri Lankas.

## 3. Explain the statistical choices

**Why preserve answer ranges?** An answer such as 20–40 kWh does not identify the
exact energy. Range-aware methods preserve that information instead of inventing
a precise value.

**How does interval likelihood work?** The candidate model contributes the
probability of the reported interval. An open-upper answer contributes the
probability above its lower bound. Unknown responses do not contribute an
invented numerical observation.

**Why Gamma and lognormal?** They are positive-valued candidates for usual energy.
The synthetic fit used 144 informative owners. Gamma had lower AIC among those
candidates, but that does not establish the true distribution. The simulator
compares alternative energy assumptions rather than treating AIC as empirical
validation.

**Why compare Poisson and negative binomial?** They are count-model candidates.
The synthetic negative-binomial fit approached its Poisson limit, so it was not
treated as a separately superior fitted model. Weekly reported counts and an
event-time generator are also different modelling decisions.

**What is the joint frequency model?** It links the total weekly rate to the four
context rates while respecting reported bounds. The context rates must sum to
the total. Open-ended or inconsistent evidence stays identifiable.

**What is a seed?** It initializes a pseudorandom stream so a run can be reproduced.
Different seeds create process realizations under the same model. They are not
new survey respondents or independently observed countries.

**What is a paired comparison?** Alternative cases reuse matched owners and
appropriate random inputs. The difference is computed within each replicate;
the MCSE of those differences is used. Common seeds alone are insufficient:
the saved pairing was explicitly checked.

## 4. Explain charging physics and scheduling

Energy is measured in kWh and power in kW. For a constant average-power proxy,
duration is session energy divided by effective average power.

If a 7 kW session is active for 30 minutes within an hour, it contributes 3.5 kWh
to that hour, equivalent to a 3.5 kW hourly average. A session crossing midnight
contributes to both dates. Sessions crossing the study boundary are split into
before-study, in-study and after-study energy.

Summing the 24 hourly mean powers times one hour reproduces average daily energy.
This is an accounting check, not proof of realistic behaviour.

The overlap rejection in the control means the same owner was already charging.
It does not measure public-station queues or insufficient charger capacity.
The generator must distinguish proposed starts from accepted starts and completed
charges. A survey reporting completed charges cannot automatically be used as the
rate of proposals that will subsequently be rejected.

The availability-aware process schedules a subsequent charge after the owner
becomes available and applies start-window logic. Its calibrated idle time was
checked using independent random streams. Aggregate agreement near the target
does not prove agreement for every owner/context.

Weekly time below 168 hours is a necessary screening idea, not sufficient proof
that a schedule fits the reported windows. Window-capacity bounds also do not
establish a feasible joint schedule by themselves.

No-overlap, source and energy checks passed in the saved pilots. Full SOC/travel
energy balance, charging-curve realism, connector compatibility, warm-up
stationarity and empirical validity have not all been established.

## 5. Explain what the results mean

In the saved uniform/reduced pilot, the 60-owner Poisson control produced about
770.5 kWh/day and the availability-aware process about 1,026.5 kWh/day. This was
a generator-policy sensitivity result on synthetic behaviour inputs. It is not
Sri Lanka's daily EV energy consumption.

For that pilot, the saved Car decomposition contains 32 owners and approximately
643.5 kWh/day under the availability-aware process. Use the generated pilot report
for exact values, units and MCSE. Other vehicle classes explain the difference
between the 32-Car total and the 60-owner total.

Changing charging power changes duration and temporal coincidence. If the same
charging energy is completed, higher power does not automatically increase daily
energy. Small positive or negative paired energy differences can arise from
scheduling and study-boundary effects.

The peak of the ensemble mean profile is not the mean of replicate peaks or a
worst-case peak. A pointwise 95th percentile is not a simultaneous worst-case
daily trajectory.

MCSE describes how precisely simulation averages are estimated. It generally
decreases with more independent replicates under fixed assumptions. It does not
measure real-world forecasting accuracy. Current owner parameter-bank selections
and process randomness also do not fully propagate uncertainty in fitted Gamma
or lognormal parameters or in the sampled owner population.

## 6. Explain geography and fleet scaling

Public inputs and behavioural inputs have different origins. The survey/interview
examples are synthetic. DCS, DMT, SLTDA and OSM snapshots are public references.
Future adoption levels, district allocation rules and routing fractions are
additional research assumptions. Keep all three categories visible.

The district registry contains 25 districts and nine provinces. Resolving
Monaragala to Moneragala uses a declared spelling alias. It is name normalization,
not numerical imputation.

OSM mapped land area is not complete land-use coverage. Road length is not traffic
volume. Mapped tourism objects are not necessarily unique venues, visitor counts
or EV arrivals. SLTDA rooms and sampled occupancy statistics need their original
definitions; they cannot automatically be converted into charging demand.

DMT annual Electric registrations are a flow of new registrations, not the active
electric fleet. All-fuel car population is not EV population. The draft future
Car counts and population/equal-district allocations are conditional experiments.

Residence-attributed profiles locate demand according to assumed owner residence.
Charging-destination profiles locate it according to charging assignments. An
owner can charge away from home inside the same district. In the 32-Car cohort,
three report no away charging, 16 mostly within, ten both and three mostly outside.
The 13 named outside-district reports do not establish an outside-session share.

In the new pilot, the 25 located districts plus the unresolved outside category
must recover the original 32-Car hourly total. The unresolved category is not a
26th district. This conditional cohort experiment is not future national routing.

## 7. Explain real-data transition and remaining research

The pipeline structure can be reused when real responses arrive. Repeat source
checks, feature encoding and evidence review. Refit candidate distributions,
review interview evidence, reassess joint constraints and cohort coverage, rerun
the simulations and compare against available charging records. New responses
can change fitted parameters, eligibility and conclusions.

The proposed N-BEATSx stage has not been trained. A surrogate trained on simulator
outputs learns the simulator's relationships. It does not independently validate
the real world. Future evaluation needs suitable baselines and leakage-safe splits
that keep related scenarios and simulation realizations appropriately separated.

Current scaling can change magnitude while preserving a case's normalized shape.
To investigate genuine future shape changes, the model needs justified changes
in behaviour, charging access, power, timing or fleet composition. Relabelling
scaled copies by year does not supply that evidence.

## Practice questions

Answer each aloud in 30–60 seconds, then demonstrate one supporting output.

1. What is your individual contribution and who will use it?
2. Which data are synthetic, public references or researcher assumptions?
3. Why use Monte Carlo instead of one deterministic daily curve?
4. What does a replicate represent? How many are in the displayed experiment?
5. Why are 3,000 planned profile groups different from Monte Carlo replicates?
6. Why did only 60 of 170 owners enter the common simulation cohort?
7. Could that complete-case subset introduce selection bias?
8. Why preserve intervals and open-upper answers?
9. What does lower AIC establish, and what does it not establish?
10. How did you link total and context charging frequencies?
11. Why did the initial control lose completed charging events?
12. How did you test the availability-aware alternative independently?
13. What checks prove energy conservation at midnight and study boundaries?
14. Why does passing every software audit not establish empirical validity?
15. What is MCSE, and how is it different from predictive uncertainty?
16. Why can higher charging power leave daily energy approximately unchanged?
17. Why can an owner charge away from home but inside the home district?
18. How were outside fractions selected? Show their policy file.
19. What happens when an outside destination is unknown?
20. Why do the survey T contexts need a separate geographic-place crosswalk?
21. Why are DMT new Electric registrations not an active fleet estimate?
22. Which current model limitations materially affect interpretation?
23. What changes when real data arrive?
24. Why is N-BEATSx still pending, and what would its evaluation establish?
25. What can the prototype demonstrate today, and what is the next research task?

## Preparation schedule for 22 October

First, study the pipeline and quantities until you can redraw the process from
memory. Next, practise the statistical and scheduling explanations with one
concrete example each. Then inspect the final results, source flags and policy
file. Finish with the opening explanation, a timed demonstration and the questions
above. Keep a local copy of the source data, reports, figures and code for the viva.

When unsure of a number, open its saved report and state the experiment's scope.
Describe implemented decisions and their evidence directly. Do not claim completed
real-data collection, national accuracy, full convergence or trained deep learning.
