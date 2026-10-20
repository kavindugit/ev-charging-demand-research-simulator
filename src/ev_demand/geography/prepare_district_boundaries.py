"""Acquire a versioned public district layer and audit its DCS identity links."""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sys
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from ev_demand.geography.build_district_registry import (
    make_aliases,
    normalize_name,
    read_reference,
    require,
    sha256,
    write_csv,
)

API_URL = "https://www.geoboundaries.org/api/current/gbHumanitarian/LKA/ADM2/"
UPSTREAM_URL = "https://data.humdata.org/dataset/cod-ab-lka"

BOUNDARY_ID = "LKA-ADM2-33603344"
COMMIT = "9469f09592ced973a3448cf66b6100b741b64c0d"
RELATIVE_FILE = (
    "releaseData/gbHumanitarian/LKA/ADM2/"
    "geoBoundaries-LKA-ADM2.geojson"
)
DOWNLOAD_URL = (
    f"https://github.com/wmgeolab/geoBoundaries/raw/"
    f"{COMMIT}/{RELATIVE_FILE}"
)

LICENSE = (
    "Creative Commons Attribution 3.0 Intergovernmental Organisations "
    "(CC BY 3.0 IGO)"
)
MAX_GEOJSON = 64 * 1024 * 1024

CRS_NAMES = {
    "urn:ogc:def:crs:OGC:1.3:CRS84",
    "urn:ogc:def:crs:OGC::CRS84",
    "EPSG:4326",
}


def download(url, limit):
    require(urlparse(url).scheme == "https", "Use HTTPS sources.")

    request = Request(
        url,
        headers={"User-Agent": "EVChargingResearch/1.0"},
    )

    with urlopen(request, timeout=30) as response:
        final_url = response.geturl()
        parsed = urlparse(final_url)

        require(
            parsed.scheme == "https"
            and parsed.hostname in {
                "www.geoboundaries.org",
                "geoboundaries.org",
                "github.com",
                "raw.githubusercontent.com",
                "media.githubusercontent.com",
            },
            "Unexpected source redirect.",
        )

        payload = response.read(limit + 1)

    require(
        0 < len(payload) <= limit,
        "Empty or oversized source download.",
    )
    return payload, final_url


def check_metadata(metadata):
    require(
        metadata.get("boundaryID") == BOUNDARY_ID
        and metadata.get("boundaryISO") == "LKA"
        and metadata.get("boundaryType") == "ADM2"
        and metadata.get("admUnitCount") == "25"
        and metadata.get("boundaryYearRepresented") == "2022"
        and metadata.get("boundaryLicense") == LICENSE
        and "Survey Department" in metadata.get("boundarySource", "")
        and "HDX" in metadata.get("boundarySource", ""),
        "Selected boundary revision/source/licence changed; "
        "review the provider metadata.",
    )

    url = urlparse(metadata["gjDownloadURL"])
    pattern = (
        r"/wmgeolab/geoBoundaries/raw/(?:"
        + COMMIT
        + "|"
        + COMMIT[:7]
        + r")/"
        + re.escape(RELATIVE_FILE)
    )

    require(
        url.scheme == "https"
        and url.hostname == "github.com"
        and not url.query
        and not url.fragment
        and re.fullmatch(pattern, url.path) is not None,
        "Unexpected full-resolution source URL or revision.",
    )


def coordinate_audit(geometry):
    require(
        isinstance(geometry, dict)
        and geometry.get("type") in {"Polygon", "MultiPolygon"},
        "Expected nonempty Polygon/MultiPolygon district geometry.",
    )

    polygons = (
        [geometry.get("coordinates")]
        if geometry["type"] == "Polygon"
        else geometry.get("coordinates")
    )

    require(
        isinstance(polygons, list) and polygons,
        "Empty district geometry.",
    )

    count = 0
    rings = 0
    bounds = [math.inf, math.inf, -math.inf, -math.inf]

    for polygon in polygons:
        require(
            isinstance(polygon, list) and polygon,
            "Empty polygon.",
        )

        for ring in polygon:
            require(
                isinstance(ring, list)
                and len(ring) >= 4
                and ring[0] == ring[-1],
                "Open or undersized polygon ring.",
            )

            for point in ring:
                require(
                    isinstance(point, list)
                    and len(point) == 2
                    and all(
                        type(value) in (int, float)
                        and math.isfinite(value)
                        for value in point
                    ),
                    "Invalid 2D coordinate.",
                )

                lon, lat = point
                require(
                    78 <= lon <= 83 and 5 <= lat <= 11,
                    "Coordinates outside broad Sri Lanka plausibility bounds.",
                )

                bounds = [
                    min(bounds[0], lon),
                    min(bounds[1], lat),
                    max(bounds[2], lon),
                    max(bounds[3], lat),
                ]
                count += 1

            require(
                len({tuple(point) for point in ring}) >= 3,
                "Degenerate polygon ring.",
            )
            rings += 1

    return {
        "polygon_parts": len(polygons),
        "rings": rings,
        "coordinate_positions": count,
        "bbox_min_lon": bounds[0],
        "bbox_min_lat": bounds[1],
        "bbox_max_lon": bounds[2],
        "bbox_max_lat": bounds[3],
    }


def link_features(collection, districts):
    require(
        collection.get("type") == "FeatureCollection"
        and len(collection.get("features", [])) == 25,
        "Expected 25 district features.",
    )

    crs = collection.get("crs")
    require(
        crs is None
        or (
            crs.get("type") == "name"
            and crs.get("properties", {}).get("name") in CRS_NAMES
        ),
        "Unexpected source coordinate reference system.",
    )

    aliases, _ = make_aliases()
    registry = {row["district_id"]: row for row in districts}

    require(
        len(registry) == len(districts) == 25,
        "Invalid DCS district registry.",
    )

    seen_shapes = set()
    seen_districts = set()
    features = []
    rows = []

    for feature in collection["features"]:
        require(
            feature.get("type") == "Feature",
            "Invalid source feature.",
        )

        properties = feature["properties"]
        shape_id = properties.get("shapeID")
        name = properties.get("shapeName")

        require(
            isinstance(shape_id, str)
            and shape_id
            and shape_id not in seen_shapes
            and isinstance(name, str)
            and properties.get("shapeGroup") == "LKA"
            and properties.get("shapeType") == "ADM2",
            "Missing, duplicate or unexpected feature identity.",
        )

        code = aliases.get(normalize_name(name))
        district_id = "DCS-" + code if code else ""

        require(
            district_id in registry
            and district_id not in seen_districts,
            "Unmatched or duplicate district name: " + name,
        )

        district = registry[district_id]
        info = coordinate_audit(feature["geometry"])
        source_properties = dict(properties)

        require(
            not (
                {
                    "district_id",
                    "district_name",
                    "province_name",
                    "data_origin",
                    "boundary_source_id",
                }
                & source_properties.keys()
            ),
            "Source properties collide with project fields.",
        )

        source_properties.update(
            district_id=district_id,
            district_name=district["district_name"],
            province_name=district["province_name"],
            data_origin="PUBLIC_REFERENCE",
            boundary_source_id=BOUNDARY_ID,
        )

        features.append({
            "type": "Feature",
            "properties": source_properties,
            "geometry": feature["geometry"],
        })

        rows.append({
            "district_id": district_id,
            "district_name": district["district_name"],
            "province_name": district["province_name"],
            "source_shape_id": shape_id,
            "source_district_name": name,
            "match_status": (
                "canonical"
                if normalize_name(name)
                == normalize_name(district["district_name"])
                else "declared_alias"
            ),
            "geometry_type": feature["geometry"]["type"],
            **info,
        })

        seen_shapes.add(shape_id)
        seen_districts.add(district_id)

    require(
        seen_districts == set(registry),
        "Incomplete district coverage.",
    )

    features.sort(
        key=lambda feature: feature["properties"]["district_id"]
    )
    rows.sort(key=lambda row: row["district_id"])

    return {
        "type": "FeatureCollection",
        "features": features,
    }, rows


def prepare(
    registry_folder,
    metadata_json=None,
    source_geojson=None,
    project_root=ROOT,
):
    root = Path(project_root).resolve()
    watched = {}

    require(
        (metadata_json is None) == (source_geojson is None),
        "Supply both local source files, or neither.",
    )

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
        "docs/research_pipeline_specification.md",
        "src/ev_demand/geography/build_district_registry.py",
        "src/ev_demand/geography/prepare_district_boundaries.py",
    ):
        watch(name)

    registry = (root / registry_folder).resolve()

    require(
        registry.is_relative_to(root / "data/processed/reference"),
        "Use a completed public district registry.",
    )

    report = json.loads(
        watch(
            registry / "district_registry_report.json"
        ).read_text(encoding="utf-8-sig")
    )

    require(
        report.get("schema_version") == "district_registry_v1"
        and report.get("status") == "complete"
        and report.get("registry_origin") == "PUBLIC_REFERENCE"
        and report.get("checks")
        and all(
            value is True
            for value in report["checks"].values()
        ),
        "Unexpected district-registry report.",
    )

    for name, digest in report["output_sha256"].items():
        watch(registry / name, digest)

    for name, digest in report["input_sha256"].items():
        watch(name, digest)

    with (
        registry / "district_registry.csv"
    ).open(encoding="utf-8-sig", newline="") as handle:
        districts = list(csv.DictReader(handle))

    source = watch(
        report["source"]["snapshot"],
        report["source"]["sha256"],
    )
    reproduced, _ = read_reference(source)

    require(
        districts == reproduced,
        "District registry does not reproduce its DCS source.",
    )

    if metadata_json is None:
        print("Downloading boundary source metadata...", flush=True)
        metadata_bytes, metadata_final = download(
            API_URL,
            1024 * 1024,
        )
        acquisition = "downloaded_from_geoboundaries_provider"
    else:
        meta_path = watch(metadata_json)
        geo_path = watch(source_geojson)

        require(
            meta_path.stat().st_size <= 1024 * 1024
            and geo_path.stat().st_size <= MAX_GEOJSON,
            "Local source exceeds size limit.",
        )

        metadata_bytes = meta_path.read_bytes()
        metadata_final = None
        acquisition = (
            "local_copy; upstream identity declared, "
            "not live authenticated"
        )

    metadata = json.loads(metadata_bytes)
    check_metadata(metadata)

    if source_geojson is None:
        print(
            "Downloading full-resolution district GeoJSON "
            "(approximately 16 MB)...",
            flush=True,
        )
        geometry_bytes, geometry_final = download(
            DOWNLOAD_URL,
            MAX_GEOJSON,
        )
    else:
        geometry_bytes = geo_path.read_bytes()
        geometry_final = None

    require(
        0 < len(geometry_bytes) <= MAX_GEOJSON,
        "Empty or oversized district GeoJSON.",
    )

    collection = json.loads(geometry_bytes)
    prepared, rows = link_features(collection, districts)

    require(
        all(
            sha256(path) == digest
            for path, digest in watched.items()
        ),
        "Input changed during boundary preparation.",
    )

    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%S_%fZ")

    raw = (
        root
        / "data/raw/public/district_boundary_sources"
        / stamp
    )
    raw.mkdir(parents=True, exist_ok=False)

    metadata_path = raw / "geoboundaries_metadata.json"
    geometry_path = raw / "geoboundaries_LKA_ADM2.geojson"

    metadata_path.write_bytes(metadata_bytes)
    geometry_path.write_bytes(geometry_bytes)

    watch(metadata_path)
    watch(geometry_path)

    output = (
        root
        / "data/processed/reference"
        / ("district_boundaries_" + stamp)
    )
    output.mkdir(parents=True, exist_ok=False)

    write_csv(
        output / "district_boundary_identity_audit.csv",
        rows,
    )

    (
        output / "district_boundaries.geojson"
    ).write_text(
        json.dumps(
            prepared,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ) + "\n",
        encoding="utf-8",
    )

    result = {
        "schema_version": "district_boundary_identity_v1",
        "status": "identity_and_coordinate_checks_complete",
        "data_origin": "PUBLIC_REFERENCE",
        "created_utc": now.isoformat(),
        "registry_folder": registry.relative_to(root).as_posix(),
        "districts": len(rows),
        "provinces": len({
            row["province_name"]
            for row in rows
        }),
        "declared_alias_matches": sum(
            row["match_status"] == "declared_alias"
            for row in rows
        ),
        "coordinate_positions": sum(
            row["coordinate_positions"]
            for row in rows
        ),
        "geometry_type_counts": dict(Counter(
            row["geometry_type"]
            for row in rows
        )),
        "source": {
            "provider": "geoBoundaries gbHumanitarian",
            "upstream_page": UPSTREAM_URL,
            "api_url": API_URL,
            "metadata_final_url": metadata_final,
            "metadata": metadata,
            "boundary_id": BOUNDARY_ID,
            "github_commit": COMMIT,
            "source_reference_year": "2022",
            "retrieved_utc": (
                now.isoformat()
                if metadata_json is None
                else None
            ),
            "acquisition": acquisition,
            "download_url": DOWNLOAD_URL,
            "final_download_url": geometry_final,
            "metadata_snapshot":
                metadata_path.relative_to(root).as_posix(),
            "metadata_sha256": watched[metadata_path],
            "geometry_snapshot":
                geometry_path.relative_to(root).as_posix(),
            "geometry_sha256": watched[geometry_path],
            "licence": LICENSE,
            "licence_url":
                "https://creativecommons.org/licenses/by/3.0/igo/",
            "attribution": (
                "Survey Department of Sri Lanka; "
                "UN OCHA/HDX COD-AB; "
                "geoBoundaries (Runfola et al., 2020)."
            ),
        },
        "coordinate_reference": (
            "WGS84 longitude/latitude degrees; "
            "source CRS checked; no reprojection"
        ),
        "topology_validated": False,
        "interdistrict_overlaps_checked": False,
        "area_measured": False,
        "district_demand_ready": False,
        "checks": {
            "registry_hashes_verified": True,
            "registry_reproduced_from_DCS_snapshot": True,
            "selected_boundary_revision_checked": True,
            "full_25_district_identity_join": True,
            "unique_source_and_project_ids": True,
            "coordinate_ranges_and_ring_structure_checked": True,
            "geometry_coordinates_preserved": True,
            "input_files_unchanged": True,
        },
        "interpretation": [
            "Geography is a public reference, independent of "
            "synthetic owner inputs.",

            "Source reference year, provider build date and "
            "retrieval time are distinct; current legal boundary "
            "accuracy is not asserted.",

            "Full-resolution geometry is retained; names use "
            "declared DCS aliases, without fuzzy matching or "
            "inferred source codes.",

            "Identity and coordinate/ring checks do not establish "
            "valid topology, shared-edge agreement or suitable "
            "area/length measurements.",

            "Next: topology, overlap and projected/geodesic "
            "measurement review before spatial feature extraction.",

            "Registry, source inventory, owner parameters, "
            "eligibility and simulation results are unchanged.",
        ],
        "input_sha256": {
            path.relative_to(root).as_posix(): digest
            for path, digest in watched.items()
        },
        "output_sha256": {
            path.name: sha256(path)
            for path in sorted(output.iterdir())
        },
    }

    require(
        all(
            sha256(path) == digest
            for path, digest in watched.items()
        ),
        "Input changed during export.",
    )

    (
        output / "district_boundary_report.json"
    ).write_text(
        json.dumps(
            result,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        ) + "\n",
        encoding="utf-8",
    )

    return output, result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", required=True)
    parser.add_argument(
        "--metadata-json",
        help="Optional local provider metadata; requires --source-geojson",
    )
    parser.add_argument(
        "--source-geojson",
        help="Optional local full-resolution GeoJSON; requires --metadata-json",
    )
    args = parser.parse_args()

    output, report = prepare(
        args.registry,
        args.metadata_json,
        args.source_geojson,
    )

    print("District boundary source and identity preparation complete.")

    for key in (
        "districts",
        "provinces",
        "declared_alias_matches",
        "coordinate_positions",
        "geometry_type_counts",
    ):
        print(f"{key}: {report[key]}")

    print("boundary_reference_year: 2022")
    print("topology_validated: False")
    print("district_demand_ready: False")
    print(
        "Source, registry-link, coordinate and "
        "input-integrity checks passed."
    )
    print(f"Output: {output.relative_to(ROOT)}")
    print(
        "Public geometry preparation only; topology/measurement "
        "review is next; no demand simulations."
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
        print(
            f"District boundary preparation failed: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1)