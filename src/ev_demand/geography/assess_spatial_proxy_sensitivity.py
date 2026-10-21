"""Compare eight diagnostic geographic proxy-weight constructions."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from itertools import product
import json
import math
from pathlib import Path
import platform
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from ev_demand.geography.build_district_feature_review import (
    digest,
    keyed,
    number,
    read_csv,
    read_json,
    require,
    write_csv,
)

PLACES = (
    "P1_commercial",
    "P2_residential",
    "P3_tourism",
    "P4_road",
)

METHODS = (
    "minmax",
    "divide_by_maximum",
)

TOURISM = {
    "all_objects": (
        "mapped_tourism_objects_raw",
    ),
    "accommodation_visitor": (
        "mapped_accommodation_objects_raw",
        "mapped_visitor_site_objects_raw",
    ),
}

ROADS = {
    "primary_trunk": "mapped_primary_trunk_way_km",
    "with_motorway": "mapped_primary_trunk_motorway_way_km",
}

REFERENCE = "minmax__all_objects__primary_trunk"

FLAG_FIELDS = (
    "case_id",
    "district_id",
    "place",
    "issue",
    "raw_value",
    "detail",
)


def make_case(method, tourism, road):
    return dict(
        case_id=f"{method}__{tourism}__{road}",
        method=method,
        tourism_definition=tourism,
        road_scope=road,
        columns=(
            ("mapped_commercial_retail_km2",),
            ("mapped_residential_km2",),
            TOURISM[tourism],
            (ROADS[road],),
        ),
    )


def evaluate_case(rows, case):
    require(
        case["method"] in METHODS,
        "Unknown normalization method.",
    )

    raw = [
        [
            math.fsum(
                number(row[column], column, j == 2)
                for column in columns
            )
            for j, columns in enumerate(case["columns"])
        ]
        for row in rows
    ]

    require(bool(raw), "No district rows supplied.")

    low = [
        min(values[j] for values in raw)
        for j in range(4)
    ]

    high = [
        max(values[j] for values in raw)
        for j in range(4)
    ]

    usable = [
        (
            high[j] > low[j]
            if case["method"] == "minmax"
            else high[j] > 0
        )
        for j in range(4)
    ]

    anchors = [
        dict(
            case_id=case["case_id"],
            place=place,
            method=case["method"],
            source_columns_json=json.dumps(
                case["columns"][j]
            ),
            unit=("km2", "km2", "objects", "km")[j],
            reference_minimum=low[j],
            reference_maximum=high[j],
            reference_districts=len(rows),
            normalizable=usable[j],
            minimum_district_ids_json=json.dumps(
                [
                    row["district_id"]
                    for i, row in enumerate(rows)
                    if raw[i][j] == low[j]
                ]
            ),
            maximum_district_ids_json=json.dumps(
                [
                    row["district_id"]
                    for i, row in enumerate(rows)
                    if raw[i][j] == high[j]
                ]
            ),
        )
        for j, place in enumerate(PLACES)
    ]

    compositions = []
    scores = []
    flags = []

    for row, values in zip(rows, raw):
        normalized = []

        for j, value in enumerate(values):
            if not usable[j]:
                score = None

            elif case["method"] == "minmax":
                score = (
                    (value - low[j])
                    / (high[j] - low[j])
                )

            else:
                score = value / high[j]

            require(
                score is None
                or math.isfinite(score) and 0 <= score <= 1,
                "Invalid normalized score.",
            )

            normalized.append(score)

            scores.append(
                dict(
                    case_id=case["case_id"],
                    district_id=row["district_id"],
                    district_name=row["district_name"],
                    place=PLACES[j],
                    raw_value=value,
                    normalized_score=score,
                    score_status=(
                        "defined"
                        if score is not None
                        else "undefined_reference"
                    ),
                )
            )

            if score == 0 and value > 0:
                flags.append(
                    dict(
                        case_id=case["case_id"],
                        district_id=row["district_id"],
                        place=PLACES[j],
                        issue="zero_score_with_positive_raw",
                        raw_value=value,
                        detail=(
                            "A reference-relative zero is not "
                            "evidence of absent activity or zero "
                            "charging demand."
                        ),
                    )
                )

        denominator = (
            math.fsum(normalized)
            if all(value is not None for value in normalized)
            else None
        )

        if denominator is None:
            status = "undefined_reference"
        elif denominator == 0:
            status = "zero_score_sum"
        else:
            status = "defined"

        weights = (
            [value / denominator for value in normalized]
            if status == "defined"
            else [None] * 4
        )

        if status == "defined":
            require(
                all(
                    math.isfinite(value)
                    and 0 <= value <= 1
                    for value in weights
                )
                and math.isclose(
                    math.fsum(weights),
                    1,
                    abs_tol=1e-12,
                    rel_tol=1e-12,
                ),
                "Diagnostic weights do not sum to one.",
            )

        else:
            flags.append(
                dict(
                    case_id=case["case_id"],
                    district_id=row["district_id"],
                    place="ALL",
                    issue=status,
                    raw_value=None,
                    detail=(
                        "Weights remain undefined; no epsilon, "
                        "uniform fallback or feature omission "
                        "is applied."
                    ),
                )
            )

        compositions.append(
            dict(
                case_id=case["case_id"],
                district_id=row["district_id"],
                district_name=row["district_name"],
                method=case["method"],
                tourism_definition=case["tourism_definition"],
                road_scope=case["road_scope"],
                status=status,
                normalized_score_sum=denominator,
                **{
                    f"diagnostic_w_{place}": value
                    for place, value in zip(PLACES, weights)
                },
                dominant_places_json=(
                    json.dumps(
                        [
                            place
                            for place, value in zip(PLACES, weights)
                            if value == max(weights)
                        ]
                    )
                    if status == "defined"
                    else "[]"
                ),
                data_origin="PUBLIC_REFERENCE",
            )
        )

    return compositions, scores, anchors, flags


def compare_cases(compositions, cases):
    index = {
        (row["case_id"], row["district_id"]): row
        for row in compositions
    }

    require(
        len(index) == len(compositions),
        "Duplicate case/district result.",
    )

    reference_ids = sorted(
        district_id
        for case_id, district_id in index
        if case_id == REFERENCE
    )

    summaries = []
    comparisons = []

    for case in cases:
        require(
            sorted(
                district_id
                for case_id, district_id in index
                if case_id == case["case_id"]
            ) == reference_ids,
            "Case district sets differ.",
        )

        differences = []

        for district_id in reference_ids:
            base = index[(REFERENCE, district_id)]
            current = index[(case["case_id"], district_id)]

            defined = (
                base["status"] == current["status"] == "defined"
            )

            delta = (
                [
                    current[f"diagnostic_w_{place}"]
                    - base[f"diagnostic_w_{place}"]
                    for place in PLACES
                ]
                if defined
                else [None] * 4
            )

            result = dict(
                case_id=case["case_id"],
                reference_case_id=REFERENCE,
                district_id=district_id,
                district_name=current["district_name"],
                comparison_status=(
                    "defined" if defined else "undefined_input"
                ),
                **{
                    f"difference_pp_{place}": (
                        100 * value
                        if value is not None
                        else None
                    )
                    for place, value in zip(PLACES, delta)
                },
                maximum_absolute_shift_pp=(
                    max(abs(100 * value) for value in delta)
                    if defined
                    else None
                ),
                half_l1_weight_difference=(
                    math.fsum(abs(value) for value in delta) / 2
                    if defined
                    else None
                ),
                dominant_places_changed=(
                    json.loads(current["dominant_places_json"])
                    != json.loads(base["dominant_places_json"])
                    if defined
                    else None
                ),
            )

            differences.append(result)

            if case["case_id"] != REFERENCE:
                comparisons.append(result)

        valid = [
            row
            for row in differences
            if row["comparison_status"] == "defined"
        ]

        maximum = (
            max(
                row["maximum_absolute_shift_pp"]
                for row in valid
            )
            if valid
            else None
        )

        summaries.append(
            dict(
                case_id=case["case_id"],
                method=case["method"],
                tourism_definition=case["tourism_definition"],
                road_scope=case["road_scope"],
                districts=len(differences),
                defined_compositions=sum(
                    index[(case["case_id"], district_id)]["status"]
                    == "defined"
                    for district_id in reference_ids
                ),
                comparable_with_reference=len(valid),
                largest_absolute_shift_pp=maximum,
                largest_shift_districts_json=json.dumps(
                    [
                        row["district_id"]
                        for row in valid
                        if row["maximum_absolute_shift_pp"] == maximum
                    ]
                ),
                mean_half_l1_weight_difference=(
                    math.fsum(
                        row["half_l1_weight_difference"]
                        for row in valid
                    ) / len(valid)
                    if valid
                    else None
                ),
                dominant_place_changes=sum(
                    row["dominant_places_changed"]
                    for row in valid
                ),
            )
        )

    return summaries, comparisons


def assess(review_folder, project_root=ROOT):
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

    watch("src/ev_demand/geography/assess_spatial_proxy_sensitivity.py")
    watch("src/ev_demand/geography/build_district_feature_review.py")
    watch("docs/research_pipeline_specification.md")

    folder = (root / review_folder).resolve()

    require(
        folder.is_relative_to(root / "data/processed/reference"),
        "Use a processed public-reference review folder.",
    )

    review = read_json(
        watch(folder / "district_feature_review_report.json")
    )

    require(
        review.get("schema_version") == "district_feature_review_v1"
        and review.get("status") == "feature_review_bundle_prepared"
        and review.get("data_origin") == "PUBLIC_REFERENCE"
        and review.get("checks")
        and all(
            value is True
            for value in review["checks"].values()
        ),
        "Unexpected feature-review report.",
    )

    expected_outputs = {
        "district_feature_review.csv",
        "feature_statistics.csv",
        "feature_definitions.csv",
        "spatial_review_worklist.csv",
    }

    require(
        review.get("input_sha256")
        and set(review.get("output_sha256", {})) == expected_outputs,
        "Incomplete review manifests.",
    )

    require(
        review.get("districts") == 25
        and review.get("typology_weights_created") is False
        and review.get("normalization_applied") is False
        and review.get("district_demand_ready") is False,
        "Use the original unnormalized review bundle.",
    )

    for name, expected in review["input_sha256"].items():
        watch(name, expected)

    for name, expected in review["output_sha256"].items():
        require(
            Path(name).name == name,
            "Unexpected review export name.",
        )
        watch(folder / name, expected)

    rows = sorted(
        read_csv(folder / "district_feature_review.csv"),
        key=lambda row: row["district_id"],
    )

    require(
        len(
            keyed(
                rows,
                "district_id",
                "district feature review",
            )
        ) == 25
        and all(
            row["data_origin"] == "PUBLIC_REFERENCE"
            for row in rows
        ),
        "Incomplete or unexpected district table.",
    )

    for row in rows:
        total = number(
            row["mapped_tourism_objects_raw"],
            "tourism total",
            True,
        )

        scoped = sum(
            number(row[column], column, True)
            for column in TOURISM["accommodation_visitor"]
        )

        require(
            scoped <= total,
            "Scoped tourism count exceeds all selected tourism objects.",
        )

        require(
            number(
                row[ROADS["with_motorway"]],
                "expanded road length",
            ) + 1e-8 >= number(
                row[ROADS["primary_trunk"]],
                "primary/trunk length",
            ),
            "Expanded road length is smaller than the baseline.",
        )

    cases = [
        make_case(*values)
        for values in product(METHODS, TOURISM, ROADS)
    ]

    compositions = []
    scores = []
    anchors = []
    flags = []

    for case in cases:
        result = evaluate_case(rows, case)

        for destination, values in zip(
            (compositions, scores, anchors, flags),
            result,
        ):
            destination.extend(values)

    summaries, comparisons = compare_cases(
        compositions,
        cases,
    )

    require(
        len(compositions) == 25 * 8
        and len(scores) == 25 * 8 * 4
        and len(anchors) == 8 * 4
        and len(comparisons) == 25 * 7,
        "Incomplete diagnostic grid.",
    )

    require(
        all(
            digest(path) == expected
            for path, expected in watched.items()
        ),
        "Source changed during diagnostic assessment.",
    )

    now = datetime.now(timezone.utc)

    output = (
        root
        / "data/processed/reference"
        / (
            "spatial_proxy_sensitivity_"
            + now.strftime("%Y%m%dT%H%M%S_%fZ")
        )
    )

    output.mkdir(parents=True, exist_ok=False)

    for name, table in (
        ("diagnostic_proxy_weights.csv", compositions),
        ("raw_and_normalized_scores.csv", scores),
        ("normalization_references.csv", anchors),
        ("case_summary.csv", summaries),
        ("differences_from_plan.csv", comparisons),
    ):
        write_csv(output / name, table)

    write_csv(
        output / "normalization_review_flags.csv",
        flags,
        FLAG_FIELDS,
    )

    report = dict(
        schema_version="spatial_proxy_sensitivity_v1",
        status="diagnostic_proxy_cases_prepared",
        data_origin="PUBLIC_REFERENCE",
        created_utc=now.isoformat(),
        districts=25,
        cases=8,
        reference_case=REFERENCE,
        district_case_rows=len(compositions),
        score_rows=len(scores),
        comparison_rows=len(comparisons),
        normalization_methods=list(METHODS),
        composition_status_counts=dict(
            Counter(row["status"] for row in compositions)
        ),
        review_flags=len(flags),
        positive_raw_zero_score_flags=sum(
            row["issue"] == "zero_score_with_positive_raw"
            for row in flags
        ),
        case_summaries=summaries,
        source_review_folder=folder.relative_to(root).as_posix(),
        osm_data_timestamp=review["osm_data_timestamp"],
        boundary_reference_year=review["boundary_reference_year"],
        source_licence=review["source_licence"],
        source_licence_url=review["source_licence_url"],
        attribution=review["attribution"],
        boundary_licence=review["boundary_licence"],
        boundary_licence_url=review["boundary_licence_url"],
        boundary_attribution=review["boundary_attribution"],
        rules=dict(
            minmax=(
                "(x - minimum)/(maximum - minimum), "
                "then divide the four scores by their sum"
            ),
            divide_by_maximum=(
                "x/maximum, then divide the four scores by their sum"
            ),
            references=(
                "The saved 25-district public feature snapshot; "
                "reference values are exported for each "
                "method/definition case"
            ),
            feature_importance=(
                "Equal coefficients on the four normalized "
                "indicators before row normalization; "
                "an explicit diagnostic assumption"
            ),
            undefined=(
                "Keep undefined values blank when a reference "
                "denominator or row score sum is zero; "
                "no smoothing or uniform fallback"
            ),
            places=dict(
                zip(
                    PLACES,
                    (
                        "Mapped commercial/retail",
                        "Mapped residential",
                        "Mapped tourism",
                        (
                            "Mapped primary/trunk roads, "
                            "optionally including motorways"
                        ),
                    ),
                )
            ),
        ),
        diagnostic_proxy_weights_created=True,
        production_weights_activated=False,
        district_demand_ready=False,
        new_simulations=0,
        manual_five_district_review_completed=False,
        tourism_venue_deduplication_complete=False,
        method_reference="https://doi.org/10.1787/9789264043466-en",
        interpretation=[
            (
                "These deterministic geographic proxy weights "
                "are assumption diagnostics; they are not observed "
                "EV shares, owner probabilities or demand predictions."
            ),
            (
                "P1-P4 identify geographic proxy dimensions; "
                "charging-behaviour contexts T1-T4 remain separate."
            ),
            (
                "A score of zero under min-max describes the "
                "reference minimum and does not establish "
                "absent activity."
            ),
            (
                "All tourism counts retain source-identity review "
                "issues; the scoped variant changes categories "
                "only and does not deduplicate venues."
            ),
            (
                "This bounded comparison evaluates eight chosen "
                "constructions; it does not establish completeness, "
                "superiority, empirical validity or exhaustive "
                "uncertainty."
            ),
            (
                "The planned manual district/source checks and "
                "mapping-to-charging-allocation decisions remain "
                "pending; no simulator weights are updated."
            ),
        ],
        software={
            "python": platform.python_version(),
        },
        checks=dict(
            source_hashes_verified=True,
            complete_eight_case_district_grid=True,
            scores_bounded_when_defined=True,
            diagnostic_weights_sum_to_one_when_defined=True,
            common_district_comparisons_verified=True,
            undefined_values_not_imputed=True,
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
        "Source changed during export.",
    )

    (output / "spatial_proxy_sensitivity_report.json").write_text(
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
        "--review",
        required=True,
    )

    args = parser.parse_args()
    output, report = assess(args.review)

    print("Spatial proxy construction diagnostics complete.")

    for key in (
        "districts",
        "cases",
        "reference_case",
        "district_case_rows",
        "composition_status_counts",
        "positive_raw_zero_score_flags",
    ):
        print(f"{key}: {report[key]}")

    for row in report["case_summaries"]:
        shift = row["largest_absolute_shift_pp"]

        formatted = (
            "undefined"
            if shift is None
            else f"{shift:.6f}"
        )

        print(
            f"{row['case_id']}: "
            f"defined={row['defined_compositions']}/25, "
            f"maximum_shift_vs_plan={formatted} percentage points"
        )

    print("diagnostic_proxy_weights_created: True")
    print("production_weights_activated: False")
    print("district_demand_ready: False")
    print(f"Output: {output.relative_to(ROOT)}")

    print(
        "Deterministic proxy diagnostics only; "
        "no new demand simulations or empirical validation."
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
        raise SystemExit(
            f"Spatial proxy sensitivity assessment failed: {exc}"
        )