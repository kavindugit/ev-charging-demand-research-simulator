# Five-district source spot-check guide

This packet prepares checks; no human finding has been entered.

## District selection

| District | Reason |
|---|---|
| Colombo | commercial_maximum |
| Hambantota | residential_maximum |
| Galle | tourism_maximum |
| Mannar | commercial_minimum |
| Kilinochchi | largest_normalization_only_shift |

Ties use district-ID order and are recorded in district_selection.csv. This is a purposive extreme-case check, not a representative district sample.

## Review once, record the evidence

1. Keep this generated packet unchanged. Copy manual_review_template.csv to docs/spatial_source_review_responses.csv for your findings.
2. Work through its 20 district/topic tasks. Use source_examples.csv for saved source records and OSM object/history links; the original audit paths remain available.
3. source_example_coverage.csv shows which mapped groups have examples. An empty group means no eligible example in the saved extraction, not absence of real activity. Inspect the original saved land-use union geometry when checking area extent and overlap.
4. spatial_worklist_with_links.csv retains every original review entry. Filter selected_district_related=True for these five districts. Rows with no_district_link=True still require separate geographic review; no district was guessed.
5. Record inspected IDs, findings and evidence. Use reviewed_no_change, change_requested or unresolved only after inspection. Leave uninspected tasks pending. Add reviewer and UTC review time.
6. Live OSM pages may have changed since the saved snapshot. Record access time and distinguish live observations from saved tags/geometry. This same-source check is not independent ground-truth validation.
7. Do not change source counts or choose weights to obtain expected rankings. Proposed fixes need a separate reviewed transformation, with before/after evidence and sensitivity checks.

## Boundaries of this check

At most two deterministic examples per district/layer group; these are spot checks, not a completeness estimate or an exhaustive object review. Global issues remain pending even when local examples look correct.
The eight compositions retain their original 25-district references; weights are not renormalized within these five districts. Geographic P1-P4 dimensions remain separate from charging contexts T1-T4.
The next decision is a documented geographic proxy definition and its allocation role, informed by the source review and public evidence. This packet activates no production weights, fleet scaling or demand simulations.

## Source attribution

OSM snapshot: 2026-10-19T20:21:11Z
© OpenStreetMap contributors; extract provided by Geofabrik.
https://opendatacommons.org/licenses/odbl/1-0/

Boundary reference year: 2022
Survey Department of Sri Lanka; UN OCHA/HDX COD-AB; geoBoundaries (Runfola et al., 2020).
https://creativecommons.org/licenses/by/3.0/igo/
