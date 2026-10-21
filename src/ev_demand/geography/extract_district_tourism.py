"""Extract district tourism object counts and retain venue-identity review issues."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import gzip
import hashlib
from importlib.metadata import version
from itertools import combinations
import json
import math
from pathlib import Path
import platform
import re
import unicodedata

import osmium
import pyproj
import shapely
from shapely.errors import GEOSException
from shapely.geometry import Point, Polygon, mapping, shape

ROOT = Path(__file__).resolve().parents[3]

LEGACY = {"hotel", "guest_house"}

CATEGORIES = {
    "accommodation": {
        "hotel",
        "guest_house",
        "hostel",
        "motel",
        "apartment",
        "chalet",
        "camp_site",
        "caravan_site",
        "alpine_hut",
        "wilderness_hut",
    },
    "visitor_site": {
        "attraction",
        "museum",
        "gallery",
        "theme_park",
        "zoo",
        "aquarium",
        "viewpoint",
        "picnic_site",
    },
    "information": {"information"},
    "artwork": {"artwork"},
    "camp_pitch": {"camp_pitch"},
    "other_or_unclassified": set(),
}

ISSUE_FIELDS = (
    "osm_key",
    "issue",
    "detail",
)

PAIR_FIELDS = (
    "osm_key_a",
    "osm_key_b",
    "name_a",
    "name_b",
    "kind_a",
    "kind_b",
    "district_a",
    "district_b",
    "reasons_json",
    "review_status",
    "automatically_merged",
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_csv(path, rows, fields=None):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fields or list(rows[0]),
        )
        writer.writeheader()
        writer.writerows(rows)


def selected(tags):
    return "tourism" in tags or tags.get("amenity") in LEGACY


def classify(tags):
    kind = (
        tags.get("tourism")
        if "tourism" in tags
        else tags.get("amenity")
    )

    category = next(
        (
            name
            for name, values in CATEGORIES.items()
            if kind in values
        ),
        "other_or_unclassified",
    )

    flags = []

    if category == "other_or_unclassified":
        flags.append("tourism_value_requires_review")

    if (
        "tourism" in tags
        and tags.get("amenity") in LEGACY
        and tags["tourism"] != tags["amenity"]
    ):
        flags.append("tourism_and_legacy_amenity_disagree")

    for key in (
        "disused",
        "abandoned",
        "demolished",
        "construction",
        "proposed",
    ):
        if tags.get(key) in {"yes", "true", "1"}:
            flags.append(key + "_tag_requires_review")

    return kind, category, flags


def collect_tourism(pbf):
    records = {}
    geometries = {}
    assembled_areas = {}

    factory = osmium.geom.WKBFactory()

    tag_filter = osmium.filter.KeyFilter("tourism", "amenity")

    # Keep relation metadata. Native area assembly receives all members.
    tag_filter.enable_for(
        osmium.osm.NODE | osmium.osm.WAY | osmium.osm.AREA
    )

    processor = (
        osmium.FileProcessor(str(pbf))
        .with_locations("flex_mem")
        .with_areas()
        .with_filter(tag_filter)
    )

    print(
        "Reading tourism objects and assembling tagged relation areas...",
        flush=True,
    )

    def save_geometry(key, geometry, status):
        require(
            not geometry.is_empty and geometry.is_valid,
            "Invalid source geometry: "
            + shapely.is_valid_reason(geometry),
        )

        bounds = geometry.bounds

        require(
            all(math.isfinite(x) for x in bounds)
            and -180 <= bounds[0] <= bounds[2] <= 180
            and -90 <= bounds[1] <= bounds[3] <= 90,
            "Invalid source longitude/latitude.",
        )

        if status == "valid_area":
            require(
                geometry.geom_type in {"Polygon", "MultiPolygon"}
                and geometry.area > 0,
                "No valid positive area.",
            )

        geometries[key] = geometry
        records[key]["geometry_status"] = status

    for obj in processor:
        if obj.is_area():
            if obj.from_way():
                # The original tagged way already has one source ID.
                continue

            if not selected(dict(obj.tags)):
                continue

            key = f"r/{obj.orig_id()}"

            # Native areas may arrive before the original relation record.
            try:
                assembled_areas[key] = (
                    shapely.from_wkb(
                        factory.create_multipolygon(obj)
                    ),
                    "",
                )
            except (RuntimeError, ValueError, GEOSException) as exc:
                assembled_areas[key] = (None, str(exc))

            continue

        tags = dict(obj.tags)

        if not selected(tags):
            continue

        kind_prefix = (
            "n" if obj.is_node()
            else "w" if obj.is_way()
            else "r"
        )

        key = f"{kind_prefix}/{obj.id}"

        require(
            key not in records,
            "Duplicate current-state OSM object: " + key,
        )

        kind, category, flags = classify(tags)

        row = dict(
            osm_key=key,
            tourism_value=tags.get("tourism", ""),
            legacy_amenity_value=(
                tags.get("amenity", "")
                if tags.get("amenity") in LEGACY
                else ""
            ),
            legacy_amenity_only="tourism" not in tags,
            kind=kind,
            category=category,
            name=tags.get("name", ""),
            wikidata=tags.get("wikidata", ""),
            information_subtype=tags.get("information", ""),
            source_tags_json=json.dumps(
                tags,
                ensure_ascii=False,
                sort_keys=True,
            ),
            tag_review_flags_json=json.dumps(flags),
            geometry_status="pending",
            detail="",
            relation_members_json="[]",
            assignment_status="geometry_unresolved",
            district_id="",
            intersecting_district_ids_json="[]",
            display_lon=None,
            display_lat=None,
            duplicate_review_pairs=0,
            data_origin="PUBLIC_REFERENCE",
        )

        records[key] = row

        try:
            if obj.is_node():
                require(
                    obj.location.valid(),
                    "Missing node location.",
                )
                save_geometry(
                    key,
                    Point(obj.lon, obj.lat),
                    "valid_point",
                )

            elif obj.is_relation():
                row["relation_members_json"] = json.dumps(
                    [
                        dict(
                            osm_key=f"{member.type}/{member.ref}",
                            role=member.role,
                        )
                        for member in obj.members
                    ]
                )

                if tags.get("area") == "no":
                    row.update(
                        geometry_status="explicit_area_no",
                        detail="No area inferred from area=no.",
                    )

                elif tags.get("type") not in {
                    "multipolygon",
                    "boundary",
                }:
                    row.update(
                        geometry_status="unsupported_relation_type",
                        detail=tags.get("type", "missing type"),
                    )

                else:
                    row["geometry_status"] = "pending_relation"

            elif tags.get("area") == "no" or not obj.is_closed():
                row.update(
                    geometry_status="line_or_open_way",
                    detail=(
                        "A line or open way is retained without "
                        "inventing a point or polygon."
                    ),
                )

            else:
                require(
                    all(
                        node.location.valid()
                        for node in obj.nodes
                    ),
                    "Missing way-node locations.",
                )

                coordinates = [
                    (node.lon, node.lat)
                    for node in obj.nodes
                ]

                require(
                    len(coordinates) >= 4,
                    "Too few nodes for a closed area.",
                )

                save_geometry(
                    key,
                    Polygon(coordinates),
                    "valid_area",
                )

        except (RuntimeError, ValueError, GEOSException) as exc:
            row.update(
                geometry_status="geometry_error",
                detail=str(exc),
            )

        if len(records) % 2000 == 0:
            print(
                f"Tourism source objects reviewed: {len(records):,}",
                flush=True,
            )

    # Only originally tagged relations become source records.
    # Inherited assembler tags cannot invent another tourism object.
    for key, row in records.items():
        if row["geometry_status"] != "pending_relation":
            continue

        geometry, error = assembled_areas.get(
            key,
            (None, "No native assembled area was returned."),
        )

        if geometry is None:
            row.update(
                geometry_status="unassembled_relation",
                detail=error,
            )
        else:
            try:
                save_geometry(key, geometry, "valid_area")
            except (ValueError, GEOSException) as exc:
                row.update(
                    geometry_status="area_assembly_error",
                    detail=str(exc),
                )

    return records, geometries


def assign_districts(features, records, geometries):
    districts = [
        shape(feature["geometry"])
        for feature in features
    ]

    ids = [
        feature["properties"]["district_id"]
        for feature in features
    ]

    require(
        len(ids) == len(set(ids)),
        "Duplicate district IDs.",
    )

    require(
        all(
            g.geom_type in {"Polygon", "MultiPolygon"}
            and g.is_valid
            and not g.is_empty
            and g.area > 0
            for g in districts
        ),
        "Invalid district geometry.",
    )

    require(
        bool(shapely.coverage_is_valid(districts)),
        "District coverage fails the non-overlapping shared-edge check.",
    )

    shapely.prepare(districts)
    tree = shapely.STRtree(districts)

    errors = []
    failed = set()

    for key, geometry in geometries.items():
        row = records[key]
        candidates = sorted(map(int, tree.query(geometry)))

        try:
            covering = [
                i
                for i in candidates
                if districts[i].covers(geometry)
            ]

            if geometry.geom_type == "Point":
                hits = covering
            else:
                hits = [
                    i
                    for i in candidates
                    if geometry.intersection(districts[i]).area > 0
                ]

            row["intersecting_district_ids_json"] = json.dumps(
                [ids[i] for i in hits]
            )

            # Display only: this point does not allocate the object.
            point = geometry.representative_point()

            row.update(
                display_lon=point.x,
                display_lat=point.y,
            )

            if len(covering) == 1:
                row.update(
                    assignment_status="assigned",
                    district_id=ids[covering[0]],
                )

            elif len(covering) > 1:
                row["assignment_status"] = "district_boundary_review"

            elif len(hits) > 1:
                row["assignment_status"] = "cross_district_area_review"

            elif hits:
                row["assignment_status"] = (
                    "partial_district_coverage_review"
                )

            else:
                row["assignment_status"] = "outside_district_layer"

        except (ValueError, GEOSException) as exc:
            row.update(
                assignment_status="operation_error",
                detail=str(exc),
            )

            failed.update(ids[i] for i in candidates)

            errors.append(
                dict(
                    osm_key=key,
                    issue="district_assignment_error",
                    detail=str(exc),
                )
            )

    return errors, failed


def normalized_name(value):
    return " ".join(
        unicodedata.normalize("NFKC", value).casefold().split()
    )


def duplicate_review(records, geometries):
    pairs = defaultdict(set)

    def add(a, b, reason):
        if a != b:
            pairs[tuple(sorted((a, b)))].add(reason)

    entities = defaultdict(list)

    for key, row in records.items():
        identifier = row["wikidata"].strip()

        if re.fullmatch(r"Q[1-9][0-9]*", identifier):
            entities[identifier].append(key)

        if key.startswith("r/"):
            for member in json.loads(row["relation_members_json"]):
                child = member["osm_key"]

                if (
                    child in records
                    and records[child]["kind"] == row["kind"]
                ):
                    add(
                        key,
                        child,
                        "same_kind_relation_member_role="
                        + member["role"],
                    )

    # Use entity identifiers, not shared brand identifiers.
    for keys in entities.values():
        for a, b in combinations(keys, 2):
            add(a, b, "same_wikidata_entity")

    keys = sorted(geometries)
    geoms = [geometries[key] for key in keys]
    tree = shapely.STRtree(geoms)

    for i, a in enumerate(keys):
        for j in map(
            int,
            tree.query(geoms[i], predicate="intersects"),
        ):
            if j <= i:
                continue

            b = keys[j]

            if records[a]["kind"] != records[b]["kind"]:
                continue

            ga, gb = geoms[i], geoms[j]

            if ga.equals(gb):
                add(a, b, "same_kind_equal_geometry")

            node_in_area = (
                ga.geom_type == "Point"
                and gb.geom_type in {"Polygon", "MultiPolygon"}
                and gb.covers(ga)
            ) or (
                gb.geom_type == "Point"
                and ga.geom_type in {"Polygon", "MultiPolygon"}
                and ga.covers(gb)
            )

            if node_in_area:
                add(a, b, "same_kind_node_in_area")

            name = normalized_name(records[a]["name"])

            if (
                name
                and name == normalized_name(records[b]["name"])
            ):
                add(a, b, "same_kind_name_and_intersection")

    counts = Counter(
        key
        for pair in pairs
        for key in pair
    )

    for key, row in records.items():
        row["duplicate_review_pairs"] = counts[key]

    return [
        dict(
            osm_key_a=a,
            osm_key_b=b,
            name_a=records[a]["name"],
            name_b=records[b]["name"],
            kind_a=records[a]["kind"],
            kind_b=records[b]["kind"],
            district_a=records[a]["district_id"],
            district_b=records[b]["district_id"],
            reasons_json=json.dumps(sorted(reasons)),
            review_status="unreviewed",
            automatically_merged=False,
        )
        for (a, b), reasons in sorted(pairs.items())
    ]


def summarize(features, records, failed):
    rows = []

    for feature in features:
        props = feature["properties"]
        district_id = props["district_id"]

        source = [
            row
            for row in records.values()
            if row["district_id"] == district_id
            and row["assignment_status"] == "assigned"
        ]

        complete = district_id not in failed

        def count(condition):
            return (
                sum(condition(row) for row in source)
                if complete
                else None
            )

        result = dict(
            district_id=district_id,
            district_name=props["district_name"],
            province_name=props["province_name"],
            mapped_tourism_objects_raw=(
                len(source) if complete else None
            ),
        )

        for category in CATEGORIES:
            result[f"mapped_{category}_objects_raw"] = count(
                lambda row, category=category: (
                    row["category"] == category
                )
            )

        result.update(
            legacy_amenity_only_objects_raw=count(
                lambda row: row["legacy_amenity_only"]
            ),
            objects_with_duplicate_review=count(
                lambda row: row["duplicate_review_pairs"] > 0
            ),
            objects_with_tag_review=count(
                lambda row: bool(
                    json.loads(row["tag_review_flags_json"])
                )
            ),
            measurement_complete=complete,
            measurement_scope="unreviewed_mapped_OSM_object_counts",
            data_origin="PUBLIC_REFERENCE",
        )

        rows.append(result)

        if complete:
            require(
                sum(
                    result[f"mapped_{category}_objects_raw"]
                    for category in CATEGORIES
                ) == len(source),
                "Tourism category accounting failed.",
            )

    values = defaultdict(list)

    for row in records.values():
        origin = (
            "legacy_amenity_only"
            if row["legacy_amenity_only"]
            else "tourism"
        )

        values[
            (
                origin,
                row["kind"],
                row["information_subtype"],
                row["category"],
            )
        ].append(row)

    table = [
        dict(
            tag_origin=origin,
            tag_value=kind,
            information_subtype=info,
            category=category,
            source_objects=len(group),
            valid_points=sum(
                row["geometry_status"] == "valid_point"
                for row in group
            ),
            valid_areas=sum(
                row["geometry_status"] == "valid_area"
                for row in group
            ),
            assigned_objects=sum(
                row["assignment_status"] == "assigned"
                for row in group
            ),
            objects_with_duplicate_review=sum(
                row["duplicate_review_pairs"] > 0
                for row in group
            ),
            data_origin="PUBLIC_REFERENCE",
        )
        for (
            origin,
            kind,
            info,
            category,
        ), group in sorted(values.items())
    ]

    require(
        sum(row["source_objects"] for row in table) == len(records),
        "Tourism value-table accounting failed.",
    )

    if not failed:
        require(
            sum(
                row["mapped_tourism_objects_raw"]
                for row in rows
            ) == sum(
                row["assignment_status"] == "assigned"
                for row in records.values()
            ),
            "District object totals disagree with source assignments.",
        )

    return rows, table


def extract(osm_folder, project_root=ROOT):
    for package, expected in (
        ("osmium", "4.3.2"),
        ("shapely", "2.1.2"),
        ("pyproj", "3.7.2"),
    ):
        require(
            version(package) == expected,
            f"Use {package}=={expected}.",
        )

    require(
        shapely.geos_version >= (3, 12, 0),
        "Coverage checks require GEOS >= 3.12.",
    )

    root = Path(project_root).resolve()
    watched = {}

    def watch(value, expected=None):
        path = (root / value).resolve()

        require(
            path.is_relative_to(root) and path.is_file(),
            "Missing project input: " + str(value),
        )

        actual = watched.get(path) or digest(path)

        require(
            expected is None or actual == expected,
            "Input hash mismatch: " + str(value),
        )

        watched[path] = actual
        return path

    def load_report(folder, filename, schema, status):
        folder = (root / folder).resolve()

        require(
            folder.is_relative_to(root / "data/processed/reference"),
            "Use a processed public-reference folder.",
        )

        report = read_json(watch(folder / filename))

        require(
            report.get("schema_version") == schema
            and report.get("status") == status
            and report.get("data_origin") == "PUBLIC_REFERENCE"
            and report.get("checks")
            and all(
                value is True
                for value in report["checks"].values()
            ),
            "Unexpected upstream report: " + filename,
        )

        outputs = {
            "osm_country_snapshot_v1": {
                "osm_tag_presence.csv",
            },
            "district_geometry_audit_v1": {
                "district_geometry_audit.csv",
                "district_pair_audit.csv",
                "district_geometry_issues.geojson",
            },
            "district_boundary_identity_v1": {
                "district_boundaries.geojson",
                "district_boundary_identity_audit.csv",
            },
        }

        require(
            bool(report.get("input_sha256"))
            and set(report.get("output_sha256", {})) == outputs[schema],
            "Incomplete upstream manifests.",
        )

        for name, expected in report["input_sha256"].items():
            watch(name, expected)

        for name, expected in report["output_sha256"].items():
            require(
                Path(name).name == name,
                "Unexpected upstream output name.",
            )
            watch(folder / name, expected)

        return folder, report

    watch("src/ev_demand/geography/extract_district_tourism.py")
    watch("docs/research_pipeline_specification.md")

    folder, osm = load_report(
        osm_folder,
        "osm_snapshot_report.json",
        "osm_country_snapshot_v1",
        "source_inspected",
    )

    _, audit = load_report(
        osm["geometry_audit_folder"],
        "district_geometry_report.json",
        "district_geometry_audit_v1",
        "geometry_checks_passed",
    )

    boundary_folder, boundary = load_report(
        audit["boundary_folder"],
        "district_boundary_report.json",
        "district_boundary_identity_v1",
        "identity_and_coordinate_checks_complete",
    )

    pbf = watch(
        osm["source"]["snapshot"],
        osm["source"]["sha256"],
    )

    require(
        pbf.stat().st_size == osm["source"]["bytes"],
        "PBF size changed.",
    )

    collection = read_json(
        watch(
            boundary_folder / "district_boundaries.geojson",
            boundary["output_sha256"]["district_boundaries.geojson"],
        )
    )

    features = sorted(
        collection["features"],
        key=lambda feature: feature["properties"]["district_id"],
    )

    require(
        collection["type"] == "FeatureCollection"
        and len(features) == 25
        and len({
            feature["properties"]["province_name"]
            for feature in features
        }) == 9,
        "Incomplete district layer.",
    )

    records, geometries = collect_tourism(pbf)

    require(
        bool(records),
        "No selected tourism objects were found.",
    )

    errors, failed = assign_districts(
        features,
        records,
        geometries,
    )

    print(
        "Checking possible duplicate representations "
        "without merging objects...",
        flush=True,
    )

    pairs = duplicate_review(records, geometries)

    rows, value_table = summarize(
        features,
        records,
        failed,
    )

    issues = list(errors)

    for row in records.values():
        if row["geometry_status"] not in {
            "valid_point",
            "valid_area",
        }:
            issues.append(
                dict(
                    osm_key=row["osm_key"],
                    issue=row["geometry_status"],
                    detail=row["detail"],
                )
            )

        elif row["assignment_status"] not in {
            "assigned",
            "operation_error",
        }:
            issues.append(
                dict(
                    osm_key=row["osm_key"],
                    issue=row["assignment_status"],
                    detail=row["intersecting_district_ids_json"],
                )
            )

        for flag in json.loads(row["tag_review_flags_json"]):
            issues.append(
                dict(
                    osm_key=row["osm_key"],
                    issue=flag,
                    detail=row["source_tags_json"],
                )
            )

    require(
        len(rows) == len({
            row["district_id"] for row in rows
        }) == 25,
        "Incomplete district output grid.",
    )

    require(
        all(
            digest(path) == expected
            for path, expected in watched.items()
        ),
        "Source changed during extraction.",
    )

    now = datetime.now(timezone.utc)

    output = (
        root
        / "data/processed/reference"
        / ("district_tourism_" + now.strftime("%Y%m%dT%H%M%S_%fZ"))
    )

    output.mkdir(parents=True, exist_ok=False)

    write_csv(
        output / "district_tourism_features.csv",
        rows,
    )

    write_csv(
        output / "tourism_value_counts.csv",
        value_table,
    )

    write_csv(
        output / "tourism_source_audit.csv",
        [records[key] for key in sorted(records)],
    )

    write_csv(
        output / "tourism_duplicate_review.csv",
        pairs,
        PAIR_FIELDS,
    )

    write_csv(
        output / "tourism_issues.csv",
        issues,
        ISSUE_FIELDS,
    )

    with gzip.open(
        output / "tourism_source_geometries.geojsonl.gz",
        "wt",
        encoding="utf-8",
    ) as handle:
        for key in sorted(geometries):
            row = records[key]

            properties = {
                name: row[name]
                for name in (
                    "osm_key",
                    "kind",
                    "category",
                    "district_id",
                    "assignment_status",
                    "data_origin",
                )
            }

            feature = dict(
                type="Feature",
                properties=properties,
                geometry=mapping(geometries[key]),
            )

            handle.write(
                json.dumps(
                    feature,
                    ensure_ascii=False,
                    allow_nan=False,
                ) + "\n"
            )

    assigned = sum(
        row["assignment_status"] == "assigned"
        for row in records.values()
    )

    report = dict(
        schema_version="district_tourism_features_v1",
        status="tourism_extracted_for_review",
        data_origin="PUBLIC_REFERENCE",
        created_utc=now.isoformat(),
        districts=25,
        source_objects=len(records),
        source_status_counts=dict(
            Counter(
                row["geometry_status"]
                for row in records.values()
            )
        ),
        assignment_status_counts=dict(
            Counter(
                row["assignment_status"]
                for row in records.values()
            )
        ),
        assigned_source_objects=assigned,
        unassigned_source_objects=len(records) - assigned,
        operation_errors=len(errors),
        district_measurements_complete=not errors,
        potential_duplicate_pairs=len(pairs),
        objects_with_duplicate_review=sum(
            row["duplicate_review_pairs"] > 0
            for row in records.values()
        ),
        objects_with_tag_review=sum(
            bool(json.loads(row["tag_review_flags_json"]))
            for row in records.values()
        ),
        assigned_category_counts={
            category: sum(
                row["assignment_status"] == "assigned"
                and row["category"] == category
                for row in records.values()
            )
            for category in CATEGORIES
        },
        source_osm_folder=folder.relative_to(root).as_posix(),
        osm_data_timestamp=osm["source"]["osm_data_timestamp"],
        boundary_reference_year=boundary["source"]["source_reference_year"],
        boundary_attribution=boundary["source"]["attribution"],
        boundary_licence=boundary["source"]["licence"],
        boundary_licence_url=boundary["source"]["licence_url"],
        source_licence=osm["source"]["licence"],
        source_licence_url=osm["source"]["licence_url"],
        attribution=osm["source"]["attribution"],
        rules=dict(
            selection=[
                "tourism=*",
                "amenity=hotel",
                "amenity=guest_house",
            ],
            categories={
                category: sorted(values)
                for category, values in CATEGORIES.items()
            },
            residual_category="other_or_unclassified",
            source_identity=(
                "One record per originally tagged node/way/relation; "
                "native area events do not add another source object"
            ),
            assignment=(
                "Assign only when full point/area geometry is covered "
                "by exactly one district; retain boundary, cross-district "
                "and partial-coverage cases unassigned"
            ),
            display_points=(
                "Representative points are for display only; "
                "they do not determine district assignment"
            ),
            duplicate_policy=(
                "Flag specified identity/geometry/member relationships; "
                "do not merge, remove or downweight source objects"
            ),
            tag_policy=(
                "Retain raw values, including unknown/multiple/lifecycle "
                "values; category labels are descriptive and have "
                "no demand weights"
            ),
            geometry=(
                "Validate raw closed-way polygons and native relation "
                "multipolygons; no Shapely repair, snapping or invented "
                "point for an open way"
            ),
            measurement_complete=(
                "Supported assignments processed without operation "
                "errors; unresolved source geometry, boundary coverage "
                "and venue-identity review can remain"
            ),
            zero=(
                "No assigned measurable selected mapped objects; "
                "does not establish absence of tourist activity"
            ),
        ),
        venue_deduplication_complete=False,
        unique_venue_counts_established=False,
        visitor_volume_estimated=False,
        osm_mapping_completeness_established=False,
        exact_dated_extract_extent_verified=False,
        typology_weights_created=False,
        district_demand_ready=False,
        method_references=[
            "https://wiki.openstreetmap.org/wiki/Key:tourism",
            "https://wiki.openstreetmap.org/wiki/Tag:tourism=hotel",
            (
                "https://docs.osmcode.org/pyosmium/latest/"
                "user_manual/03-Working-with-Geometries/"
            ),
            (
                "https://shapely.readthedocs.io/en/2.1.2/"
                "reference/shapely.covers.html"
            ),
        ],
        software={
            **{
                package: version(package)
                for package in ("osmium", "shapely", "pyproj")
            },
            "python": platform.python_version(),
            "GEOS": shapely.geos_version_string,
            "PROJ": pyproj.proj_version_str,
        },
        checks=dict(
            upstream_hashes_verified=True,
            complete_25_district_grid=True,
            source_and_category_accounting_passed=True,
            completed_district_assignments_reproduced=not errors,
            unresolved_sources_retained=True,
            input_files_unchanged=True,
        ),
        interpretation=[
            (
                "These are unreviewed OSM object counts, not unique "
                "venues, visitors, EV arrivals or charging sessions."
            ),
            (
                "Information features, artwork and camp pitches "
                "remain separate from accommodation and visitor-site "
                "categories."
            ),
            (
                "Potential duplicates are review candidates, not "
                "proof that two source IDs describe the same venue; "
                "the review is not exhaustive."
            ),
            (
                "Objects without an unambiguous supported geometry/"
                "district remain in the source audit; all original "
                "input files are preserved."
            ),
            (
                "No charging parameters, owner eligibility, district "
                "allocation weights or demand simulations are changed."
            ),
        ],
        input_sha256={
            path.relative_to(root).as_posix(): expected
            for path, expected in watched.items()
        },
        output_sha256={
            path.name: digest(path)
            for path in sorted(output.iterdir())
        },
    )

    require(
        all(
            digest(path) == expected
            for path, expected in watched.items()
        ),
        "Source changed during export.",
    )

    (output / "district_tourism_report.json").write_text(
        json.dumps(
            report,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        ) + "\n",
        encoding="utf-8",
    )

    return output, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument(
        "--osm",
        required=True,
        help="Completed OSM snapshot folder",
    )

    args = parser.parse_args()
    output, report = extract(args.osm)

    print("District mapped tourism extraction complete.")

    for key in (
        "status",
        "districts",
        "source_objects",
        "source_status_counts",
        "assignment_status_counts",
        "assigned_source_objects",
        "unassigned_source_objects",
        "operation_errors",
        "district_measurements_complete",
        "potential_duplicate_pairs",
        "objects_with_duplicate_review",
        "objects_with_tag_review",
        "assigned_category_counts",
    ):
        print(f"{key}: {report[key]}")

    print("unique_venue_counts_established: False")
    print("typology_weights_created: False")
    print("district_demand_ready: False")
    print(f"Output: {output.relative_to(ROOT)}")

    print(
        "Public mapped-object counts and review candidates only; "
        "no visitor estimates, weights or demand simulations."
    )


if __name__ == "__main__":
    try:
        main()
    except (
        ValueError,
        OSError,
        KeyError,
        TypeError,
        RuntimeError,
        GEOSException,
    ) as exc:
        raise SystemExit(f"District tourism extraction failed: {exc}")