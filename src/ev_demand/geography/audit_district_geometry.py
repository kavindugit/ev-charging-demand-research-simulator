"""Audit district topology and WGS84 measurements without changing source geometry."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
from itertools import combinations
import json
import math
from pathlib import Path
import platform
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
from ev_demand.geography.build_district_registry import (
    read_reference, require, sha256, write_csv,
)
from ev_demand.geography.prepare_district_boundaries import (
    check_metadata, link_features,
)

try:
    import numpy as np
    import pyproj
    import shapely
    from shapely.errors import GEOSException
    from shapely.geometry import mapping, shape
    from shapely.geometry.polygon import orient
except ImportError as exc:
    raise SystemExit(
        'Install dependencies: python -m pip install '
        '"shapely==2.1.2" "pyproj==3.7.2"'
    ) from exc

GEOD = pyproj.Geod(ellps="WGS84")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def area_km2(geometry):
    """Orient analysis copies so holes subtract; preserve stored coordinates."""
    if geometry.is_empty:
        return 0.0

    if geometry.geom_type == "Polygon":
        signed_area, _ = GEOD.geometry_area_perimeter(
            orient(geometry, sign=1.0)
        )
        value = abs(signed_area) / 1_000_000.0
    elif hasattr(geometry, "geoms"):
        value = sum(area_km2(part) for part in geometry.geoms)
    else:
        value = 0.0

    require(math.isfinite(value), "Nonfinite geodesic area.")
    return value


def analyse(features):
    geometries = [shape(feature["geometry"]) for feature in features]
    valid = [
        not geom.is_empty and geom.is_valid and geom.area > 0
        for geom in geometries
    ]
    rows, pairs, issues = [], [], []

    def issue(kind, properties, geometry):
        issues.append(dict(
            type="Feature",
            properties=dict(issue=kind, **properties),
            geometry=mapping(geometry),
        ))

    for feature, geom, okay in zip(features, geometries, valid):
        props = feature["properties"]
        error, area, length = "", None, None

        if okay:
            try:
                area = area_km2(geom)
                length = GEOD.geometry_length(geom.boundary) / 1000.0
                require(
                    area > 0 and math.isfinite(length) and length > 0,
                    "Nonpositive or nonfinite geometry measurement.",
                )
            except (ValueError, RuntimeError) as exc:
                error = str(exc)
                area, length = None, None
        else:
            issue(
                "invalid_district_geometry",
                dict(
                    district_id=props["district_id"],
                    district_name=props["district_name"],
                ),
                geom,
            )

        rows.append(dict(
            district_id=props["district_id"],
            district_name=props["district_name"],
            province_name=props["province_name"],
            source_shape_id=props["shapeID"],
            geometry_type=geom.geom_type,
            is_empty=geom.is_empty,
            geometry_valid=okay,
            validity_reason=shapely.is_valid_reason(geom),
            area_km2=area,
            all_ring_boundary_km=length,
            measurement_error=error,
            coverage_invalid_edge=None,
        ))

    coverage, coverage_error = None, ""

    if all(valid):
        try:
            coverage = bool(
                shapely.coverage_is_valid(geometries, gap_width=0.0)
            )
            edges = shapely.coverage_invalid_edges(
                geometries, gap_width=0.0
            )
            for row, edge in zip(rows, edges):
                row["coverage_invalid_edge"] = not edge.is_empty
                if not edge.is_empty:
                    issue(
                        "coverage_invalid_edge",
                        dict(
                            district_id=row["district_id"],
                            district_name=row["district_name"],
                        ),
                        edge,
                    )
        except GEOSException as exc:
            coverage = None
            coverage_error = str(exc)

    for i, j in combinations(range(len(geometries)), 2):
        left, right = geometries[i], geometries[j]
        pair = dict(
            district_a_id=rows[i]["district_id"],
            district_a_name=rows[i]["district_name"],
            district_b_id=rows[j]["district_id"],
            district_b_name=rows[j]["district_name"],
            status="not_evaluable_invalid_geometry",
            intersection_type="",
            positive_area_overlap=None,
            overlap_area_km2=None,
            error="",
        )

        if valid[i] and valid[j]:
            try:
                intersection = left.intersection(right)

                # Cartesian area detects two-dimensional overlap only.
                # Reported areas use ellipsoidal square kilometres.
                overlap = (
                    not intersection.is_empty and intersection.area > 0
                )
                pair.update(
                    status=(
                        "positive_area_overlap" if overlap else
                        "disjoint" if intersection.is_empty else
                        "boundary_contact_only"
                    ),
                    intersection_type=intersection.geom_type,
                    positive_area_overlap=overlap,
                    overlap_area_km2=(
                        area_km2(intersection) if overlap else 0.0
                    ),
                )

                if overlap:
                    issue(
                        "positive_area_overlap",
                        dict(
                            district_a_id=pair["district_a_id"],
                            district_b_id=pair["district_b_id"],
                        ),
                        intersection,
                    )
            except (GEOSException, ValueError, RuntimeError) as exc:
                pair.update(
                    status="operation_error",
                    error=str(exc),
                    positive_area_overlap=None,
                    overlap_area_km2=None,
                )

        pairs.append(pair)

    evaluated = sum(
        pair["positive_area_overlap"] is not None for pair in pairs
    )
    overlaps = sum(
        pair["positive_area_overlap"] is True for pair in pairs
    )
    measurements_ok = all(row["area_km2"] is not None for row in rows)
    no_overlaps = overlaps == 0 if evaluated == len(pairs) else None

    passed = (
        all(valid)
        and measurements_ok
        and coverage is True
        and no_overlaps is True
    )

    findings = dict(
        districts=len(rows),
        valid_district_geometries=sum(valid),
        measurements_completed=sum(
            row["area_km2"] is not None for row in rows
        ),
        district_pairs=len(pairs),
        evaluated_pairs=evaluated,
        positive_area_overlap_pairs=overlaps,
        no_positive_area_overlaps=no_overlaps,
        coverage_valid=coverage,
        coverage_error=coverage_error,
        districts_with_invalid_coverage_edges=sum(
            row["coverage_invalid_edge"] is True for row in rows
        ),
        sum_district_area_km2=(
            sum(row["area_km2"] for row in rows)
            if measurements_ok else None
        ),
        geometry_checks_passed=passed,
    )
    return rows, pairs, issues, findings


def audit_boundaries(boundaries_folder, project_root=ROOT):
    require(
        shapely.__version__ == "2.1.2"
        and pyproj.__version__ == "3.7.2",
        "Use the tested pins: shapely==2.1.2 and pyproj==3.7.2.",
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
        digest = sha256(path)
        require(
            (expected is None or digest == expected)
            and (path not in watched or watched[path] == digest),
            "Input hash mismatch: " + str(value),
        )
        watched[path] = digest
        return path

    for name in (
        "src/ev_demand/geography/audit_district_geometry.py",
        "src/ev_demand/geography/build_district_registry.py",
        "src/ev_demand/geography/prepare_district_boundaries.py",
        "docs/research_pipeline_specification.md",
    ):
        watch(name)

    folder = (root / boundaries_folder).resolve()
    require(
        folder.is_relative_to(root / "data/processed/reference"),
        "Use a public boundary output folder.",
    )
    report = read_json(watch(folder / "district_boundary_report.json"))

    require(
        report.get("schema_version") == "district_boundary_identity_v1"
        and report.get("status")
        == "identity_and_coordinate_checks_complete"
        and report.get("data_origin") == "PUBLIC_REFERENCE"
        and report.get("districts") == 25
        and report.get("provinces") == 9
        and report.get("checks")
        and all(value is True for value in report["checks"].values()),
        "Unexpected boundary identity report.",
    )
    require(
        set(report["output_sha256"]) == {
            "district_boundaries.geojson",
            "district_boundary_identity_audit.csv",
        },
        "Incomplete boundary exports.",
    )

    for name, digest in report["input_sha256"].items():
        watch(name, digest)
    for name, digest in report["output_sha256"].items():
        require(Path(name).name == name, "Unexpected boundary output name.")
        watch(folder / name, digest)

    registry = (root / report["registry_folder"]).resolve()
    require(
        registry.is_relative_to(root / "data/processed/reference"),
        "Unexpected registry path.",
    )
    reg_report = read_json(
        watch(registry / "district_registry_report.json")
    )
    require(
        reg_report.get("schema_version") == "district_registry_v1"
        and reg_report.get("status") == "complete"
        and reg_report.get("registry_origin") == "PUBLIC_REFERENCE"
        and reg_report.get("checks")
        and all(
            value is True for value in reg_report["checks"].values()
        ),
        "Unexpected district registry.",
    )

    for name, digest in reg_report["input_sha256"].items():
        watch(name, digest)
    for name, digest in reg_report["output_sha256"].items():
        require(Path(name).name == name, "Unexpected registry output name.")
        watch(registry / name, digest)

    districts = read_csv(watch(registry / "district_registry.csv"))
    reproduced, _ = read_reference(
        watch(
            reg_report["source"]["snapshot"],
            reg_report["source"]["sha256"],
        )
    )
    require(
        districts == reproduced,
        "Registry differs from its DCS source snapshot.",
    )

    source = report["source"]
    metadata = read_json(
        watch(source["metadata_snapshot"], source["metadata_sha256"])
    )
    check_metadata(metadata)
    require(
        source["metadata"] == metadata
        and source["boundary_id"] == metadata["boundaryID"]
        and source["source_reference_year"]
        == metadata["boundaryYearRepresented"],
        "Boundary metadata disagreement.",
    )

    raw = read_json(
        watch(source["geometry_snapshot"], source["geometry_sha256"])
    )
    reproduced_layer, identity = link_features(raw, districts)
    layer = read_json(folder / "district_boundaries.geojson")

    require(
        layer == reproduced_layer,
        "Prepared boundaries differ from the linked raw snapshot.",
    )
    require(
        read_csv(folder / "district_boundary_identity_audit.csv")
        == [
            {key: str(value) for key, value in row.items()}
            for row in identity
        ],
        "Identity audit does not reproduce the source geometry.",
    )

    print(
        "Checking 25 district geometries and 300 district pairs...",
        flush=True,
    )
    rows, pairs, issues, findings = analyse(layer["features"])
    require(
        len(rows) == 25 and len(pairs) == 300,
        "Incomplete district/pair audit grid.",
    )
    require(
        all(sha256(path) == digest for path, digest in watched.items()),
        "Input changed during geometry audit.",
    )

    now = datetime.now(timezone.utc)
    output = (
        root / "data/processed/reference"
        / ("district_geometry_audit_"
           + now.strftime("%Y%m%dT%H%M%S_%fZ"))
    )
    output.mkdir(parents=True, exist_ok=False)

    write_csv(output / "district_geometry_audit.csv", rows)
    write_csv(output / "district_pair_audit.csv", pairs)
    (output / "district_geometry_issues.geojson").write_text(
        json.dumps(
            dict(type="FeatureCollection", features=issues),
            ensure_ascii=False,
            allow_nan=False,
        ) + "\n",
        encoding="utf-8",
    )

    result = dict(
        schema_version="district_geometry_audit_v1",
        status=(
            "geometry_checks_passed"
            if findings["geometry_checks_passed"]
            else "review_required"
        ),
        data_origin="PUBLIC_REFERENCE",
        created_utc=now.isoformat(),
        boundary_folder=folder.relative_to(root).as_posix(),
        source_boundary_id=source["boundary_id"],
        boundary_reference_year=source["source_reference_year"],
        source_attribution=source["attribution"],
        source_licence=source["licence"],
        source_licence_url=source["licence_url"],
        findings=findings,
        area_measured=findings["measurements_completed"] == 25,
        gap_completeness_assessed=False,
        legal_boundary_accuracy_established=False,
        district_demand_ready=False,
        source_geometry_changed=False,
        methods=dict(
            topology=(
                "GEOS planar predicates on the original "
                "longitude/latitude coordinates"
            ),
            coverage=(
                "Exact shared-edge / non-overlap check; "
                "coverage gap_width=0.0 degrees"
            ),
            overlaps=(
                "All 300 pairs; any positive two-dimensional "
                "intersection is reported, with no area tolerance"
            ),
            area=(
                "WGS84 ellipsoid; orient analysis copies CCW exterior / "
                "CW holes; absolute net area per polygon in km2"
            ),
            boundary_length=(
                "WGS84 ellipsoid; sum all exterior and interior "
                "ring lengths in km"
            ),
            repair=(
                "None; no snapping, simplification, clipping or buffer(0)"
            ),
        ),
        software=dict(
            python=platform.python_version(),
            numpy=np.__version__,
            shapely=shapely.__version__,
            geos=shapely.geos_version_string,
            pyproj=pyproj.__version__,
            proj=pyproj.proj_version_str,
        ),
        checks=dict(
            source_hashes_verified=True,
            DCS_registry_reproduced=True,
            boundary_and_identity_exports_reproduced=True,
            complete_25_district_300_pair_grid=True,
            input_files_unchanged=True,
        ),
        interpretation=[
            "A completed audit can report review_required; geometry "
            "findings are exported rather than repaired.",
            "Coverage validity permits holes; no coastline, water-body "
            "or missing-land completeness test is claimed.",
            "Areas describe this 2022 reference geometry, including "
            "whatever inland water it contains; they are not official "
            "land-area statistics.",
            "A geometry pass does not establish district fleet weights, "
            "charging-location allocation or demand readiness.",
            "Registry, owner parameters, eligibility, source inventory "
            "and existing simulations remain unchanged.",
        ],
        input_sha256={
            path.relative_to(root).as_posix(): digest
            for path, digest in watched.items()
        },
        output_sha256={
            path.name: sha256(path)
            for path in sorted(output.iterdir())
        },
    )

    require(
        all(sha256(path) == digest for path, digest in watched.items()),
        "Input changed during audit export.",
    )
    (output / "district_geometry_report.json").write_text(
        json.dumps(
            result, indent=2, ensure_ascii=False, allow_nan=False
        ) + "\n",
        encoding="utf-8",
    )
    return output, result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--boundaries", required=True)
    args = parser.parse_args()

    output, report = audit_boundaries(args.boundaries)

    print("District geometry and measurement audit complete.")
    print(f"status: {report['status']}")
    for key in (
        "districts",
        "valid_district_geometries",
        "evaluated_pairs",
        "positive_area_overlap_pairs",
        "coverage_valid",
        "districts_with_invalid_coverage_edges",
        "sum_district_area_km2",
    ):
        print(f"{key}: {report['findings'][key]}")
    print("gap_completeness_assessed: False")
    print("district_demand_ready: False")
    print(
        "Source, DCS-link, export-reproduction and "
        "input-integrity checks passed."
    )
    print(f"Output: {output.relative_to(ROOT)}")
    print(
        "Public geometry audit only; source geometry unchanged; "
        "no demand simulations."
    )


if __name__ == "__main__":
    try:
        main()
    except (
        ValueError, OSError, KeyError, TypeError,
        RuntimeError, GEOSException,
    ) as exc:
        print(f"District geometry audit failed: {exc}", file=sys.stderr)
        raise SystemExit(1)