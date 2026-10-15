# Synthetic parameter-readiness review decisions

Decision date: 2026-10-15

## Review result

- Owners reviewed: 170.
- Owners ready for the bounded development test: 60.
- Owners deferred: 110.
- Owners with multiple detected issues: 41.
- Checks marked not_evaluable: 143.

The review reproduced the original readiness decisions and ready profiles.
No owner eligibility or parameter values were changed.

Detected issue categories overlap. Their counts must not be added together
as a count of distinct owners.

## Energy gaps

Among the 42 owners with unresolved usual-energy bounds:

| Original answer | Owners | Interpretation |
|---|---:|---|
| It varies a lot | 7 | No bounded usual-energy range |
| I do not know | 19 | Energy unknown |
| 60 kWh or more | 16 | Lower bound available; upper bound unresolved |

Do not replace unknown energy with zero, a midpoint or a fleet average.
Do not treat "60 kWh or more" as exactly 60 kWh.

Battery capacity alone does not determine session energy. Any future
capacity-based bound needs a documented energy boundary and assumptions.

## Frequency gaps

Among the 35 owners with unresolved total-frequency bounds:

| Original answer | Owners | Interpretation |
|---|---:|---|
| It varies a lot | 23 | No bounded weekly rate |
| More than 7 times | 12 | Open upper bound |

Do not interpret variable frequency as zero.
Do not replace "More than 7 times" with exactly seven weekly sessions.
An upper cap would be a new modelling assumption, not a surveyed value.

## Correction to equipment-conflict interpretation

The review exported home_equipment_type_conflict for 11 owners.

Inspection showed:
- Eight answered "I know the equipment but not the power level".
- Three answered "I do not know".
- All eleven had unknown home-power bounds.

These cases are unresolved equipment details, not demonstrated contradictions
between two explicitly identified equipment types.

The review's text-prefix check produced an overly strong conflict label.
Retain the original output for traceability and interpret these eleven cases
as equipment-detail uncertainty.

Their unresolved home power still prevents bounded simulation under the
current policy. No owner becomes ready through this interpretation correction.

Future review code should distinguish unknown equipment details from an
explicit socket-versus-dedicated-AC contradiction.

## Development decision

Continue with the existing 60 ready owners.

Retain all 110 deferred owners in the audit. Do not automatically fill missing
values or release owners into simulation.

Use 28 study days, 7 warm-up days and 100 replicates for future development
experiments, as recorded in synthetic_simulator_development_settings.md.

The selected owners form a biased completeness subset. Their composition
must not be extrapolated to Sri Lankan EV owners.

## Future resolution routes

- Check original answers and encoding before changing any values.
- Use respondent clarification or suitable measured evidence where available.
- Treat any invented bounds or charger mixtures as separate assumption
  scenarios, with their sources and affected owners recorded.
- Recheck all blockers before changing an owner's readiness status.

## Source review

data/processed/synthetic/readiness_review_20261015T114333_031760Z

## Next stage

Prepare a reproducible development reference run using the selected settings
and existing ready-owner pool.

Empirical calibration, population expansion and national scaling remain
separate work.
