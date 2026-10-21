"""Compare district distributions of mapped tourism objects and registered rooms."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from ev_demand.geography.build_district_feature_review import (
    digest, keyed, number, read_csv, read_json, require, write_csv,
)


REFERENCE = "sltda_rooms_2025"

ORDER = (
    "osm_all_tourism_objects",
    "osm_accommodation_visitor_objects",
    "sltda_rooms_2024",
    REFERENCE,
)


def construct(features, capacity, osm_timestamp):
    identity = keyed(features, "district_id", "OSM district feature table")

    require(len(identity) == 25, "Expected all 25 districts.")
    require(
        len({row["province_name"] for row in features}) == 9,
        "Expected nine provinces.",
    )

    rooms = {}

    for row in capacity:
        district_id = row["district_id"]
        year = number(row["reference_year"], "capacity year", True)
        key = (year, district_id)

        require(
            district_id in identity
            and year in {2024, 2025}
            and key not in rooms,
            "Unexpected or duplicate room-count key.",
        )

        require(
            row["unit"] == "rooms"
            and row["data_origin"] == "PUBLIC_REFERENCE",
            "Unexpected room-count unit or origin.",
        )

        require(
            all(
                row[column] == identity[district_id][column]
                for column in ("district_name", "province_name")
            ),
            "District identities differ between public sources.",
        )

        rooms[key] = number(
            row["registered_room_count"], "registered rooms", True
        )

    require(
        set(rooms) == {
            (year, district_id)
            for year in (2024, 2025)
            for district_id in identity
        },
        "Incomplete two-year district room grid.",
    )

    definitions = [
        dict(
            indicator_id=ORDER[0],
            label="OSM all retained tourism objects",
            unit="mapped_objects",
            source="OpenStreetMap / Geofabrik",
            vintage=osm_timestamp,
            scope=(
                "All retained assigned categories; original object "
                "identities; unresolved duplicates retained"
            ),
        ),
        dict(
            indicator_id=ORDER[1],
            label="OSM accommodation and visitor-site objects",
            unit="mapped_objects",
            source="OpenStreetMap / Geofabrik",
            vintage=osm_timestamp,
            scope=(
                "Assigned accommodation plus visitor_site categories; "
                "category filter only; no deduplication"
            ),
        ),
        dict(
            indicator_id=ORDER[2],
            label="SLTDA registered rooms 2024",
            unit="rooms",
            source="SLTDA",
            vintage="2024",
            scope="Registered room counts in the 2024 district table",
        ),
        dict(
            indicator_id=ORDER[3],
            label="SLTDA registered rooms 2025",
            unit="rooms",
            source="SLTDA",
            vintage="2025",
            scope=(
                "Registered room counts in the 2025 district table; "
                "comparison reference"
            ),
        ),
    ]

    values = {key: {} for key in ORDER}

    for district_id, row in identity.items():
        require(
            row["data_origin"] == "PUBLIC_REFERENCE",
            "Unexpected OSM feature origin.",
        )

        broad = number(
            row["mapped_tourism_objects_raw"],
            "mapped tourism objects",
            True,
        )

        scoped = sum(
            number(row[column], column, True)
            for column in (
                "mapped_accommodation_objects_raw",
                "mapped_visitor_site_objects_raw",
            )
        )

        require(
            scoped <= broad,
            "Scoped tourism count exceeds the broad count.",
        )

        values[ORDER[0]][district_id] = broad
        values[ORDER[1]][district_id] = scoped

        for year in (2024, 2025):
            values[f"sltda_rooms_{year}"][district_id] = rooms[
                (year, district_id)
            ]

    distributions = []

    for definition in definitions:
        indicator = definition["indicator_id"]
        counts = values[indicator]
        total = sum(counts.values())

        definition.update(
            districts=25,
            total_raw_value=total,
            data_origin="PUBLIC_REFERENCE",
        )

        for district_id in sorted(identity):
            value = counts[district_id]
            share = value / total if total > 0 else None

            distributions.append(dict(
                indicator_id=indicator,
                district_id=district_id,
                district_name=identity[district_id]["district_name"],
                province_name=identity[district_id]["province_name"],
                raw_value=value,
                raw_unit=definition["unit"],
                source_vintage=definition["vintage"],
                indicator_total=total,
                district_share=share,
                district_share_percent=(
                    100 * share if share is not None else None
                ),
                descending_competition_rank=(
                    1 + sum(other > value for other in counts.values())
                    if total > 0 else None
                ),
                status=(
                    "defined"
                    if share is not None
                    else "undefined_zero_indicator_total"
                ),
                share_scope=(
                    "district_fraction_of_25_district_total_"
                    "within_this_indicator"
                ),
                data_origin="PUBLIC_REFERENCE",
            ))

        if total > 0:
            require(
                math.isclose(
                    math.fsum(
                        row["district_share"]
                        for row in distributions
                        if row["indicator_id"] == indicator
                    ),
                    1,
                    abs_tol=1e-12,
                    rel_tol=1e-12,
                ),
                "Indicator district shares do not sum to one.",
            )

    index = {
        (row["indicator_id"], row["district_id"]): row
        for row in distributions
    }

    differences, summaries = [], []

    for indicator in ORDER[:-1]:
        group = []

        for district_id in sorted(identity):
            current = index[(indicator, district_id)]
            reference = index[(REFERENCE, district_id)]

            defined = (
                current["status"] == reference["status"] == "defined"
            )

            group.append(dict(
                indicator_id=indicator,
                reference_indicator_id=REFERENCE,
                district_id=district_id,
                district_name=current["district_name"],
                indicator_share_percent=current["district_share_percent"],
                reference_share_percent=reference["district_share_percent"],
                difference_pp=(
                    current["district_share_percent"]
                    - reference["district_share_percent"]
                    if defined else None
                ),
                indicator_rank=current["descending_competition_rank"],
                reference_rank=reference["descending_competition_rank"],
                rank_difference=(
                    current["descending_competition_rank"]
                    - reference["descending_competition_rank"]
                    if defined else None
                ),
                status="defined" if defined else "undefined_input",
                data_origin="PUBLIC_REFERENCE",
            ))

        valid = [
            row for row in group if row["status"] == "defined"
        ]

        maximum = (
            max(abs(row["difference_pp"]) for row in valid)
            if valid else None
        )

        distance = (
            math.fsum(
                abs(row["difference_pp"]) / 100 for row in valid
            ) / 2
            if valid else None
        )

        require(
            not valid or len(valid) == 25,
            "Partial comparison grid.",
        )

        require(
            not valid
            or abs(math.fsum(row["difference_pp"] for row in valid))
            < 1e-10,
            "Signed district-share differences do not balance.",
        )

        require(
            distance is None or 0 <= distance <= 1 + 1e-12,
            "Invalid half-L1 share distance.",
        )

        summaries.append(dict(
            indicator_id=indicator,
            reference_indicator_id=REFERENCE,
            comparable_districts=len(valid),
            largest_absolute_share_difference_pp=maximum,
            largest_gap_district_ids_json=json.dumps([
                row["district_id"]
                for row in valid
                if abs(row["difference_pp"]) == maximum
            ]),
            largest_gap_district_names_json=json.dumps([
                row["district_name"]
                for row in valid
                if abs(row["difference_pp"]) == maximum
            ]),
            half_l1_share_distance=distance,
            largest_absolute_rank_difference=(
                max(abs(row["rank_difference"]) for row in valid)
                if valid else None
            ),
            status="defined" if valid else "undefined_input",
            data_origin="PUBLIC_REFERENCE",
        ))

        differences.extend(group)

    require(
        len(distributions) == 100 and len(differences) == 75,
        "Incomplete indicator comparison.",
    )

    return definitions, distributions, differences, summaries


def draw(output, definitions, distributions):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    index = {
        (row["indicator_id"], row["district_id"]): row
        for row in distributions
    }

    ids = sorted(
        (
            row["district_id"]
            for row in distributions
            if row["indicator_id"] == REFERENCE
        ),
        key=lambda district_id: (
            -index[(REFERENCE, district_id)]["raw_value"],
            district_id,
        ),
    )

    fig, ax = plt.subplots(figsize=(11, 11.5))
    colours = ("#1b9e77", "#d95f02", "#80a6d0", "#173d69")

    for position, (definition, colour) in enumerate(
        zip(definitions, colours)
    ):
        indicator = definition["indicator_id"]
        rows = [
            index[(indicator, district_id)] for district_id in ids
        ]

        defined = [
            (index_number, row)
            for index_number, row in enumerate(rows)
            if row["district_share_percent"] is not None
        ]

        ax.scatter(
            [row["district_share_percent"] for _, row in defined],
            [
                index_number + (position - 1.5) * 0.14
                for index_number, _ in defined
            ],
            s=30,
            color=colour,
            label=definition["label"],
            zorder=3,
        )

    ax.set_yticks(
        range(25),
        [
            index[(REFERENCE, district_id)]["district_name"]
            for district_id in ids
        ],
    )

    ax.invert_yaxis()
    ax.set_xlim(left=0)

    ax.set_xlabel(
        "District share of the 25-district total within each indicator (%)"
    )

    ax.set_title(
        "Tourism indicators describe different district distributions\n"
        "Ordered by reported 2025 room capacity",
        pad=14,
    )

    ax.grid(axis="x", alpha=0.2)
    ax.legend(loc="lower right", fontsize=9)

    fig.subplots_adjust(
        left=0.18, right=0.97, top=0.91, bottom=0.13
    )

    fig.text(
        0.5,
        0.065,
        "Mapped objects and registered rooms have different units, "
        "coverage and vintages.\n"
        "These shares are indicator comparisons; they are not "
        "EV-demand allocation weights.",
        ha="center",
        fontsize=9,
    )

    fig.text(
        0.5,
        0.025,
        "Sources: © OpenStreetMap contributors / Geofabrik; "
        "SLTDA Year in Review 2024 and 2025.",
        ha="center",
        fontsize=8,
    )

    fig.savefig(
        output / "tourism_indicator_district_shares.png",
        dpi=160,
    )
    fig.savefig(output / "tourism_indicator_district_shares.svg")

    plt.close(fig)
    return matplotlib.__version__


def assess(
    review_folder,
    tables_folder,
    policy_path,
    project_root=ROOT,
):
    root, watched = Path(project_root).resolve(), {}

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

    def load(folder, filename):
        folder = (root / folder).resolve()

        require(
            folder.is_relative_to(root / "data/processed/reference"),
            "Use processed public-reference inputs.",
        )

        report = read_json(watch(folder / filename))

        require(
            report.get("input_sha256") and report.get("output_sha256"),
            "Incomplete source manifests.",
        )

        for name, expected in report["input_sha256"].items():
            watch(name, expected)

        for name, expected in report["output_sha256"].items():
            require(
                Path(name).name == name,
                "Unexpected source export name.",
            )
            watch(folder / name, expected)

        return folder, report

    watch("src/ev_demand/geography/compare_tourism_spatial_proxies.py")
    watch("src/ev_demand/geography/build_district_feature_review.py")

    review_folder, review = load(
        review_folder,
        "district_feature_review_report.json",
    )

    tables_folder, tables = load(
        tables_folder,
        "sltda_table_report.json",
    )

    require(
        review.get("schema_version") == "district_feature_review_v1"
        and review.get("status") == "feature_review_bundle_prepared"
        and review.get("data_origin") == "PUBLIC_REFERENCE"
        and review.get("districts") == 25
        and review.get("normalization_applied") is False
        and review.get("typology_weights_created") is False
        and review.get("checks")
        and all(value is True for value in review["checks"].values()),
        "Unexpected OSM feature review.",
    )

    require(
        tables.get("schema") == "sltda_district_tables_v1"
        and tables.get("status") == "tables_extracted_with_review_flags"
        and tables.get("data_origin") == "PUBLIC_REFERENCE"
        and tables.get("districts") == 25
        and tables.get("missing_values_imputed") is False,
        "Unexpected SLTDA table package.",
    )

    policy = watch(policy_path)

    match = re.search(
        r"Table-report SHA256:\s*([0-9a-fA-F]{64})",
        policy.read_text(encoding="utf-8-sig"),
    )

    require(
        match is not None
        and match.group(1).lower()
        == digest(tables_folder / "sltda_table_report.json"),
        "The decision note refers to a different SLTDA table report.",
    )

    features = read_csv(
        watch(review_folder / "district_feature_review.csv")
    )

    capacity = read_csv(
        watch(tables_folder / "district_room_capacity.csv")
    )

    definitions, distributions, differences, summaries = construct(
        features,
        capacity,
        review["osm_data_timestamp"],
    )

    require(
        all(
            digest(path) == expected
            for path, expected in watched.items()
        ),
        "Source changed during comparison.",
    )

    now = datetime.now(timezone.utc)

    output = (
        root / "data/processed/reference"
        / (
            "tourism_proxy_comparison_"
            + now.strftime("%Y%m%dT%H%M%S_%fZ")
        )
    )

    output.mkdir(parents=True, exist_ok=False)

    for name, rows in (
        ("indicator_definitions.csv", definitions),
        ("district_indicator_shares.csv", distributions),
        ("differences_from_2025_room_shares.csv", differences),
        ("indicator_comparison_summary.csv", summaries),
    ):
        write_csv(output / name, rows)

    matplotlib_version = draw(
        output, definitions, distributions
    )

    require(
        all(
            digest(path) == expected
            for path, expected in watched.items()
        ),
        "Source changed during export.",
    )

    provenance_fields = (
        "osm_data_timestamp",
        "boundary_reference_year",
        "source_licence",
        "source_licence_url",
        "attribution",
        "boundary_licence",
        "boundary_licence_url",
        "boundary_attribution",
    )

    report = dict(
        schema="tourism_spatial_proxy_comparison_v1",
        status="indicator_comparison_prepared",
        created_utc=now.isoformat(),
        data_origin="PUBLIC_REFERENCE",
        districts=25,
        indicators=4,
        district_indicator_rows=len(distributions),
        comparison_rows=len(differences),
        reference_indicator=REFERENCE,
        indicator_definitions=definitions,
        comparison_summaries=summaries,
        source_review_folder=review_folder.relative_to(root).as_posix(),
        source_tables_folder=tables_folder.relative_to(root).as_posix(),
        decision_document=policy.relative_to(root).as_posix(),
        osm_provenance={
            key: review[key] for key in provenance_fields
        },
        sltda_numerical_review_flags_retained=(
            tables["numerical_review_flags"]
        ),
        rules=dict(
            shares=(
                "Raw indicator value divided by the sum of that "
                "indicator across the same 25 districts"
            ),
            differences=(
                "Indicator district share minus 2025 room district "
                "share, expressed in percentage points"
            ),
            ranks=(
                "Descending competition ranks: tied values share rank; "
                "the following rank skips tied positions"
            ),
            half_l1=(
                "Half the sum of absolute district-share differences; "
                "a descriptive distance, not an error against truth"
            ),
            zero_total=(
                "Undefined shares and comparisons remain blank; "
                "no smoothing or uniform replacement"
            ),
        ),
        occupancy_used=False,
        bed_conversion_used=False,
        production_weights_activated=False,
        unique_venue_counts_established=False,
        mapping_completeness_established=False,
        district_demand_ready=False,
        new_demand_simulations=0,
        interpretation_limits=[
            "The reference is a reported accommodation-capacity "
            "indicator, not observed tourism activity or EV demand.",
            "OSM object counts and SLTDA room counts measure different "
            "concepts; agreement or disagreement is not empirical validation.",
            "OSM categories and unresolved duplicate representations "
            "are retained; the scoped indicator is not a unique-venue count.",
            "Within-indicator district shares differ from four-place "
            "compositions within a district; neither is activated as an EV share.",
            "The 2024/2025 room comparison describes reported "
            "distributions; revisions or scope changes may affect it, "
            "and it is not a growth forecast.",
        ],
        software=dict(
            python=sys.version.split()[0],
            matplotlib=matplotlib_version,
        ),
        checks=dict(
            source_hashes_verified=True,
            complete_district_indicator_grid=True,
            district_identities_match=True,
            defined_shares_sum_to_one=True,
            signed_share_differences_balance=True,
            source_review_flags_retained=True,
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

    (output / "tourism_proxy_comparison_report.json").write_text(
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

    parser.add_argument("--review", required=True)
    parser.add_argument("--tables", required=True)

    parser.add_argument(
        "--decisions",
        default="docs/sltda_evidence_use_decisions.md",
    )

    args = parser.parse_args()

    output, report = assess(
        args.review,
        args.tables,
        args.decisions,
    )

    print("Tourism spatial-indicator comparison complete.")
    print("districts:", report["districts"])
    print("indicators:", report["indicators"])
    print("district_indicator_rows:", report["district_indicator_rows"])
    print("comparison_rows:", report["comparison_rows"])

    for row in report["comparison_summaries"]:
        value = row["largest_absolute_share_difference_pp"]

        print(
            row["indicator_id"]
            + ": maximum share difference vs 2025 rooms="
            + (
                f"{value:.6f} percentage points"
                if value is not None
                else "undefined"
            )
        )

    print(
        "Source, district-identity, share-accounting and "
        "input-integrity checks passed."
    )

    print("Figures: 1 PNG + 1 SVG")
    print("Output:", output.relative_to(ROOT).as_posix())

    print(
        "Public indicator comparison only; no EV allocation "
        "weights or new demand simulations."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            "Tourism proxy comparison failed:",
            exc,
            file=sys.stderr,
        )
        raise SystemExit(1)