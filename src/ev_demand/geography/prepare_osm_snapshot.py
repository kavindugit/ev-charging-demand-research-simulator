"""Acquire and inspect one dated Sri Lanka OSM extract; do not derive demand weights."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import platform
import re
import sys
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
from ev_demand.geography.build_district_registry import (
    require, sha256, write_csv,
)

try:
    import osmium
except ImportError as exc:
    raise SystemExit(
        'Install dependency: python -m pip install "osmium==4.3.2"'
    ) from exc

PAGE = "https://download.geofabrik.de/asia/sri-lanka.html"
NAME = "sri-lanka-261019.osm.pbf"
URL = "https://download.geofabrik.de/asia/" + NAME
POLY_URL = "https://download.geofabrik.de/asia/sri-lanka.poly"
EXPECTED_BYTES = 144472504
EXPECTED_MD5 = "a44e6a336014472a74deec6f2fa96a68"
KEYS = {"landuse", "tourism", "highway", "amenity"}


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def digest_file(path, algorithm):
    digest = hashlib.new(algorithm, usedforsecurity=False)
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def open_source(url, headers=None):
    response = urlopen(
        Request(
            url,
            headers={
                "User-Agent": "EVChargingResearch/1.0",
                "Accept-Encoding": "identity",
                **(headers or {}),
            },
        ),
        timeout=120,
    )
    if response.geturl() != url:
        response.close()
        raise ValueError("Unexpected source redirect: " + url)
    return response


def small_source(url):
    with open_source(url) as response:
        require(
            response.status == 200,
            "Unexpected metadata HTTP status.",
        )
        payload = response.read(2 * 1024 * 1024 + 1)
    require(
        0 < len(payload) <= 2 * 1024 * 1024,
        "Empty or oversized metadata.",
    )
    return payload


def download_pbf(path):
    offset = path.stat().st_size if path.exists() else 0
    require(
        0 <= offset <= EXPECTED_BYTES,
        "Unexpected partial-file size; review the download cache.",
    )

    if offset == EXPECTED_BYTES:
        print(
            "Complete cached download found; checking its checksum...",
            flush=True,
        )
        return offset

    headers = {"Range": f"bytes={offset}-"} if offset else {}
    print(
        f"Downloading dated OSM extract; retained bytes: {offset:,}",
        flush=True,
    )

    with open_source(URL, headers) as response:
        if offset:
            match = re.fullmatch(
                r"bytes (\d+)-(\d+)/(\d+)",
                response.headers.get("Content-Range", ""),
            )
            require(
                response.status == 206
                and match is not None
                and int(match[1]) == offset
                and int(match[2]) == EXPECTED_BYTES - 1
                and int(match[3]) == EXPECTED_BYTES,
                "Server did not honour the requested resume range; "
                "cached bytes were preserved.",
            )
        else:
            require(
                response.status == 200,
                "Unexpected PBF download HTTP status.",
            )

        remaining = EXPECTED_BYTES - offset
        length = response.headers.get("Content-Length")
        require(
            length is None or int(length) == remaining,
            "Provider file size differs from the selected snapshot.",
        )

        total, last_update = offset, time.monotonic()
        with path.open("ab" if path.exists() else "xb") as handle:
            while block := response.read(256 * 1024):
                total += len(block)
                require(
                    total <= EXPECTED_BYTES,
                    "Download exceeds the selected snapshot size.",
                )
                handle.write(block)

                if time.monotonic() - last_update >= 8:
                    print(
                        f"OSM download: {total / EXPECTED_BYTES:.1%} "
                        f"({total / 1048576:.1f} MiB)",
                        flush=True,
                    )
                    last_update = time.monotonic()

    require(
        path.stat().st_size == EXPECTED_BYTES,
        "Download incomplete; rerun the same command to resume "
        "the retained partial file.",
    )
    return offset


class TagAudit(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.counts = Counter(node=0, way=0, relation=0)
        self.tags = Counter()
        self.last_id = dict(node=0, way=0, relation=0)
        self.ordered = dict(node=True, way=True, relation=True)
        self.total = 0

    def record(self, kind, obj):
        self.ordered[kind] &= obj.id > self.last_id[kind]
        self.last_id[kind] = obj.id
        self.counts[kind] += 1

        for tag in obj.tags:
            if tag.k in KEYS:
                self.tags[(kind, tag.k, tag.v)] += 1

        self.total += 1
        if self.total % 1_000_000 == 0:
            print(
                f"OSM records scanned: {self.total:,}",
                flush=True,
            )

    def node(self, obj):
        self.record("node", obj)

    def way(self, obj):
        self.record("way", obj)

    def relation(self, obj):
        self.record("relation", obj)


def inspect_pbf(path):
    with osmium.io.Reader(
        osmium.io.File(str(path), "pbf"),
        osmium.osm.osm_entity_bits.NOTHING,
    ) as reader:
        header = reader.header()
        require(
            not header.has_multiple_object_versions,
            "Expected a current-state extract, not OSM history.",
        )

        timestamp = header.get("osmosis_replication_timestamp", "")
        require(bool(timestamp), "PBF replication timestamp is missing.")
        parsed = datetime.fromisoformat(
            timestamp.replace("Z", "+00:00")
        )
        require(
            parsed.tzinfo is not None
            and parsed <= datetime.now(timezone.utc),
            "Invalid source data timestamp.",
        )

        box = header.box()
        bounds = (
            [
                box.bottom_left.lon,
                box.bottom_left.lat,
                box.top_right.lon,
                box.top_right.lat,
            ]
            if box.valid() else None
        )
        metadata = dict(
            generator=header.get("generator", ""),
            osm_data_timestamp=timestamp,
            replication_sequence=header.get(
                "osmosis_replication_sequence_number", ""
            ),
            replication_base_url=header.get(
                "osmosis_replication_base_url", ""
            ),
            header_bbox=bounds,
        )

    handler = TagAudit()
    handler.apply_file(
        osmium.io.File(str(path), "pbf"),
        locations=False,
    )
    require(
        all(
            handler.counts[kind] > 0
            for kind in ("node", "way", "relation")
        ),
        "Expected nodes, ways and relations in the country extract.",
    )

    rows = [
        dict(
            object_type=kind,
            tag_key=key,
            tag_value=value,
            object_count=count,
        )
        for (kind, key, value), count in sorted(handler.tags.items())
    ]
    require(rows, "No declared spatial tag families were found.")
    return metadata, dict(handler.counts), handler.ordered, rows


def prepare(geometry_audit, project_root=ROOT):
    require(
        version("osmium") == "4.3.2",
        "Use the tested dependency: osmium==4.3.2.",
    )
    root, watched = Path(project_root).resolve(), {}

    def watch(value, expected=None):
        path = (root / value).resolve()
        require(
            path.is_relative_to(root) and path.is_file(),
            "Missing project input: " + str(value),
        )
        digest = digest_file(path, "sha256")
        require(
            (expected is None or digest == expected)
            and (path not in watched or watched[path] == digest),
            "Input hash mismatch: " + str(value),
        )
        watched[path] = digest
        return path

    for name in (
        "src/ev_demand/geography/prepare_osm_snapshot.py",
        "src/ev_demand/geography/build_district_registry.py",
        "docs/research_pipeline_specification.md",
    ):
        watch(name)

    audit_folder = (root / geometry_audit).resolve()
    require(
        audit_folder.is_relative_to(root / "data/processed/reference"),
        "Use the completed public geometry audit.",
    )
    audit = read_json(
        watch(audit_folder / "district_geometry_report.json")
    )
    require(
        audit.get("schema_version") == "district_geometry_audit_v1"
        and audit.get("status") == "geometry_checks_passed"
        and audit.get("data_origin") == "PUBLIC_REFERENCE"
        and audit.get("checks")
        and all(value is True for value in audit["checks"].values()),
        "A passing district geometry audit is required.",
    )

    findings = audit["findings"]
    require(
        findings["districts"]
        == findings["valid_district_geometries"] == 25
        and findings["evaluated_pairs"] == 300
        and findings["positive_area_overlap_pairs"] == 0
        and findings["coverage_valid"] is True
        and findings["geometry_checks_passed"] is True,
        "Unexpected geometry-audit findings.",
    )
    require(
        set(audit["output_sha256"]) == {
            "district_geometry_audit.csv",
            "district_pair_audit.csv",
            "district_geometry_issues.geojson",
        },
        "Incomplete geometry-audit exports.",
    )

    for name, digest in audit["input_sha256"].items():
        watch(name, digest)
    for name, digest in audit["output_sha256"].items():
        require(
            Path(name).name == name,
            "Unexpected geometry audit output name.",
        )
        watch(audit_folder / name, digest)

    print(
        "Retrieving Geofabrik source page, published checksum "
        "and extract extent...",
        flush=True,
    )
    page = small_source(PAGE)
    md5_bytes = small_source(URL + ".md5")
    extent = small_source(POLY_URL)

    require(
        NAME in page.decode("utf-8"),
        "Selected dated PBF is no longer listed by the provider.",
    )
    checksum = re.fullmatch(
        r"([0-9a-fA-F]{32})\s+\*?([^\s]+)\s*",
        md5_bytes.decode("ascii"),
    )
    require(
        checksum is not None
        and checksum[1].lower() == EXPECTED_MD5
        and checksum[2] == NAME,
        "Published checksum or filename changed; "
        "review the selected source.",
    )
    require(b"END" in extent, "Unexpected extract-extent metadata.")

    cache = root / "data/interim/public/osm_downloads"
    cache.mkdir(parents=True, exist_ok=True)
    partial = cache / (
        NAME + "." + EXPECTED_MD5[:12] + ".part"
    )

    resumed = download_pbf(partial)
    require(
        digest_file(partial, "md5") == EXPECTED_MD5,
        "PBF checksum mismatch; download cache retained for review.",
    )
    pbf_sha = digest_file(partial, "sha256")

    print(
        "Published checksum matched. Reading the PBF header "
        "and all OSM records...",
        flush=True,
    )
    metadata, counts, ordered, tag_rows = inspect_pbf(partial)

    require(
        digest_file(partial, "sha256") == pbf_sha,
        "PBF changed during inspection.",
    )
    require(
        all(
            digest_file(path, "sha256") == digest
            for path, digest in watched.items()
        ),
        "Project input changed.",
    )

    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%S_%fZ")
    raw = root / "data/raw/public/osm_sources" / stamp
    raw.mkdir(parents=True, exist_ok=False)

    pbf = raw / NAME
    partial.replace(pbf)

    for name, payload in (
        ("geofabrik_sri_lanka_source_page.html", page),
        (NAME + ".md5", md5_bytes),
        ("sri_lanka_extract_extent.poly", extent),
    ):
        (raw / name).write_bytes(payload)

    for path in raw.iterdir():
        watch(path)
    require(
        watched[pbf] == pbf_sha,
        "PBF changed while moving to the raw archive.",
    )

    output = (
        root / "data/processed/reference"
        / ("osm_snapshot_" + stamp)
    )
    output.mkdir(parents=True, exist_ok=False)
    write_csv(output / "osm_tag_presence.csv", tag_rows)

    report = dict(
        schema_version="osm_country_snapshot_v1",
        status=(
            "source_inspected"
            if all(ordered.values()) else "review_required"
        ),
        data_origin="PUBLIC_REFERENCE",
        created_utc=now.isoformat(),
        geometry_audit_folder=audit_folder.relative_to(root).as_posix(),
        districts_in_geometry_audit=25,
        source=dict(
            provider="Geofabrik; OpenStreetMap contributors",
            page_url=PAGE,
            download_url=URL,
            dated_filename=NAME,
            filename_date="2026-10-19",
            retrieval_completed_utc=now.isoformat(),
            snapshot=pbf.relative_to(root).as_posix(),
            bytes=pbf.stat().st_size,
            published_md5=EXPECTED_MD5,
            sha256=pbf_sha,
            resumed_bytes=resumed,
            licence="Open Database License (ODbL) 1.0",
            licence_url=(
                "https://opendatacommons.org/licenses/odbl/1-0/"
            ),
            attribution=(
                "© OpenStreetMap contributors; "
                "extract provided by Geofabrik."
            ),
            extent_snapshot=(
                raw / "sri_lanka_extract_extent.poly"
            ).relative_to(root).as_posix(),
            extent_same_revision_as_dated_pbf_verified=False,
            **metadata,
        ),
        object_counts=counts,
        object_ids_strictly_increasing_by_type=ordered,
        tag_families_audited=sorted(KEYS),
        tag_presence_rows=len(tag_rows),
        district_feature_extraction_complete=False,
        district_extract_extent_coverage_assessed=False,
        typology_weights_created=False,
        district_demand_ready=False,
        software=dict(
            python=platform.python_version(),
            osmium=version("osmium"),
        ),
        checks=dict(
            geometry_audit_hashes_verified=True,
            published_md5_matches=True,
            selected_file_size_matches=True,
            full_pbf_read_completed=True,
            raw_archive_sha256_matches=True,
            input_files_unchanged=True,
        ),
        interpretation=[
            "This is one dated current-state OSM extract, "
            "not a historical series or future-year map.",
            "Geofabrik's extract boundary is a buffered extraction "
            "footprint, not an official country boundary; district "
            "coverage needs a separate check.",
            "The extent file is a separately retrieved provider "
            "snapshot; its exact correspondence to the dated PBF "
            "is not asserted.",
            "Tag counts are raw OSM object counts, not deduplicated "
            "venues, district measurements, EV counts or "
            "allocation weights.",
            "OSM omissions do not establish zero activity. Geometry "
            "assembly, district clipping and feature rules are "
            "the next processing step.",
            "Existing public inventories, owner parameters, "
            "readiness and demand simulations are unchanged.",
        ],
        input_sha256={
            path.relative_to(root).as_posix(): digest
            for path, digest in watched.items()
        },
        output_sha256={
            "osm_tag_presence.csv": sha256(
                output / "osm_tag_presence.csv"
            )
        },
    )

    require(
        all(
            digest_file(path, "sha256") == digest
            for path, digest in watched.items()
        ),
        "Input changed during export.",
    )
    (output / "osm_snapshot_report.json").write_text(
        json.dumps(
            report, indent=2, ensure_ascii=False, allow_nan=False
        ) + "\n",
        encoding="utf-8",
    )
    return output, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--geometry-audit", required=True)
    args = parser.parse_args()

    output, report = prepare(args.geometry_audit)

    print("Dated Sri Lanka OSM source preparation complete.")
    print(f"status: {report['status']}")
    print(
        f"osm_data_timestamp: "
        f"{report['source']['osm_data_timestamp']}"
    )
    print(f"object_counts: {report['object_counts']}")
    print(f"tag_presence_rows: {report['tag_presence_rows']}")
    print("district_feature_extraction_complete: False")
    print("district_demand_ready: False")
    print(
        "Source checksum, complete-file reading "
        "and input-integrity checks passed."
    )
    print(f"Raw PBF: {report['source']['snapshot']}")
    print(f"Output: {output.relative_to(ROOT)}")
    print(
        "Source acquisition only; "
        "no allocation weights or demand simulations."
    )


if __name__ == "__main__":
    try:
        main()
    except (
        ValueError, OSError, KeyError,
        TypeError, RuntimeError, URLError,
    ) as exc:
        print(f"OSM source preparation failed: {exc}", file=sys.stderr)
        raise SystemExit(1)