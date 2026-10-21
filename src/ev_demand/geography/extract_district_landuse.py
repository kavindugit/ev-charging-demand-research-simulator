"""Measure mapped commercial/retail and residential land in the 25 districts."""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import gzip
import hashlib
from importlib.metadata import version
import json
import math
from pathlib import Path
import platform

import osmium
import pyproj
import shapely
from shapely.errors import GEOSException
from shapely.geometry import Point, Polygon, mapping, shape
from shapely.geometry.polygon import orient

ROOT = Path(__file__).resolve().parents[3]
GROUP = {
    "commercial": "commercial_retail",
    "retail": "commercial_retail",
    "residential": "residential",
}
GEOD = pyproj.Geod(ellps="WGS84")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_csv(path, rows):
    require(bool(rows), "Expected a nonempty audit table.")
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def area_km2(geometry):
    if geometry.is_empty:
        return 0.0
    if geometry.geom_type == "Polygon":
        area = abs(
            GEOD.geometry_area_perimeter(
                orient(geometry, sign=1.0)
            )[0]
        ) / 1e6
    elif hasattr(geometry, "geoms"):
        area = sum(area_km2(part) for part in geometry.geoms)
    else:
        area = 0.0
    require(math.isfinite(area), "Nonfinite geodesic area.")
    return area


def collect_landuse(pbf):
    records, geometries, relations = {}, {}, {}
    factory = osmium.geom.WKBFactory()

    def record(key, value):
        if key not in records:
            records[key] = dict(
                osm_key=key,
                landuse_value_used=value,
                group=GROUP[value],
                tag_origin="original_osm_tags",
                source_tags_json="{}",
                geometry_status="pending",
                detail="",
                standalone_status="",
                standalone_detail="",
                represented_by_json="[]",
                intersecting_district_ids_json="[]",
            )
        return records[key]

    def save_geometry(key, geometry):
        geometries[key] = geometry
        row = records[key]
        if (
            geometry.is_empty
            or not geometry.is_valid
            or geometry.area <= 0
        ):
            row.update(
                geometry_status="invalid_area",
                detail=(
                    "Empty, invalid or zero-area geometry: "
                    + shapely.is_valid_reason(geometry)
                ),
            )
        else:
            row["geometry_status"] = "valid_area"

    selected = osmium.filter.TagFilter(
        *[("landuse", value) for value in GROUP]
    )
    selected.enable_for(
        osmium.osm.NODE | osmium.osm.WAY | osmium.osm.AREA
    )
    processor = (
        osmium.FileProcessor(str(pbf))
        .with_locations("flex_mem")
        .with_areas()
        .with_filter(selected)
    )

    print(
        "Assembling OSM areas and retaining selected land-use objects...",
        flush=True,
    )
    seen = 0
    for obj in processor:
        seen += 1
        if seen % 2000 == 0:
            print(
                f"Selected objects / relation metadata reviewed: {seen:,}",
                flush=True,
            )

        value = obj.tags.get("landuse", "")

        if obj.is_relation():
            key = f"r/{obj.id}"
            tags = dict(obj.tags)
            relations[key] = dict(
                tags=tags,
                outer_ways=[
                    f"w/{member.ref}"
                    for member in obj.members
                    if member.type == "w" and member.role == "outer"
                ],
                empty_role_ways=[
                    f"w/{member.ref}"
                    for member in obj.members
                    if member.type == "w" and member.role == ""
                ],
            )
            if value in GROUP:
                row = record(key, value)
                row["source_tags_json"] = json.dumps(
                    tags, ensure_ascii=False, sort_keys=True
                )
                if tags.get("type") not in {"multipolygon", "boundary"}:
                    row.update(
                        geometry_status="unsupported_relation_type",
                        detail=tags.get("type", "missing type"),
                    )

        elif obj.is_area():
            if obj.from_way() or value not in GROUP:
                continue
            key = f"r/{obj.orig_id()}"
            record(key, value)
            try:
                save_geometry(
                    key,
                    shapely.from_wkb(
                        factory.create_multipolygon(obj)
                    ),
                )
            except (RuntimeError, ValueError, GEOSException) as exc:
                records[key].update(
                    geometry_status="area_assembly_error",
                    detail=str(exc),
                )

        elif value in GROUP:
            kind = "n" if obj.is_node() else "w"
            key = f"{kind}/{obj.id}"
            row = record(key, value)
            row["source_tags_json"] = json.dumps(
                dict(obj.tags), ensure_ascii=False, sort_keys=True
            )
            try:
                if obj.is_node():
                    geometries[key] = Point(obj.lon, obj.lat)
                    row.update(
                        geometry_status="non_area_node",
                        detail="A point does not report a land area.",
                    )
                elif obj.tags.get("area") == "no":
                    row.update(
                        geometry_status="explicit_area_no",
                        detail="Source explicitly states area=no.",
                    )
                elif not obj.is_closed():
                    row.update(
                        geometry_status="open_way",
                        detail=(
                            "No standalone polygon; a parent relation "
                            "may represent it."
                        ),
                    )
                else:
                    save_geometry(
                        key,
                        Polygon([
                            (node.lon, node.lat)
                            for node in obj.nodes
                        ]),
                    )
            except (RuntimeError, ValueError, GEOSException) as exc:
                row.update(
                    geometry_status="geometry_error",
                    detail=str(exc),
                )

    for key, row in records.items():
        if key.startswith("r/"):
            source = relations.get(key, {}).get("tags", {})
            row["source_tags_json"] = json.dumps(
                source, ensure_ascii=False, sort_keys=True
            )
            original = source.get("landuse", "")
            if original not in GROUP:
                row["tag_origin"] = "native_assembler_inherited_tags"
            elif GROUP[original] != row["group"]:
                row.update(
                    geometry_status="source_tag_conflict",
                    detail="Original and assembled land-use groups differ.",
                )
            if source.get("area") == "no":
                row.update(
                    geometry_status="explicit_area_no",
                    detail="Source explicitly states area=no.",
                )
            if row["geometry_status"] == "pending":
                row.update(
                    geometry_status="unassembled_relation",
                    detail="No valid assembled area was returned.",
                )

    # A tagged outer way must not fill a hole in its complete parent area.
    parents, uncertain_parents = {}, {}
    for key, relation in relations.items():
        if (
            key in records
            and records[key]["geometry_status"] == "valid_area"
        ):
            for child in relation["outer_ways"]:
                if (
                    child in records
                    and records[child]["group"] == records[key]["group"]
                ):
                    parents.setdefault(child, []).append(key)

            for child in relation["empty_role_ways"]:
                if (
                    child in records
                    and records[child]["group"] == records[key]["group"]
                ):
                    uncertain_parents.setdefault(child, []).append(key)

    for key, parent_ids in uncertain_parents.items():
        row = records[key]
        row.update(
            standalone_status=row["geometry_status"],
            standalone_detail=row["detail"],
            geometry_status="ambiguous_member_role",
            detail=(
                "Same-class parent has an unspecified member role; "
                "component area deferred."
            ),
            represented_by_json=json.dumps(sorted(parent_ids)),
        )

    for key, parent_ids in parents.items():
        row = records[key]
        if (
            row["geometry_status"] != "explicit_area_no"
            and row["standalone_status"] != "explicit_area_no"
        ):
            row.update(
                standalone_status=(
                    row["standalone_status"] or row["geometry_status"]
                ),
                standalone_detail=(
                    row["standalone_detail"] or row["detail"]
                ),
                geometry_status="represented_by_relation",
                represented_by_json=json.dumps(sorted(parent_ids)),
                detail=(
                    "Use the complete same-class parent area, "
                    "including its holes."
                ),
            )

    return records, geometries


def measure_districts(features, records, geometries):
    districts = [
        shape(feature["geometry"]) for feature in features
    ]
    ids = [
        feature["properties"]["district_id"]
        for feature in features
    ]
    require(len(ids) == len(set(ids)), "Duplicate district IDs.")
    require(
        all(
            not geometry.is_empty
            and geometry.is_valid
            and geometry.area > 0
            for geometry in districts
        ),
        "Invalid district geometry.",
    )
    require(
        bool(shapely.coverage_is_valid(districts)),
        "District coverage does not pass the shared-edge check.",
    )
    shapely.prepare(districts)
    tree = shapely.STRtree(districts)

    pieces = {
        group: [[] for _ in districts]
        for group in set(GROUP.values())
    }
    counts = {
        group: [0 for _ in districts]
        for group in set(GROUP.values())
    }
    errors, failed = [], set()

    for number, key in enumerate(sorted(records), 1):
        row = records[key]
        if row["geometry_status"] != "valid_area":
            continue

        geometry = geometries[key]
        hits = []
        for i in sorted(map(int, tree.query(geometry))):
            try:
                if not districts[i].intersects(geometry):
                    continue
                clipped = geometry.intersection(districts[i])
                if not clipped.is_empty and clipped.area > 0:
                    pieces[row["group"]][i].append(clipped)
                    counts[row["group"]][i] += 1
                    hits.append(ids[i])
            except GEOSException as exc:
                failed.add((row["group"], i))
                errors.append(dict(
                    osm_key=key,
                    district_id=ids[i],
                    issue="intersection_error",
                    detail=str(exc),
                ))

        row["intersecting_district_ids_json"] = json.dumps(hits)
        if number % 1000 == 0:
            print(
                f"Land-use object clipping progress: "
                f"{number:,}/{len(records):,}",
                flush=True,
            )

    rows, union_features = [], []
    for i, feature in enumerate(features):
        props = feature["properties"]
        unions, values = {}, {}

        for group in sorted(pieces):
            try:
                merged = shapely.union_all(pieces[group][i])
                require(
                    merged.is_empty or merged.is_valid,
                    "Invalid land-use union.",
                )
                unions[group] = merged
                values[group] = (
                    None if (group, i) in failed
                    else area_km2(merged)
                )
                if not merged.is_empty:
                    union_features.append(dict(
                        type="Feature",
                        properties=dict(
                            district_id=ids[i],
                            landuse_group=group,
                            measurement_complete=(
                                (group, i) not in failed
                            ),
                            data_origin="PUBLIC_REFERENCE",
                        ),
                        geometry=mapping(merged),
                    ))
            except (ValueError, GEOSException) as exc:
                values[group] = None
                errors.append(dict(
                    osm_key="",
                    district_id=ids[i],
                    issue="union_error",
                    detail=str(exc),
                ))

        overlap = None
        if len(unions) == 2:
            try:
                overlap = area_km2(
                    unions["commercial_retail"].intersection(
                        unions["residential"]
                    )
                )
            except GEOSException as exc:
                errors.append(dict(
                    osm_key="",
                    district_id=ids[i],
                    issue="cross_class_overlay_error",
                    detail=str(exc),
                ))

        district_area = area_km2(districts[i])
        selected_area = None
        if (
            len(unions) == 2
            and all(value is not None for value in values.values())
        ):
            try:
                selected_area = area_km2(
                    shapely.union_all(list(unions.values()))
                )
            except GEOSException as exc:
                errors.append(dict(
                    osm_key="",
                    district_id=ids[i],
                    issue="selected_union_error",
                    detail=str(exc),
                ))

        rows.append(dict(
            district_id=ids[i],
            district_name=props["district_name"],
            province_name=props["province_name"],
            district_reference_area_km2=district_area,
            mapped_commercial_retail_km2=values.get(
                "commercial_retail"
            ),
            mapped_residential_km2=values.get("residential"),
            commercial_residential_overlap_km2=overlap,
            commercial_retail_source_areas=counts[
                "commercial_retail"
            ][i],
            residential_source_areas=counts["residential"][i],
            mapped_selected_land_share_percent=(
                100 * selected_area / district_area
                if selected_area is not None else None
            ),
            measurement_scope="mapped_selected_areas_only",
            data_origin="PUBLIC_REFERENCE",
        ))

        print(
            f"Measured {i + 1}/{len(features)} districts: "
            f"{props['district_name']}",
            flush=True,
        )

    return rows, union_features, errors


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
    root, watched = Path(project_root).resolve(), {}

    def watch(value, expected=None):
        path = (root / value).resolve()
        require(
            path.is_relative_to(root) and path.is_file(),
            "Missing project input: " + str(value),
        )
        actual = digest(path)
        require(
            (expected is None or actual == expected)
            and (path not in watched or watched[path] == actual),
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
        expected_outputs = {
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
            set(report["output_sha256"]) == expected_outputs[schema],
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

    watch("src/ev_demand/geography/extract_district_landuse.py")
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
    _, boundary = load_report(
        audit["boundary_folder"],
        "district_boundary_report.json",
        "district_boundary_identity_v1",
        "identity_and_coordinate_checks_complete",
    )

    pbf = watch(
        osm["source"]["snapshot"], osm["source"]["sha256"]
    )
    require(
        pbf.stat().st_size == osm["source"]["bytes"],
        "PBF size changed.",
    )
    boundary_folder = root / audit["boundary_folder"]
    collection = read_json(watch(
        boundary_folder / "district_boundaries.geojson",
        boundary["output_sha256"]["district_boundaries.geojson"],
    ))
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

    records, geometries = collect_landuse(pbf)
    require(
        bool(records),
        "No selected land-use source objects were found.",
    )
    rows, union_features, operation_errors = measure_districts(
        features, records, geometries
    )
    statuses = Counter(
        row["geometry_status"] for row in records.values()
    )
    unresolved = [
        dict(
            osm_key=row["osm_key"],
            district_id="",
            issue=row["geometry_status"],
            detail=row["detail"],
        )
        for row in records.values()
        if row["geometry_status"] not in {
            "valid_area", "represented_by_relation"
        }
    ]
    issues = unresolved + operation_errors

    require(
        len(rows) == len({
            row["district_id"] for row in rows
        }) == 25,
        "Incomplete district measurement grid.",
    )
    area_columns = (
        "mapped_commercial_retail_km2",
        "mapped_residential_km2",
        "commercial_residential_overlap_km2",
    )
    numeric = area_columns + (
        "district_reference_area_km2",
        "mapped_selected_land_share_percent",
    )
    require(
        all(
            row[key] is None
            or (math.isfinite(row[key]) and row[key] >= 0)
            for row in rows for key in numeric
        ),
        "Invalid measured land-use quantity.",
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
        root / "data/processed/reference"
        / ("district_landuse_"
           + now.strftime("%Y%m%dT%H%M%S_%fZ"))
    )
    output.mkdir(parents=True, exist_ok=False)

    write_csv(output / "district_landuse_features.csv", rows)
    write_csv(
        output / "landuse_source_audit.csv",
        [records[key] for key in sorted(records)],
    )
    with (output / "landuse_issues.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["osm_key", "district_id", "issue", "detail"],
        )
        writer.writeheader()
        writer.writerows(issues)

    with gzip.open(
        output / "district_landuse_unions.geojsonl.gz",
        "wt",
        encoding="utf-8",
    ) as handle:
        for feature in union_features:
            handle.write(
                json.dumps(
                    feature, ensure_ascii=False, allow_nan=False
                ) + "\n"
            )

    complete = all(
        row[key] is not None for row in rows for key in numeric
    )
    report = dict(
        schema_version="district_landuse_features_v1",
        status="landuse_extracted_for_review",
        data_origin="PUBLIC_REFERENCE",
        created_utc=now.isoformat(),
        districts=25,
        source_objects=len(records),
        source_status_counts=dict(statuses),
        unresolved_source_objects=len(unresolved),
        operation_errors=len(operation_errors),
        district_measurements_complete=complete,
        districts_with_cross_class_overlap=sum(
            (row["commercial_residential_overlap_km2"] or 0) > 0
            for row in rows
        ),
        source_osm_folder=folder.relative_to(root).as_posix(),
        osm_data_timestamp=osm["source"]["osm_data_timestamp"],
        boundary_reference_year=boundary["source"][
            "source_reference_year"
        ],
        boundary_attribution=boundary["source"]["attribution"],
        boundary_licence=boundary["source"]["licence"],
        boundary_licence_url=boundary["source"]["licence_url"],
        source_licence=osm["source"]["licence"],
        source_licence_url=osm["source"]["licence_url"],
        attribution=osm["source"]["attribution"],
        totals={
            key: (
                sum(row[key] for row in rows)
                if all(row[key] is not None for row in rows)
                else None
            )
            for key in area_columns
        },
        rules=dict(
            commercial_retail=[
                "landuse=commercial", "landuse=retail"
            ],
            residential=["landuse=residential"],
            area=(
                "WGS84 ellipsoidal km2; holes subtract; overlays "
                "use longitude/latitude coordinates"
            ),
            duplicate_area=(
                "Complete same-class parent relation replaces "
                "tagged outer member ways; within-class clipped "
                "areas are unioned"
            ),
            cross_class_overlap=(
                "Reported separately; no priority or subtraction "
                "between commercial and residential classes"
            ),
            geometry=(
                "Validate raw closed-way polygons and native "
                "assembled multipolygons; no Shapely repair, "
                "snapping or simplification"
            ),
            zero=(
                "Zero means no measurable selected mapped area, "
                "not absence of activity"
            ),
        ),
        osm_mapping_completeness_established=False,
        exact_dated_extract_extent_verified=False,
        tourism_features_extracted=False,
        road_features_extracted=False,
        typology_weights_created=False,
        district_demand_ready=False,
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
            measured_values_finite_nonnegative=True,
            within_class_union_applied=True,
            unresolved_sources_retained=True,
            input_files_unchanged=True,
        ),
        interpretation=[
            "Public mapped-land measurements only; these are "
            "allocation proxies, not charging shares or EV counts.",
            "Unsupported nodes, unassembled areas and invalid "
            "geometries remain in the source audit; their areas "
            "are not invented.",
            "Missing measurements remain null if an overlay "
            "operation fails; cross-class overlaps require review.",
            "OSM omissions, source vintage and exact country-extract "
            "coverage remain limitations.",
            "No behaviour parameters, owner readiness, fleet "
            "scenarios or demand simulations are changed.",
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
    (output / "district_landuse_report.json").write_text(
        json.dumps(
            report, indent=2, ensure_ascii=False, allow_nan=False
        ) + "\n",
        encoding="utf-8",
    )
    return output, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--osm", required=True,
        help="Completed OSM snapshot folder",
    )
    args = parser.parse_args()
    output, report = extract(args.osm)

    print("District mapped land-use extraction complete.")
    for key in (
        "status",
        "districts",
        "source_objects",
        "source_status_counts",
        "unresolved_source_objects",
        "operation_errors",
        "district_measurements_complete",
        "districts_with_cross_class_overlap",
        "totals",
    ):
        print(f"{key}: {report[key]}")
    print("typology_weights_created: False")
    print("district_demand_ready: False")
    print(f"Output: {output.relative_to(ROOT)}")
    print(
        "Public mapped-area measurements only; source objects "
        "retained for review; no demand simulations."
    )


if __name__ == "__main__":
    try:
        main()
    except (
        ValueError, OSError, KeyError,
        TypeError, RuntimeError, GEOSException,
    ) as exc:
        raise SystemExit(
            f"District land-use extraction failed: {exc}"
        )