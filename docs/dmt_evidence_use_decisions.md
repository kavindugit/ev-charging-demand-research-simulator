# DMT evidence-use decisions

Decision date: 2026-10-21 (Asia/Colombo)
Status: development evidence-use decision; numerical source flags retained.

## Purpose and provenance

Record the permitted use of the extracted public DMT tables before
implementing fleet and adoption scenarios. This is a research modelling
decision, not approval or correction of the source publications.

Source package: data/processed/reference/dmt_sources_20261020T190921_228364Z
Extracted tables: data/processed/reference/dmt_tables_20261020T192940_825879Z
Table-report SHA256: be3faaffbf12ac7ed27ff0c9ada207cfec1ad933abbad99d26c2fe42d24ff783
Verified source/output hash entries: 18

The original PDF snapshots, extracted cells, raw cell grids and both
non-numeric value states remain unchanged.

## Selected published counts

| Reference | Count | Permitted interpretation |
|---|---:|---|
| 2025 total vehicle population | 8,816,613 | National all-fuel registry population reference |
| 2025 motor-car population | 973,376 | National all-fuel motor-car population reference |
| 2025 total new registrations | 362,100 | Registration flow during 2025 |
| 2025 motor-car new registrations | 68,047 | Motor-car registration flow during 2025 |
| 2025 Electric-category motor-car new registrations | 7,239 | Reported Electric-category motor-car flow |
| 2025 Petrol/Electric motor-car new registrations | 18,207 | Reported mixed-fuel motor-car flow; plug-in capability unresolved |
| 2025 Electric-category motor-cycle new registrations | 34,056 | Reported Electric-category motor-cycle flow |

These classes and measures must remain separate. In particular, the
Electric-category motor-car registration flow is not the total existing
national electric-car fleet. The all-fuel population is not an EV count.

## Retained numerical review flags

- fuel_column_sum / buses/P: left numeric subtotal None; right numeric subtotal 2; status review.
- fuel_class_sum / TOTAL: left numeric subtotal 362100; right numeric subtotal 362098; status review.

None above means that no numeric subtotal is available; it does not mean
zero. The annual petrol-bus cell remains blank. Its observed monthly
numeric subtotal is 2. The printed annual grand total remains 362,100;
the annual numeric fuel-cell subtotal remains 362,098. These related
checks do not justify filling a blank or changing the published total.

All printed dashes and blank cells retain their original states. An
observed numeric subtotal can match a printed total without establishing
what every dash or blank means.

## Permitted and deferred uses

- Use the source-specific registration counts to describe observed
  registration flows and review class and fuel definitions.
- Retain the all-fuel population counts as public reference denominators.
  Any use for future scenarios must state vehicle scope and count basis.
- Keep Electric, Petrol/Electric and Diesel/Electric categories separate.
  Do not infer BEV/PHEV/HEV membership or plug-in charging participation
  solely from an unresolved administrative fuel label.
- Retain the cars-under-1000-cc-and-electric row as an overlapping
  breakout. It is neither electric-only nor an additional vehicle class.
- Do not infer national make/model shares, battery capacities, vehicle
  ages, battery health, retirement rates or active-use status from these
  tables. First registration year alone does not establish battery age.
- Review the correspondence between survey vehicle classes and DMT
  classes before scaling any behavioural profile.
- Do not use the 60-owner completeness subset or its residence shares as
  national fleet composition or district EV ownership estimates.
- OSM features and registered rooms are spatial indicators. They do not
  independently establish district vehicle ownership or EV stock.

## Fleet-scenario implementation decision

The inspected tables have not established an active electric-fleet
baseline by district. This is a limitation of the current evidence
package, not a claim that no additional official records exist.

Future fleet inputs must identify their count basis: active stock,
registered population, annual registration cohort, or an explicitly
conditional scenario. Annual registrations cannot silently replace
stock, and a historical registration cohort cannot silently become the
whole fleet. Stock reconstruction requires an initial stock and explicit
entries/exits, with any missing coverage or survival assumptions stated.

A development alternative is a conditional penetration scenario anchored
to a public all-fuel vehicle-class population reference. Its EV shares,
future total-population changes and district allocations are model
assumptions requiring documented rationale and sensitivity checks. Such
counts must be labelled as conditional scenario outputs, not observed
district counts or empirically validated adoption forecasts. No numerical
shares, growth rates or district allocations are activated here.

The next input contract must record vehicle scope, reference date,
count basis, source and hashes, scenario assumptions and their rationale,
district allocation basis, and unresolved evidence. Residence allocation
and charging-location allocation must be distinguished. Behavioural input
origin remains SYNTHETIC; future calculated demand output origin is
SIMULATED. Public source counts retain PUBLIC_REFERENCE provenance.

## Current status

- DMT source acquisition and table extraction: completed.
- Numerical review flags: retained, not corrected.
- Fuel-category definitions and behavioural class compatibility: pending.
- National active electric stock and district electric stock: not established.
- Fleet scenarios and district EV allocation: not activated.
- New demand simulations, empirical validation and model training: none.

This closes the DMT table-preparation step. The next implementation stage
is the fleet-scenario input contract and resolution of its required inputs.
