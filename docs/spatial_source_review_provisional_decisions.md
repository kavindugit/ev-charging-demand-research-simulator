# Spatial source review: provisional development decisions

Decision date: 2026-10-20

## Scope and status

Record assistant-assisted desk-review findings for the five selected
districts: Colombo, Hambantota, Galle, Mannar and Kilinochchi.

The districts were selected to investigate feature extremes and
normalization sensitivity. They are not a representative validation sample.

The selected worklist contains 22 entries:
- 12 entries covered by the saved geometry views.
- Six additional road entries.
- Four additional tourism-tag entries.

This document does not complete the human review tasks.
Generated review statuses remain pending.
No field verification or mapping-completeness assessment is claimed.

## Source references

OSM snapshot:
data/processed/reference/osm_snapshot_20261020T124821_702183Z

District feature review:
data/processed/reference/district_feature_review_20261020T142640_144883Z

Proxy sensitivity:
data/processed/reference/spatial_proxy_sensitivity_20261020T145327_997094Z

Five-district review packet:
data/processed/reference/spatial_source_review_20261020T150647_144752Z

Saved geometry views:
data/processed/reference/spatial_geometry_views_20261020T153544_270640Z

OSM data timestamp: 2026-10-19T20:21:11Z.
District boundary reference year: 2022.

## Land-use overlap treatment

Retain commercial/retail and residential measurements and their separately
reported intersection areas.

Nested or overlapping mapped polygons can reflect different mapping
granularity or conflicting classifications. Geometry alone does not
establish the correct real-world land use.

No class priority, source deletion or polygon repair is introduced.
The selected-land share is not an estimate of mapping completeness.

## Tourism representation treatment

The headline tourism feature remains a count of assigned OSM source
objects. It is not a count of independently verified establishments.

The eight selected candidate pairs include:
- Six point-and-area representation pairs.
- One pair of adjacent multipolygon representations.
- One multipolygon relation and its outer member way.

Containment, matching names and relation membership provide review
evidence. They do not justify subtracting every candidate pair from
the count.

The Tintagel relation contains an inner courtyard. Any future canonical
geometry rule must preserve that hole.

Different names within one footprint and multiple footprints with one
name remain establishment-identity questions.

No automatic merging, deletion or unique-venue claim is introduced.

## Road portions outside the district layer

The complete road extraction retains 49 ways with outside portions,
totalling 15.100155 km outside the supplied district polygon union.

The six selected entries reproduce in an independent inspection of the
checksum-pinned source geometry:

| Review ID | OSM way | Related district | Outside km |
|---|---|---|---:|
| SPATIAL-0040 | 153486334 | Mannar | 0.128543 |
| SPATIAL-0041 | 153486335 | Mannar | 2.830078 |
| SPATIAL-0043 | 161272509 | Mannar | 0.476985 |
| SPATIAL-0060 | 667551066 | Mannar | 0.024008 |
| SPATIAL-0061 | 667551067 | Mannar | 0.005343 |
| SPATIAL-0074 | 941626574 | Kilinochchi | 2.282411 |

Their combined outside length is approximately 5.747369 km.

Way 153486334 has bridge=yes in the saved OSM source.
Way 941626574 is named Sangupiddy Causeway and has causeway=yes.

These are source-tag observations, not independent jurisdiction checks.
Outside the polygon layer does not mean outside Sri Lanka.

Retain measured inside lengths and separately retain outside lengths.
Do not snap roads, expand boundaries or assign outside portions to the
nearest district. No traffic or demand impact has been established.

## Tourism-tag treatment

The following four Galle objects remain other_or_unclassified:

| Review ID | OSM object | Saved tourism value |
|---|---|---|
| SPATIAL-0163 | n/5444598797 | cabin |
| SPATIAL-0166 | n/7597753515 | villa |
| SPATIAL-0169 | w/357786016 | resort |
| SPATIAL-0170 | w/460434201 | hotel;hostel |

They are already included in the broad assigned-object count.
They are excluded from the current accommodation-plus-visitor variant.

Retain the original tags. Do not infer accommodation facilities from
names alone. Do not split hotel;hostel into two establishment counts.

Any future harmonization must use an explicit, versioned rule applied
consistently across all districts, with its effect reported separately.

Guidance consulted:
https://wiki.openstreetmap.org/wiki/Tag:tourism=resort
https://wiki.openstreetmap.org/wiki/Semi-colon_value_separator

## Proxy interpretation

The eight proxy constructions remain diagnostic alternatives.
Neither normalization method is validated as an EV-demand allocation rule.

Positive mapped quantities can receive zero scores under min-max scaling.
Normalization choices produced substantial composition changes in some
districts. These effects must remain visible in subsequent comparisons.

Production allocation weights remain inactive.
District demand readiness remains false.

## Next work

Acquire and inspect official SLTDA accommodation and occupancy evidence.

Record source year, geographic coverage, establishment categories,
units, reporting population and missing districts.

Keep rooms, beds, room occupancy and bed occupancy distinct.
Accommodation capacity and occupancy do not directly establish vehicle
arrivals, EV arrivals or charging demand.

Complete recorded human findings before claiming manual validation.
Retain unresolved spatial issues when developing allocation sensitivities.

This document changes no raw sources, generated measurements, simulation
parameters, owner eligibility or demand results.
