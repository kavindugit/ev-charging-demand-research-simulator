"""Register verified district evidence and pending spatial/fleet input needs."""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from xml.etree import ElementTree as ET
from zipfile import BadZipFile

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from ev_demand.geography.build_district_registry import (
    SOURCE_ID,
    SOURCE_PAGE,
    make_aliases,
    read_reference,
    require,
    sha256,
    write_csv,
)


def pending_requirements():
    # Discovery leads and input requirements, not acquired datasets.
    definitions = [
        (
            "DISTRICT_GEOMETRY",
            "public_spatial_data",
            SOURCE_PAGE,
            "District polygons linked explicitly to all 25 DCS district IDs",
            "Select a provider; retain geometry, CRS, vintage, licence and join audit",
            "The administrative workbook contains identity fields, not polygons.",
        ),
        (
            "OSM_SPATIAL_FEATURES",
            "public_spatial_data",
            "https://dev.overpass-api.de/overpass-doc/en/",
            "Declared land-use, POI and road features within district boundaries",
            "Save extract and query; record time, tags, units, deduplication and coverage",
            "OSM coverage gaps are not measured zeros or observed charging demand.",
        ),
        (
            "TOURISM_PROXIES",
            "public_statistics",
            "https://www.sltda.gov.lk/en/statistics",
            "District tourism indicators with explicit period, coverage and units",
            "Select report and tables; audit district coverage and extraction",
            "Rooms, visitors and occupancy are different units; none is EV demand.",
        ),
        (
            "EV_FLEET_BASELINE",
            "public_statistics",
            (
                "https://dmt.gov.lk/index.php?"
                "Itemid=132&id=16&lang=en&option=com_content&view=article"
            ),
            "Baseline operating EV stock by district and included vehicle class",
            "Verify available breakdowns; document gaps and any allocation assumption",
            "New registrations are flows; all-fuel vehicle totals are not EV stock.",
        ),
        (
            "EV_FLEET_SCENARIOS",
            "derived_scenario",
            "",
            "District/year/adoption fleet table for 2026-2035, Low/Medium/High",
            "Declare baseline, growth assumptions, vehicle scope and consistency checks",
            "Future fleet values are scenarios, not observed future statistics.",
        ),
        (
            "CHARGING_LOCATION_ALLOCATION",
            "model_rule",
            "",
            "Joint district/place allocation conditional on charging context",
            "Document weights and cross-district rules; retain UNALLOCATED demand",
            "Residence is not charging location; four places must not duplicate the fleet.",
        ),
    ]

    return [
        dict(
            source_group=group,
            input_kind=kind,
            evidence_status="not_registered",
            source_lead_url=url,
            required_granularity=granularity,
            acceptance_requirement=acceptance,
            caution=caution,
            registered_data_origin="",
            raw_snapshot="",
            raw_sha256="",
            processed_artifact="",
            processed_sha256="",
            source_reference_date="",
        )
        for group, kind, url, granularity, acceptance, caution in definitions
    ]


def build_inventory(registry_folder, project_root=ROOT):
    root = Path(project_root).resolve()
    watched = {}

    def watch(value):
        path = (root / value).resolve()
        require(
            path.is_relative_to(root) and path.is_file(),
            "Input must be an existing file inside the project: " + str(value),
        )
        digest = sha256(path)
        require(
            path not in watched or watched[path] == digest,
            "Input changed during verification: " + str(path),
        )
        watched[path] = digest
        return path

    def read_csv(path):
        with path.open(encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))

    for name in (
        "docs/research_pipeline_specification.md",
        "src/ev_demand/geography/build_district_registry.py",
        "src/ev_demand/geography/build_source_inventory.py",
    ):
        watch(name)

    folder = (root / registry_folder).resolve()
    require(
        folder.is_relative_to(root) and folder.is_dir(),
        "Registry folder must be inside the project.",
    )

    report_path = watch(folder / "district_registry_report.json")
    registry_report = json.loads(
        report_path.read_text(encoding="utf-8-sig")
    )

    require(
        registry_report.get("schema_version") == "district_registry_v1"
        and registry_report.get("status") == "complete"
        and registry_report.get("registry_origin") == "PUBLIC_REFERENCE",
        "Expected a complete public district-registry report.",
    )

    required_checks = (
        "district_code_name_pairs_match",
        "province_pairs_match",
        "gnd_prefixes_confirm_codes",
        "alias_keys_unambiguous",
        "input_hashes_unchanged",
    )
    require(
        all(
            registry_report["checks"].get(key) is True
            for key in required_checks
        ),
        "A required upstream registry check did not pass.",
    )

    owner_audit = registry_report["owner_audit"]
    require(
        type(owner_audit["performed"]) is bool,
        "Invalid owner-audit flag.",
    )

    expected_files = {
        "district_registry.csv",
        "district_aliases.csv",
    }
    if owner_audit["performed"]:
        expected_files.add("owner_home_district_matches.csv")

    require(
        set(registry_report["output_sha256"]) == expected_files,
        "Unexpected registry export manifest.",
    )

    tables = {}
    for name in sorted(expected_files):
        path = watch(folder / name)
        require(
            watched[path] == registry_report["output_sha256"][name],
            "Registry export hash mismatch: " + name,
        )
        tables[name] = read_csv(path)

    source = registry_report["source"]
    require(
        source["source_id"] == SOURCE_ID,
        "Unexpected administrative source ID.",
    )

    snapshot = watch(source["snapshot"])
    require(
        watched[snapshot] == source["sha256"],
        "Raw reference snapshot hash mismatch.",
    )

    source_key = snapshot.relative_to(root).as_posix()
    require(
        registry_report["input_sha256"].get(source_key)
        == watched[snapshot],
        "Raw snapshot disagrees with the upstream input manifest.",
    )

    districts, checked = read_reference(snapshot)
    _, aliases = make_aliases()

    require(
        tables["district_registry.csv"] == districts,
        "Registry rows do not reproduce the saved official source.",
    )
    require(
        tables["district_aliases.csv"] == aliases,
        "Alias rows do not reproduce the declared matching rules.",
    )
    require(
        registry_report["districts"] == len(districts) == 25
        and registry_report["provinces"]
        == len({r["source_province_code"] for r in districts})
        == 9
        and registry_report["source_records_checked"] == checked
        and registry_report["alias_keys"] == len(aliases),
        "Registry summary does not reproduce its source and exports.",
    )

    owners = tables.get("owner_home_district_matches.csv", [])
    ids = {row["district_id"] for row in districts}
    counts = Counter(row["match_status"] for row in owners)

    require(
        set(counts).issubset(
            {"matched_canonical", "matched_alias", "unmatched"}
        ),
        "Unexpected owner residence-match status.",
    )
    require(
        len({r["owner_id"] for r in owners}) == len(owners)
        and all(r["owner_id"] for r in owners),
        "Invalid owner IDs in residence audit.",
    )
    require(
        all(
            (r["district_id"] in ids)
            if r["match_status"] != "unmatched"
            else r["district_id"] == ""
            for r in owners
        ),
        "Invalid district key in owner residence audit.",
    )
    require(
        owner_audit["owners"] == len(owners)
        and owner_audit["matched"]
        == counts["matched_canonical"] + counts["matched_alias"]
        and owner_audit["unmatched"] == counts["unmatched"]
        and owner_audit["match_status_counts"] == dict(counts)
        and owner_audit["input_origins"]
        == dict(Counter(r["data_origin"] for r in owners))
        and owner_audit["unmatched_answers"]
        == dict(
            Counter(
                r["home_district_answer"]
                for r in owners
                if r["match_status"] == "unmatched"
            )
        ),
        "Owner-audit summary disagrees with its saved export.",
    )
    require(
        all(
            r["data_origin"] in {"SYNTHETIC", "OBSERVED"}
            for r in owners
        ),
        "Unexpected owner-data origin.",
    )

    registry_csv = folder / "district_registry.csv"
    acquired = dict(
        source_group="DISTRICT_IDENTITY",
        input_kind="public_reference",
        evidence_status="verified_processed_reference",
        source_lead_url=SOURCE_PAGE,
        required_granularity="25 district identities and 9 province identities",
        acceptance_requirement=(
            "Reproduce keys and names from the saved DCS workbook"
        ),
        caution=(
            "Owner residence matches do not estimate district fleet shares "
            "or charging locations."
        ),
        registered_data_origin="PUBLIC_REFERENCE",
        raw_snapshot=source_key,
        raw_sha256=watched[snapshot],
        processed_artifact=registry_csv.relative_to(root).as_posix(),
        processed_sha256=watched[registry_csv],
        source_reference_date=source.get("source_reference_date") or "",
    )

    inventory = [acquired] + pending_requirements()

    coverage = [
        dict(
            district_id=district["district_id"],
            district_name=district["district_name"],
            province_name=district["province_name"],
            source_group=item["source_group"],
            coverage_status=(
                "identity_verified"
                if item["source_group"] == "DISTRICT_IDENTITY"
                else "not_assessed"
            ),
            evidence_status=item["evidence_status"],
            processed_artifact=item["processed_artifact"],
        )
        for district in districts
        for item in inventory
    ]

    require(
        len(
            {
                (r["district_id"], r["source_group"])
                for r in coverage
            }
        )
        == len(coverage),
        "Duplicate district/input requirement pair.",
    )
    require(
        all(
            sha256(path) == old
            for path, old in watched.items()
        ),
        "Input changed during inventory construction.",
    )

    now = datetime.now(timezone.utc)
    output = root / "data/processed/reference" / (
        "source_inventory_"
        + now.strftime("%Y%m%dT%H%M%S_%fZ")
    )
    output.mkdir(parents=True, exist_ok=False)

    write_csv(output / "source_inventory.csv", inventory)
    write_csv(output / "district_input_coverage.csv", coverage)

    report = dict(
        schema_version="research_source_inventory_v1",
        status="complete",
        created_utc=now.isoformat(),
        output_origin="DERIVED_INVENTORY",
        registry_folder=folder.relative_to(root).as_posix(),
        districts=len(districts),
        provinces=registry_report["provinces"],
        source_groups=len(inventory),
        verified_source_groups=1,
        unregistered_source_groups=len(inventory) - 1,
        district_input_coverage_rows=len(coverage),
        registry_source=source,
        upstream_owner_residence_audit=owner_audit,
        district_demand_ready=False,
        new_sources_downloaded=0,
        checks=dict(
            upstream_export_hashes_match=True,
            raw_snapshot_hash_matches=True,
            registry_reproduced_from_snapshot=True,
            owner_audit_summary_matches=True,
            unique_district_requirement_pairs=True,
            input_hashes_unchanged=True,
        ),
        input_sha256={
            p.relative_to(root).as_posix(): old
            for p, old in watched.items()
        },
        output_sha256={
            p.name: sha256(p)
            for p in sorted(output.glob("*.csv"))
        },
        interpretation=[
            (
                "Scope: district identity and spatial/fleet input requirements; "
                "not a full project data audit."
            ),
            (
                "Only the supplied registry is registered as verified evidence "
                "in this inventory."
            ),
            (
                "not_registered does not prove a file is absent elsewhere "
                "in the project."
            ),
            (
                "Source URLs are discovery leads; no additional datasets "
                "are downloaded or assessed here."
            ),
            (
                "Missing numeric inputs remain unspecified; no zero values, "
                "fleet shares or demand curves are invented."
            ),
            (
                "Registry PUBLIC_REFERENCE origin is separate from "
                "owner behavioral-data origins."
            ),
            (
                "Workbook reference date remains unknown; "
                "snapshot retrieval time is recorded separately."
            ),
            (
                "Next: behavioral population specification, then acquire/process "
                "the pending spatial and fleet inputs."
            ),
            (
                "Behavioral parameters, owner eligibility, fleet scenarios "
                "and simulations are unchanged."
            ),
        ],
    )

    require(
        all(
            sha256(path) == old
            for path, old in watched.items()
        ),
        "Input changed while exporting the inventory.",
    )

    (output / "source_inventory_report.json").write_text(
        json.dumps(report, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return output, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--registry",
        required=True,
        help="Completed district-registry output folder",
    )
    args = parser.parse_args()

    output, report = build_inventory(args.registry)

    print("Research source inventory complete.")
    for key in (
        "districts",
        "provinces",
        "source_groups",
        "verified_source_groups",
        "unregistered_source_groups",
        "district_input_coverage_rows",
    ):
        print(f"{key}: {report[key]}")

    print("district_demand_ready: False")
    print(
        "Registry source, export and residence-audit "
        "consistency checks passed."
    )
    print(f"Output: {output.relative_to(ROOT)}")
    print(
        "Inventory only; no new source downloads, "
        "numerical assumptions or simulations."
    )


if __name__ == "__main__":
    try:
        main()
    except (
        ValueError,
        OSError,
        KeyError,
        TypeError,
        IndexError,
        BadZipFile,
        ET.ParseError,
    ) as error:
        print(
            f"Source inventory failed: {error}",
            file=sys.stderr,
        )
        raise SystemExit(1)