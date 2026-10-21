"""Measure selected mapped OSM road-way lengths in Sri Lanka's 25 districts."""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
from itertools import combinations
import json
import math
from pathlib import Path
import platform

import osmium
import pyproj
import shapely
from shapely.errors import GEOSException
from shapely.geometry import LineString, shape

ROOT = Path(__file__).resolve().parents[3]

CLASSES = (
    "primary",
    "trunk",
    "motorway",
    "primary_link",
    "trunk_link",
    "motorway_link",
)

AGGREGATES = {
    "mapped_primary_trunk_way_km": ("primary", "trunk"),
    "mapped_primary_trunk_motorway_way_km": CLASSES[:3],
    "mapped_selected_link_way_km": CLASSES[3:],
    "mapped_selected_way_km": CLASSES,
}

GEOD = pyproj.Geod(ellps="WGS84")

ISSUE_FIELDS = (
    "osm_key",
    "district_id",
    "issue",
    "detail",
)

LENGTH_FIELDS = (
    "osm_key",
    "highway_class",
    "district_id",
    "clipped_way_km",
    "shared_boundary_way_km",
    "allocated_way_km",
    "data_origin",
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
    fields = fields or list(rows[0])

    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def line_parts(geometry):
    if geometry.is_empty:
        return []

    if geometry.geom_type in {"LineString", "LinearRing"}:
        return [LineString(geometry.coords)]

    if hasattr(geometry, "geoms"):
        return [
            part
            for child in geometry.geoms
            for part in line_parts(child)
        ]

    # An isolated point contributes no road length.
    return []


def lineal(geometry):
    return shapely.union_all(line_parts(geometry))


def length_km(geometry):
    value = math.fsum(
        GEOD.geometry_length(part)
        for part in line_parts(geometry)
    ) / 1000

    require(
        math.isfinite(value) and value >= 0,
        "Invalid WGS84 line length.",
    )

    return value


def collect_roads(pbf):
    records = {}
    geometries = {}

    selected = osmium.filter.TagFilter(
        *[("highway", value) for value in CLASSES]
    )

    # The location cache receives nodes before the tag filter runs.
    processor = (
        osmium.FileProcessor(str(pbf))
        .with_locations("flex_mem")
        .with_filter(selected)
    )

    print(
        "Reading selected OSM road ways and retaining source issues...",
        flush=True,
    )

    for obj in processor:
        kind = "n" if obj.is_node() else "w" if obj.is_way() else "r"
        key = f"{kind}/{obj.id}"

        require(
            key not in records,
            "Duplicate current-state OSM object: " + key,
        )

        row = dict(
            osm_key=key,
            highway_class=obj.tags.get("highway"),
            source_tags_json=json.dumps(
                dict(obj.tags),
                ensure_ascii=False,
                sort_keys=True,
            ),
            node_count=len(obj.nodes) if obj.is_way() else None,
            geometry_status="pending",
            detail="",
            within_way_union_applied=False,
            raw_way_length_km=None,
            source_geometry_length_km=None,
            measurement_status="not_measured",
            intersecting_district_ids_json="[]",
            in_districts_way_km=None,
            shared_boundary_way_km=None,
            outside_district_layer_way_km=None,
            noded_reference_way_km=None,
            allocation_balance_error_km=None,
            noding_length_change_km=None,
            data_origin="PUBLIC_REFERENCE",
        )

        records[key] = row

        try:
            if not obj.is_way():
                row.update(
                    geometry_status="non_way_object",
                    detail=(
                        "Only way geometry supplies length; "
                        "nodes and relations add none."
                    ),
                )

            elif obj.tags.get("area") == "yes":
                row.update(
                    geometry_status="explicit_road_area",
                    detail=(
                        "area=yes is not interpreted as a road centreline."
                    ),
                )

            else:
                require(
                    all(node.location.valid() for node in obj.nodes),
                    "Missing node coordinates; way retained for review.",
                )

                coordinates = [
                    (node.lon, node.lat)
                    for node in obj.nodes
                ]

                require(
                    len(coordinates) >= 2,
                    "Fewer than two located nodes.",
                )

                require(
                    all(
                        math.isfinite(x)
                        and math.isfinite(y)
                        and -180 <= x <= 180
                        and -90 <= y <= 90
                        for x, y in coordinates
                    ),
                    "Invalid longitude/latitude.",
                )

                raw = LineString(coordinates)

                require(
                    raw.is_valid
                    and not raw.is_empty
                    and raw.length > 0,
                    "Invalid or zero-length way.",
                )

                geometry = lineal(raw) if not raw.is_simple else raw

                require(
                    geometry.is_valid and not geometry.is_empty,
                    "Invalid line geometry after within-way union.",
                )

                row.update(
                    geometry_status="valid_line",
                    within_way_union_applied=not raw.is_simple,
                    raw_way_length_km=length_km(raw),
                    source_geometry_length_km=length_km(geometry),
                )

                if not raw.is_simple:
                    row["detail"] = (
                        "Within this way only, coincident segments are "
                        "unioned; original length remains recorded."
                    )

                geometries[key] = geometry

        except (RuntimeError, ValueError, GEOSException) as exc:
            row.update(
                geometry_status="geometry_error",
                detail=str(exc),
            )

        if len(records) % 2000 == 0:
            print(
                f"Selected road objects reviewed: {len(records):,}",
                flush=True,
            )

    return records, geometries


def measure_roads(features, records, geometries):
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
        "Districts fail the non-overlapping shared-edge check.",
    )

    coverage = shapely.union_all(districts)
    shapely.prepare(coverage)

    tree = shapely.STRtree(districts)

    lengths = []
    errors = []
    failed = set()

    max_error = 0.0
    max_noding_change = 0.0

    for number, key in enumerate(sorted(records), 1):
        row = records[key]

        if row["geometry_status"] != "valid_line":
            continue

        geometry = geometries[key]
        candidates = sorted(map(int, tree.query(geometry)))

        try:
            clips = {}

            for i in candidates:
                if not districts[i].intersects(geometry):
                    continue

                clipped = lineal(
                    geometry.intersection(districts[i])
                )

                if length_km(clipped) > 0:
                    clips[i] = clipped

            shared = {i: 0.0 for i in clips}
            shared_once = []

            # Positive-length shared edges belong to two districts.
            # A point where a road crosses a boundary adds no length.
            for i, j in combinations(clips, 2):
                common = length_km(
                    clips[i].intersection(clips[j])
                )

                shared[i] += common
                shared[j] += common
                shared_once.append(common)

            if coverage.covers(geometry):
                outside = shapely.GeometryCollection()
            else:
                outside = lineal(
                    geometry.difference(coverage)
                )

            inside = shapely.union_all(list(clips.values()))

            reference = length_km(
                shapely.union_all([inside, outside])
            )

            selected = []

            for i, clipped in clips.items():
                raw_length = length_km(clipped)
                allocated = raw_length - shared[i] / 2

                require(
                    0 <= shared[i] <= raw_length
                    and allocated >= 0,
                    "Inconsistent shared-boundary length.",
                )

                selected.append(
                    dict(
                        osm_key=key,
                        highway_class=row["highway_class"],
                        district_id=ids[i],
                        clipped_way_km=raw_length,
                        shared_boundary_way_km=shared[i],
                        allocated_way_km=allocated,
                        data_origin="PUBLIC_REFERENCE",
                    )
                )

            allocated = math.fsum(
                part["allocated_way_km"]
                for part in selected
            )

            outside_length = length_km(outside)
            balance = allocated + outside_length - reference

            # km: 1 mm absolute plus 1e-8 relative.
            tolerance = 1e-6 + 1e-8 * reference

            require(
                abs(balance) <= tolerance,
                (
                    "District allocation exceeds the declared "
                    "length-balance tolerance."
                ),
            )

            noding_change = (
                reference - row["source_geometry_length_km"]
            )

            require(
                abs(noding_change) <= tolerance,
                (
                    "Unexpected line-length change when district "
                    "cuts insert vertices."
                ),
            )

            lengths.extend(selected)

            row.update(
                measurement_status=(
                    "measured"
                    if selected
                    else "outside_district_layer"
                ),
                intersecting_district_ids_json=json.dumps(
                    [ids[i] for i in clips]
                ),
                in_districts_way_km=allocated,
                shared_boundary_way_km=math.fsum(shared_once),
                outside_district_layer_way_km=outside_length,
                noded_reference_way_km=reference,
                allocation_balance_error_km=balance,
                noding_length_change_km=noding_change,
            )

            max_error = max(max_error, abs(balance))
            max_noding_change = max(
                max_noding_change,
                abs(noding_change),
            )

        except (ValueError, GEOSException) as exc:
            row.update(
                measurement_status="operation_error",
                detail=str(exc),
            )

            failed.update(
                (ids[i], row["highway_class"])
                for i in candidates
            )

            errors.append(
                dict(
                    osm_key=key,
                    district_id="",
                    issue="road_measurement_error",
                    detail=str(exc),
                )
            )

        if number % 1000 == 0:
            print(
                f"Road clipping progress: {number:,}/{len(records):,}",
                flush=True,
            )

    district_rows = []

    for feature in features:
        props = feature["properties"]
        district_id = props["district_id"]

        selected = [
            part
            for part in lengths
            if part["district_id"] == district_id
        ]

        values = {
            c: (
                None
                if (district_id, c) in failed
                else math.fsum(
                    part["allocated_way_km"]
                    for part in selected
                    if part["highway_class"] == c
                )
            )
            for c in CLASSES
        }

        result = dict(
            district_id=district_id,
            district_name=props["district_name"],
            province_name=props["province_name"],
            **{
                f"mapped_{c}_way_km": values[c]
                for c in CLASSES
            },
        )

        for name, classes in AGGREGATES.items():
            result[name] = (
                math.fsum(values[c] for c in classes)
                if all(values[c] is not None for c in classes)
                else None
            )

        result.update(
            measured_way_count=len(selected),
            shared_boundary_way_count=sum(
                p["shared_boundary_way_km"] > 0
                for p in selected
            ),
            measurement_complete=all(
                value is not None
                for value in values.values()
            ),
            measurement_scope="mapped_selected_way_geometries",
            data_origin="PUBLIC_REFERENCE",
        )

        district_rows.append(result)

    summaries = []

    for c in CLASSES:
        source = [
            row
            for row in records.values()
            if row["highway_class"] == c
        ]

        valid = [
            row
            for row in source
            if row["geometry_status"] == "valid_line"
        ]

        complete = all(
            row["measurement_status"] != "operation_error"
            for row in valid
        )

        summaries.append(
            dict(
                highway_class=c,
                source_objects=len(source),
                valid_ways=len(valid),
                unresolved_objects=len(source) - len(valid),
                ways_with_positive_district_length=sum(
                    (row["in_districts_way_km"] or 0) > 0
                    for row in valid
                ),
                source_geometry_way_km=math.fsum(
                    row["source_geometry_length_km"]
                    for row in valid
                ),
                mapped_in_districts_way_km=(
                    math.fsum(
                        row["in_districts_way_km"]
                        for row in valid
                    )
                    if complete
                    else None
                ),
                outside_district_layer_way_km=(
                    math.fsum(
                        row["outside_district_layer_way_km"]
                        for row in valid
                    )
                    if complete
                    else None
                ),
                measurement_complete=complete,
                data_origin="PUBLIC_REFERENCE",
            )
        )

        if complete:
            district_sum = math.fsum(
                row[f"mapped_{c}_way_km"]
                for row in district_rows
            )

            require(
                math.isclose(
                    district_sum,
                    summaries[-1]["mapped_in_districts_way_km"],
                    abs_tol=1e-9,
                    rel_tol=1e-12,
                ),
                "District and class length totals disagree.",
            )

    return (
        district_rows,
        lengths,
        summaries,
        errors,
        dict(
            maximum_absolute_balance_error_km=max_error,
            maximum_absolute_noding_length_change_km=max_noding_change,
        ),
    )


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

        require(
            bool(report.get("input_sha256"))
            and bool(report.get("output_sha256")),
            "Missing upstream manifests.",
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
            set(report["output_sha256"]) == outputs[schema],
            "Incomplete upstream exports: " + filename,
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

    watch("src/ev_demand/geography/extract_district_roads.py")
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

    records, geometries = collect_roads(pbf)

    require(
        bool(records),
        "No selected road source objects were found.",
    )

    rows, lengths, summaries, operation_errors, balance = measure_roads(
        features,
        records,
        geometries,
    )

    unresolved = [
        dict(
            osm_key=row["osm_key"],
            district_id="",
            issue=row["geometry_status"],
            detail=row["detail"],
        )
        for row in records.values()
        if row["geometry_status"] != "valid_line"
    ]

    notices = [
        dict(
            osm_key=row["osm_key"],
            district_id="",
            issue="within_way_union_review",
            detail=row["detail"],
        )
        for row in records.values()
        if row["within_way_union_applied"]
    ]

    require(
        len(rows) == len({row["district_id"] for row in rows}) == 25,
        "Incomplete district measurement grid.",
    )

    columns = (
        tuple(f"mapped_{c}_way_km" for c in CLASSES)
        + tuple(AGGREGATES)
    )

    require(
        all(
            row[column] is None
            or math.isfinite(row[column]) and row[column] >= 0
            for row in rows
            for column in columns
        ),
        "Invalid district road length.",
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
        / ("district_roads_" + now.strftime("%Y%m%dT%H%M%S_%fZ"))
    )

    output.mkdir(parents=True, exist_ok=False)

    write_csv(
        output / "district_road_features.csv",
        rows,
    )

    write_csv(
        output / "road_class_summary.csv",
        summaries,
    )

    write_csv(
        output / "road_source_audit.csv",
        [records[key] for key in sorted(records)],
    )

    write_csv(
        output / "road_district_lengths.csv",
        lengths,
        LENGTH_FIELDS,
    )

    write_csv(
        output / "road_issues.csv",
        unresolved + notices + operation_errors,
        ISSUE_FIELDS,
    )

    complete = (
        not operation_errors
        and all(row["measurement_complete"] for row in rows)
    )

    report = dict(
        schema_version="district_road_features_v1",
        status="roads_extracted_for_review",
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
        unresolved_source_objects=len(unresolved),
        within_way_union_review_objects=len(notices),
        operation_errors=len(operation_errors),
        district_measurements_complete=complete,
        ways_with_shared_boundary_length=sum(
            (row["shared_boundary_way_km"] or 0) > 0
            for row in records.values()
        ),
        ways_with_outside_district_length=sum(
            (row["outside_district_layer_way_km"] or 0) > 0
            for row in records.values()
        ),
        totals={
            column: (
                math.fsum(row[column] for row in rows)
                if all(row[column] is not None for row in rows)
                else None
            )
            for column in columns
        },
        length_balance=balance,
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
            selected_highway_values=list(CLASSES),
            units=(
                "WGS84 ellipsoidal kilometres; "
                "overlays in longitude/latitude"
            ),
            aggregates={
                name: list(classes)
                for name, classes in AGGREGATES.items()
            },
            shared_boundary=(
                "Exact positive-length district-boundary sections "
                "are shared equally between the two districts; "
                "point contacts add zero"
            ),
            duplicate_handling=(
                "Union coincident segments within one non-simple "
                "way only; preserve distinct way IDs, including "
                "separate carriageways"
            ),
            carriageways=(
                "No multiplication by lanes or by travel direction; "
                "route relations add no extra length"
            ),
            geometry=(
                "No repair, snapping, simplification, buffer "
                "or invented road geometry"
            ),
            outside=(
                "Length outside the supplied district polygons "
                "is retained separately, without assigning a district"
            ),
            balance_tolerance_km=(
                "1e-6 + 1e-8 * noded reference length; inserted "
                "cut vertices can slightly change geodesic sums"
            ),
            zero=(
                "No measurable selected mapped road length, "
                "not absence of roads or charging demand"
            ),
        ),
        osm_mapping_completeness_established=False,
        exact_dated_extract_extent_verified=False,
        traffic_volume_estimated=False,
        typology_weights_created=False,
        district_demand_ready=False,
        method_references=[
            (
                "https://docs.osmcode.org/pyosmium/latest/"
                "user_manual/04-Working-with-Filters/"
            ),
            "https://pyproj4.github.io/pyproj/stable/api/geod.html",
            "https://wiki.openstreetmap.org/wiki/Dual_carriageway",
            "https://wiki.openstreetmap.org/wiki/Highway_link",
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
            source_issues_retained=True,
            available_lengths_finite_nonnegative=True,
            completed_allocations_within_declared_tolerance=True,
            completed_class_totals_reproduced=True,
            input_files_unchanged=True,
        ),
        interpretation=[
            (
                "Mapped selected road-way length is a geographic "
                "proxy, not traffic, official route length, "
                "lane-km or EV arrivals."
            ),
            (
                "Different mapped carriageways remain separate "
                "ways; distinct overlapping ways are not "
                "automatically deduplicated."
            ),
            (
                "Completeness refers to calculations on supported "
                "source geometry; unsupported objects and mapping "
                "omissions remain limitations."
            ),
            (
                "Failed operations retain issues and leave affected "
                "district/class values null; no allocation weights "
                "or demand simulations are created."
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

    (output / "district_road_report.json").write_text(
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

    print("District mapped road extraction complete.")

    for key in (
        "status",
        "districts",
        "source_objects",
        "source_status_counts",
        "unresolved_source_objects",
        "operation_errors",
        "district_measurements_complete",
        "ways_with_shared_boundary_length",
        "ways_with_outside_district_length",
        "totals",
        "length_balance",
    ):
        print(f"{key}: {report[key]}")

    print("typology_weights_created: False")
    print("district_demand_ready: False")
    print(f"Output: {output.relative_to(ROOT)}")

    print(
        "Public mapped-way measurements only; no traffic estimates, "
        "allocation weights or demand simulations."
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
        raise SystemExit(f"District road extraction failed: {exc}")