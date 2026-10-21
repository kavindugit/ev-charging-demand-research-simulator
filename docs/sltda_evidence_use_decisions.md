# SLTDA evidence-use decisions for simulator development

Decision date: 2026-10-21

## Scope and provenance

Record provisional development treatment of the extracted public tables.
This policy does not resolve the publisher's definitions or establish
independent validation.

Extracted tables:
data/processed/reference/sltda_district_tables_20261020T182044_030104Z

Source snapshots:
data/processed/reference/sltda_sources_20261020T180745_694783Z

Table-report SHA256:
b2548847447e1ae8b9b3cfcdf4e9e506b0d2198eeeada8b1c7ee077b1f1bd1be

The extraction passed its source-hash, district-join and accounting checks.
Original PDFs, published values and earlier reports remain unchanged.

## Capacity reference

Both district room tables contain all 25 districts.

Their district row sums reproduce their published totals:
- 2024: 55,455 registered rooms.
- 2025: 59,578 registered rooms.

Use the 2025 district table as the reference accommodation-capacity
snapshot for development comparisons.

Retain the 2024 district table as a separate historical comparison.
This reference choice does not establish a forecast for 2026-2035.

When district capacity shares are needed, derive new share columns from
room counts and their same-table total. Preserve the printed percentages.

Rooms are distinct from beds, establishments, occupied rooms, visitor
arrivals, vehicles and charging sessions.

## Four retained numerical review findings

| Finding | Published value | Comparison | Treatment |
|---|---:|---:|---|
| Kilinochchi 2024 room share | 0.1% | Approximately 0.194753% from counts | Preserve printed share; derive new shares from counts. |
| Ampara 2024 room share | 1.4% | Approximately 1.498512% from counts | Preserve printed share; derive new shares from counts. |
| 2024 room total in the 2025 category table | 53,378 | 55,455 in the 2024 district table and 2025 province comparison | Retain both statements without substituting or pooling them. |
| Published overall annual occupancy | 44.5 | Approximately 40.541667 as the simple mean of twelve monthly totals | Preserve 44.5 and retain the mean as a diagnostic. |

These comparisons do not prove source errors. Rounding conventions,
scope, revisions or weighting may differ. Their causes remain unresolved.

## Occupancy treatment

The selected 2024 tables cover:
- 22 reporting districts.
- 221 responding graded establishments.
- 264 reported district-month occupancy values.

Mannar, Mullaitivu and Kilinochchi remain missing in those tables.
Their absence does not establish zero occupancy.

Retain missing values. Do not fill them with national or neighbouring
district values.

Retain occupancy on its reported 0-100 scale. The selected tables do not
establish its room/bed denominator or annual weighting method.

Keep these observations for definition and seasonality review.
Do not apply them as occupancy rates for all registered accommodation
categories or treat them as observed 2025 occupancy.

## Update to the original tourist-inflow proposal

Do not activate bed capacity times occupancy divided by 2.5 as daily
vehicle inflow using the current evidence.

The selected tables establish room counts, without a verified conversion
to district bed capacity.

Even compatible bed capacity and bed occupancy would first describe an
occupied-capacity stock. Daily vehicle inflow requires a defined time or
turnover basis and evidence or explicit scenarios for travel mode,
vehicle occupancy and EV share.

Charging demand additionally requires charging-participation and energy
assumptions. Avoid adding visitor-related demand without checking whether
the same vehicles are already represented in the fleet model.

## Next permitted use

Compare registered-room capacity with mapped OSM tourism indicators as
alternative spatial proxies. Keep their coverage and units explicit.

District shares within one indicator differ from the four place weights
within one district. Neither automatically represents EV-demand shares.

This document activates no allocation weights or demand scaling.
District demand readiness remains false.

Existing manual spatial-review statuses remain unchanged.
Recording this policy performs no new simulations or model training.
