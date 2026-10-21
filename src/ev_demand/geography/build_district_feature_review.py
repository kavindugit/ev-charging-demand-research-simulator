"""Join completed district feature extracts and prepare a spatial review worklist."""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import platform
from statistics import median

ROOT = Path(__file__).resolve().parents[3]

ROAD_CLASSES = (
    "primary",
    "trunk",
    "motorway",
    "primary_link",
    "trunk_link",
    "motorway_link",
)

ROAD_GROUPS = {
    "mapped_primary_trunk_way_km": ROAD_CLASSES[:2],
    "mapped_primary_trunk_motorway_way_km": ROAD_CLASSES[:3],
    "mapped_selected_link_way_km": ROAD_CLASSES[3:],
    "mapped_selected_way_km": ROAD_CLASSES,
}

TOURISM_CATEGORIES = (
    "accommodation",
    "visitor_site",
    "information",
    "artwork",
    "camp_pitch",
    "other_or_unclassified",
)

SPECS = {
    "landuse": (
        "district_landuse_report.json",
        "district_landuse_features_v1",
        "landuse_extracted_for_review",
        {
            "district_landuse_features.csv",
            "landuse_source_audit.csv",
            "landuse_issues.csv",
            "district_landuse_unions.geojsonl.gz",
        },
    ),
    "roads": (
        "district_road_report.json",
        "district_road_features_v1",
        "roads_extracted_for_review",
        {
            "district_road_features.csv",
            "road_class_summary.csv",
            "road_source_audit.csv",
            "road_district_lengths.csv",
            "road_issues.csv",
        },
    ),
    "tourism": (
        "district_tourism_report.json",
        "district_tourism_features_v1",
        "tourism_extracted_for_review",
        {
            "district_tourism_features.csv",
            "tourism_value_counts.csv",
            "tourism_source_audit.csv",
            "tourism_duplicate_review.csv",
            "tourism_issues.csv",
            "tourism_source_geometries.geojsonl.gz",
        },
    ),
    "osm": (
        "osm_snapshot_report.json",
        "osm_country_snapshot_v1",
        "source_inspected",
        {"osm_tag_presence.csv"},
    ),
    "boundary": (
        "district_boundary_report.json",
        "district_boundary_identity_v1",
        "identity_and_coordinate_checks_complete",
        {
            "district_boundaries.geojson",
            "district_boundary_identity_audit.csv",
        },
    ),
}

TABLES = {
    "landuse": "district_landuse_features.csv",
    "roads": "district_road_features.csv",
    "tourism": "district_tourism_features.csv",
}

DEFINITIONS = [
    (
        "landuse",
        "district_reference_area_km2",
        "km2",
        "Area of the supplied district geometry; a reference denominator",
    ),
    (
        "landuse",
        "mapped_commercial_retail_km2",
        "km2",
        "Within-class union of mapped commercial and retail areas",
    ),
    (
        "landuse",
        "mapped_residential_km2",
        "km2",
        "Within-class union of mapped residential areas",
    ),
    (
        "landuse",
        "commercial_residential_overlap_km2",
        "km2",
        "Area common to the two mapped land-use classes; retained for review",
    ),
    (
        "landuse",
        "mapped_selected_land_share_percent",
        "percent",
        (
            "Share of the district covered by the union of these two "
            "selected classes; not OSM mapping completeness"
        ),
    ),
] + [
    (
        "roads",
        f"mapped_{road}_way_km",
        "km",
        f"Mapped highway={road} way length allocated to the district",
    )
    for road in ROAD_CLASSES
] + [
    (
        "roads",
        name,
        "km",
        "Sum of mapped way lengths for: " + ", ".join(classes),
    )
    for name, classes in ROAD_GROUPS.items()
] + [
    (
        "tourism",
        "mapped_tourism_objects_raw",
        "objects",
        (
            "Unreviewed originally tagged OSM objects assigned "
            "unambiguously to a district"
        ),
    ),
] + [
    (
        "tourism",
        f"mapped_{category}_objects_raw",
        "objects",
        (
            "Unreviewed assigned OSM objects in the descriptive "
            f"{category} category"
        ),
    )
    for category in TOURISM_CATEGORIES
]

QUEUE_FIELDS = (
    "review_id",
    "layer",
    "review_topic",
    "osm_keys_json",
    "related_district_ids_json",
    "detail",
    "source_table",
    "review_status",
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def read_csv(path):
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)

        require(
            reader.fieldnames
            and len(reader.fieldnames) == len(set(reader.fieldnames)),
            "Missing or duplicate CSV headers: " + str(path),
        )

        return list(reader)


def write_csv(path, rows, fields=None):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fields or list(rows[0]),
        )
        writer.writeheader()
        writer.writerows(rows)


def number(value, label, integer=False):
    require(
        value is not None
        and str(value).strip() != ""
        and not isinstance(value, bool),
        "Missing numeric value: " + label,
    )

    result = float(value)

    require(
        math.isfinite(result)
        and result >= 0
        and (not integer or result.is_integer()),
        "Invalid numeric value: " + label,
    )

    return int(result) if integer else result


def true_value(value):
    return value is True or value in ("True", "true")


def close(a, b, label):
    require(
        math.isclose(
            float(a),
            float(b),
            rel_tol=1e-9,
            abs_tol=1e-7,
        ),
        "Accounting mismatch: " + label,
    )


def keyed(rows, column, label):
    require(
        all(row.get(column) for row in rows),
        "Missing key in " + label,
    )

    result = {row[column]: row for row in rows}

    require(
        len(result) == len(rows),
        "Duplicate key in " + label,
    )

    return result


def join_districts(identity, tables, reports):
    require(
        len(identity) == 25
        and len({
            row["province_name"]
            for row in identity.values()
        }) == 9,
        "Expected the 25-district, 9-province identity grid.",
    )

    indexed = {
        layer: keyed(rows, "district_id", layer)
        for layer, rows in tables.items()
    }

    for layer, index in indexed.items():
        require(
            set(index) == set(identity),
            "District set mismatch: " + layer,
        )

        for district_id, row in index.items():
            require(
                all(
                    row[name] == identity[district_id][name]
                    for name in ("district_name", "province_name")
                ),
                "District identity mismatch: " + layer,
            )

            require(
                row["data_origin"] == "PUBLIC_REFERENCE",
                "Unexpected feature origin.",
            )

            if layer != "landuse":
                require(
                    true_value(row["measurement_complete"]),
                    "Incomplete district measurements: " + layer,
                )

    joined = []

    for district_id in sorted(identity):
        result = {
            name: identity[district_id][name]
            for name in (
                "district_id",
                "district_name",
                "province_name",
            )
        }

        for layer, column, unit, _ in DEFINITIONS:
            result[column] = number(
                indexed[layer][district_id][column],
                district_id + "/" + column,
                unit == "objects",
            )

        commercial = result["mapped_commercial_retail_km2"]
        residential = result["mapped_residential_km2"]
        overlap = result["commercial_residential_overlap_km2"]
        district_area = result["district_reference_area_km2"]

        require(
            district_area > 0
            and overlap <= min(commercial, residential) + 1e-7,
            "Invalid land-use area relationship.",
        )

        close(
            result["mapped_selected_land_share_percent"],
            100 * (commercial + residential - overlap) / district_area,
            "selected mapped land share",
        )

        for aggregate, classes in ROAD_GROUPS.items():
            close(
                result[aggregate],
                math.fsum(
                    result[f"mapped_{road}_way_km"]
                    for road in classes
                ),
                aggregate,
            )

        require(
            sum(
                result[f"mapped_{category}_objects_raw"]
                for category in TOURISM_CATEGORIES
            ) == result["mapped_tourism_objects_raw"],
            "Tourism district category totals disagree.",
        )

        tourism = indexed["tourism"][district_id]

        for original, output in (
            (
                "objects_with_duplicate_review",
                "tourism_objects_with_duplicate_review",
            ),
            (
                "objects_with_tag_review",
                "tourism_objects_with_tag_review",
            ),
            (
                "legacy_amenity_only_objects_raw",
                "tourism_legacy_amenity_only_objects_raw",
            ),
        ):
            result[output] = number(
                tourism[original],
                output,
                True,
            )

            require(
                result[output] <= result["mapped_tourism_objects_raw"],
                "Tourism review count exceeds assigned source objects.",
            )

        result.update(
            data_origin="PUBLIC_REFERENCE",
            review_status="pending_spatial_proxy_review",
        )

        joined.append(result)

    for layer in ("landuse", "roads"):
        for column, expected in reports[layer]["totals"].items():
            require(
                column in joined[0],
                "Unexpected reported total: " + column,
            )

            close(
                math.fsum(row[column] for row in joined),
                number(expected, column),
                layer + "/" + column,
            )

    require(
        sum(
            row["mapped_tourism_objects_raw"]
            for row in joined
        ) == reports["tourism"]["assigned_source_objects"],
        "Tourism assigned total mismatch.",
    )

    for category in TOURISM_CATEGORIES:
        require(
            sum(
                row[f"mapped_{category}_objects_raw"]
                for row in joined
            ) == reports["tourism"]["assigned_category_counts"][category],
            "Tourism category report mismatch.",
        )

    return joined


def make_statistics(joined):
    result = []

    for layer, column, unit, description in DEFINITIONS:
        values = [row[column] for row in joined]
        low, high = min(values), max(values)

        result.append(
            dict(
                layer=layer,
                feature=column,
                unit=unit,
                districts=len(values),
                total=(
                    None
                    if unit == "percent"
                    else math.fsum(values)
                ),
                minimum=low,
                median=median(values),
                maximum=high,
                zero_districts=sum(value == 0 for value in values),
                constant_across_districts=low == high,
                minimum_district_ids_json=json.dumps(
                    [
                        row["district_id"]
                        for row in joined
                        if row[column] == low
                    ]
                ),
                maximum_district_ids_json=json.dumps(
                    [
                        row["district_id"]
                        for row in joined
                        if row[column] == high
                    ]
                ),
                description=description,
            )
        )

    return result


def build_review(
    landuse_folder,
    roads_folder,
    tourism_folder,
    project_root=ROOT,
):
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

    def load(layer, value):
        folder = (root / value).resolve()

        require(
            folder.is_relative_to(root / "data/processed/reference"),
            "Use processed public-reference inputs.",
        )

        filename, schema, status, outputs = SPECS[layer]
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
            and set(report.get("output_sha256", {})) == outputs,
            "Incomplete upstream manifests: " + filename,
        )

        for name, expected in report["input_sha256"].items():
            watch(name, expected)

        for name, expected in report["output_sha256"].items():
            require(
                Path(name).name == name,
                "Unexpected upstream export name.",
            )
            watch(folder / name, expected)

        if layer in TABLES:
            require(
                report.get("districts") == 25
                and report.get("district_measurements_complete") is True
                and report.get("operation_errors") == 0
                and report.get("typology_weights_created") is False
                and report.get("district_demand_ready") is False,
                "Use completed feature extracts before weights: " + layer,
            )

        return folder, report

    watch("src/ev_demand/geography/build_district_feature_review.py")
    watch("docs/research_pipeline_specification.md")

    folders = {}
    reports = {}

    for layer, value in (
        ("landuse", landuse_folder),
        ("roads", roads_folder),
        ("tourism", tourism_folder),
    ):
        print(
            "Verifying completed " + layer + " extract...",
            flush=True,
        )
        folders[layer], reports[layer] = load(layer, value)

    osm_folders = {
        report["source_osm_folder"]
        for report in reports.values()
    }

    require(
        len(osm_folders) == 1,
        "Feature extracts use different OSM snapshots.",
    )

    osm_folder, osm = load(
        "osm",
        next(iter(osm_folders)),
    )

    boundary_inputs = []

    for report in reports.values():
        paths = [
            name
            for name in report["input_sha256"]
            if Path(name).name == "district_boundaries.geojson"
        ]

        require(
            len(paths) == 1,
            "Missing or ambiguous boundary snapshot.",
        )

        boundary_inputs.append(
            (
                paths[0],
                report["input_sha256"][paths[0]],
            )
        )

    require(
        len(set(boundary_inputs)) == 1,
        "Feature extracts use different boundary snapshots.",
    )

    boundary_path = watch(*boundary_inputs[0])

    _, boundary = load(
        "boundary",
        boundary_path.parent,
    )

    require(
        all(
            report["osm_data_timestamp"]
            == osm["source"]["osm_data_timestamp"]
            and report["boundary_reference_year"]
            == boundary["source"]["source_reference_year"]
            for report in reports.values()
        ),
        "Source vintage metadata disagree.",
    )

    collection = read_json(boundary_path)

    require(
        collection["type"] == "FeatureCollection",
        "Expected a district FeatureCollection.",
    )

    identity = keyed(
        [
            feature["properties"]
            for feature in collection["features"]
        ],
        "district_id",
        "boundary identities",
    )

    tables = {
        layer: read_csv(folders[layer] / name)
        for layer, name in TABLES.items()
    }

    joined = join_districts(identity, tables, reports)
    statistics = make_statistics(joined)

    audits = {
        layer: keyed(
            read_csv(folders[layer] / name),
            "osm_key",
            layer + " source audit",
        )
        for layer, name in (
            ("landuse", "landuse_source_audit.csv"),
            ("roads", "road_source_audit.csv"),
            ("tourism", "tourism_source_audit.csv"),
        )
    }

    for layer, index in audits.items():
        require(
            len(index) == reports[layer]["source_objects"],
            "Source-audit object count mismatch: " + layer,
        )

        require(
            dict(
                Counter(
                    row["geometry_status"]
                    for row in index.values()
                )
            ) == reports[layer]["source_status_counts"],
            "Source geometry-status mismatch: " + layer,
        )

    tourism = audits["tourism"]

    require(
        dict(
            Counter(
                row["assignment_status"]
                for row in tourism.values()
            )
        ) == reports["tourism"]["assignment_status_counts"],
        "Tourism assignment accounting mismatch.",
    )

    require(
        sum(
            row["assignment_status"] != "assigned"
            for row in tourism.values()
        ) == reports["tourism"]["unassigned_source_objects"],
        "Tourism unassigned count mismatch.",
    )

    require(
        sum(
            bool(json.loads(row["tag_review_flags_json"]))
            for row in tourism.values()
        ) == reports["tourism"]["objects_with_tag_review"],
        "Tourism tag-review count mismatch.",
    )

    for row in joined:
        assigned = [
            source
            for source in tourism.values()
            if source["assignment_status"] == "assigned"
            and source["district_id"] == row["district_id"]
        ]

        require(
            len(assigned) == row["mapped_tourism_objects_raw"],
            "Tourism source/district count mismatch.",
        )

        require(
            all(
                sum(
                    source["category"] == category
                    for source in assigned
                ) == row[f"mapped_{category}_objects_raw"]
                for category in TOURISM_CATEGORIES
            ),
            "Tourism source/district categories disagree.",
        )

    pairs = read_csv(
        folders["tourism"] / "tourism_duplicate_review.csv"
    )

    pair_counts = Counter()
    seen_pairs = set()

    for pair in pairs:
        a = pair["osm_key_a"]
        b = pair["osm_key_b"]
        key = tuple(sorted((a, b)))

        require(
            a != b
            and a in tourism
            and b in tourism
            and key not in seen_pairs,
            "Invalid tourism duplicate-review pair.",
        )

        require(
            pair["review_status"] == "unreviewed"
            and pair["automatically_merged"] in {"False", "false"},
            "Expected unmerged tourism review candidates.",
        )

        require(
            pair["district_a"] == tourism[a]["district_id"]
            and pair["district_b"] == tourism[b]["district_id"],
            "Tourism pair district links disagree.",
        )

        seen_pairs.add(key)
        pair_counts.update((a, b))

    require(
        len(pairs) == reports["tourism"]["potential_duplicate_pairs"],
        "Tourism pair total mismatch.",
    )

    require(
        len(pair_counts)
        == reports["tourism"]["objects_with_duplicate_review"],
        "Tourism duplicate-object total mismatch.",
    )

    require(
        all(
            number(
                row["duplicate_review_pairs"],
                key,
                True,
            ) == pair_counts[key]
            for key, row in tourism.items()
        ),
        "Tourism source/pair links disagree.",
    )

    queue = []

    def add(layer, topic, keys, districts, detail, table):
        related = sorted(set(d for d in districts if d))

        require(
            set(related) <= set(identity),
            "Review item refers to an unknown district.",
        )

        queue.append(
            dict(
                layer=layer,
                review_topic=topic,
                osm_keys_json=json.dumps(keys),
                related_district_ids_json=json.dumps(related),
                detail=detail,
                source_table=(
                    folders[layer] / table
                ).relative_to(root).as_posix(),
                review_status="pending",
            )
        )

    for layer, table in (
        ("landuse", "landuse_issues.csv"),
        ("roads", "road_issues.csv"),
        ("tourism", "tourism_issues.csv"),
    ):
        for issue in read_csv(folders[layer] / table):
            key = issue["osm_key"]

            require(
                key in audits[layer],
                "Issue/source-object link mismatch: " + layer,
            )

            source = audits[layer][key]

            related = json.loads(
                source.get("intersecting_district_ids_json", "[]")
            )

            if source.get("district_id"):
                related.append(source["district_id"])

            add(
                layer,
                issue["issue"],
                [key],
                related,
                issue["detail"],
                table,
            )

    for row in joined:
        if row["commercial_residential_overlap_km2"] > 0:
            add(
                "landuse",
                "commercial_residential_overlap",
                [],
                [row["district_id"]],
                json.dumps(
                    {
                        "overlap_km2": row[
                            "commercial_residential_overlap_km2"
                        ]
                    }
                ),
                TABLES["landuse"],
            )

    valid_roads = [
        row
        for row in audits["roads"].values()
        if row["geometry_status"] == "valid_line"
    ]

    outside = [
        row
        for row in valid_roads
        if number(
            row["outside_district_layer_way_km"],
            row["osm_key"],
        ) > 0
    ]

    require(
        len(outside)
        == reports["roads"]["ways_with_outside_district_length"],
        "Outside-road count mismatch.",
    )

    for row in outside:
        add(
            "roads",
            "length_outside_district_layer",
            [row["osm_key"]],
            json.loads(row["intersecting_district_ids_json"]),
            json.dumps(
                {
                    "outside_way_km": number(
                        row["outside_district_layer_way_km"],
                        row["osm_key"],
                    )
                }
            ),
            "road_source_audit.csv",
        )

    for pair in pairs:
        add(
            "tourism",
            "potential_duplicate_representation",
            [pair["osm_key_a"], pair["osm_key_b"]],
            [pair["district_a"], pair["district_b"]],
            json.dumps(
                {
                    "name_a": pair["name_a"],
                    "name_b": pair["name_b"],
                    "reasons": json.loads(pair["reasons_json"]),
                },
                ensure_ascii=False,
            ),
            "tourism_duplicate_review.csv",
        )

    queue.sort(
        key=lambda row: (
            row["layer"],
            row["review_topic"],
            row["osm_keys_json"],
            row["related_district_ids_json"],
        )
    )

    queue = [
        dict(
            review_id=f"SPATIAL-{i:04d}",
            **row,
        )
        for i, row in enumerate(queue, 1)
    ]

    for row in joined:
        row["review_items_related_to_district"] = sum(
            row["district_id"]
            in json.loads(item["related_district_ids_json"])
            for item in queue
        )

    require(
        all(
            digest(path) == expected
            for path, expected in watched.items()
        ),
        "Source changed during review assembly.",
    )

    now = datetime.now(timezone.utc)

    output = (
        root
        / "data/processed/reference"
        / (
            "district_feature_review_"
            + now.strftime("%Y%m%dT%H%M%S_%fZ")
        )
    )

    output.mkdir(parents=True, exist_ok=False)

    write_csv(
        output / "district_feature_review.csv",
        joined,
    )

    write_csv(
        output / "feature_statistics.csv",
        statistics,
    )

    write_csv(
        output / "feature_definitions.csv",
        [
            dict(
                layer=layer,
                feature=column,
                unit=unit,
                description=description,
                source_table=(
                    folders[layer] / TABLES[layer]
                ).relative_to(root).as_posix(),
            )
            for layer, column, unit, description in DEFINITIONS
        ],
    )

    write_csv(
        output / "spatial_review_worklist.csv",
        queue,
        QUEUE_FIELDS,
    )

    report = dict(
        schema_version="district_feature_review_v1",
        status="feature_review_bundle_prepared",
        data_origin="PUBLIC_REFERENCE",
        created_utc=now.isoformat(),
        districts=25,
        feature_columns=len(DEFINITIONS),
        source_layers=3,
        osm_data_timestamp=osm["source"]["osm_data_timestamp"],
        boundary_reference_year=boundary["source"]["source_reference_year"],
        source_folders={
            layer: folder.relative_to(root).as_posix()
            for layer, folder in folders.items()
        },
        source_osm_folder=osm_folder.relative_to(root).as_posix(),
        headline_totals={
            column: math.fsum(row[column] for row in joined)
            for column in (
                "mapped_commercial_retail_km2",
                "mapped_residential_km2",
                "mapped_primary_trunk_way_km",
                "mapped_tourism_objects_raw",
            )
        },
        review_worklist_items=len(queue),
        worklist_items_by_layer=dict(
            Counter(item["layer"] for item in queue)
        ),
        worklist_items_by_topic=dict(
            Counter(item["review_topic"] for item in queue)
        ),
        unassigned_tourism_source_objects=reports["tourism"][
            "unassigned_source_objects"
        ],
        potential_tourism_duplicate_pairs=len(pairs),
        road_length_outside_district_layer_km=math.fsum(
            number(
                row["outside_district_layer_way_km"],
                row["osm_key"],
            )
            for row in valid_roads
        ),
        source_licence=osm["source"]["licence"],
        source_licence_url=osm["source"]["licence_url"],
        attribution=osm["source"]["attribution"],
        boundary_licence=boundary["source"]["licence"],
        boundary_licence_url=boundary["source"]["licence_url"],
        boundary_attribution=boundary["source"]["attribution"],
        feature_layers_assembled=True,
        spatial_proxy_review_complete=False,
        normalization_applied=False,
        typology_weights_created=False,
        unique_tourism_venue_counts_established=False,
        district_demand_ready=False,
        new_simulations=0,
        pending_method_decisions=[
            (
                "Review tourism identities, tags and unassigned "
                "objects before choosing a tourism proxy"
            ),
            "Review land-use overlaps and selected mapping coverage",
            "Choose the road-class scope explicitly",
            (
                "Define geographic proxy normalisation and its "
                "relation to charging-place allocation and fleet scenarios"
            ),
        ],
        interpretation=[
            (
                "Feature quantities retain their original units; "
                "they have not been added together or converted "
                "into EV shares."
            ),
            (
                "Tourism counts retain potential duplicate source "
                "IDs and descriptive information/artwork/pitch categories."
            ),
            (
                "Selected land share is not an OSM mapping-completeness "
                "score; way length is not traffic volume."
            ),
            (
                "Worklist rows can refer to the same source object "
                "more than once; related districts are review links, "
                "not allocations."
            ),
            (
                "This review retains earlier inputs and outputs; "
                "no geometry, count, eligibility or parameter "
                "decisions are changed."
            ),
        ],
        software={
            "python": platform.python_version(),
        },
        checks=dict(
            upstream_hashes_verified=True,
            common_source_snapshot_and_boundaries=True,
            district_identity_and_complete_grid_verified=True,
            finite_nonnegative_features=True,
            feature_totals_reproduced=True,
            source_and_tourism_pair_links_verified=True,
            input_files_unchanged=True,
        ),
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
        "Source changed during review export.",
    )

    (output / "district_feature_review_report.json").write_text(
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

    parser.add_argument("--landuse", required=True)
    parser.add_argument("--roads", required=True)
    parser.add_argument("--tourism", required=True)

    args = parser.parse_args()

    output, report = build_review(
        args.landuse,
        args.roads,
        args.tourism,
    )

    print("District feature review bundle complete.")

    for key in (
        "status",
        "districts",
        "source_layers",
        "feature_columns",
        "headline_totals",
        "review_worklist_items",
        "worklist_items_by_layer",
        "unassigned_tourism_source_objects",
        "potential_tourism_duplicate_pairs",
        "road_length_outside_district_layer_km",
    ):
        print(f"{key}: {report[key]}")

    print(
        "Shared-source, district-identity, source-link "
        "and feature-accounting checks passed."
    )

    print("normalization_applied: False")
    print("typology_weights_created: False")
    print("district_demand_ready: False")
    print(f"Output: {output.relative_to(ROOT)}")

    print(
        "Review assembly only; no source changes, "
        "allocation weights or new demand simulations."
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
    ) as exc:
        raise SystemExit(f"District feature review failed: {exc}")