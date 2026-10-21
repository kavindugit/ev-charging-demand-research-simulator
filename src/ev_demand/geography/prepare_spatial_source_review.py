"""Prepare five-district source spot checks; leave human decisions pending."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
from itertools import product
import json
from pathlib import Path
import platform
import re
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

AUDITS = {
    "landuse": "landuse_source_audit.csv",
    "roads": "road_source_audit.csv",
    "tourism": "tourism_source_audit.csv",
}

GROUPS = {
    "landuse": ("commercial_retail", "residential"),
    "roads": ("primary", "trunk", "motorway"),
    "tourism": ("accommodation", "visitor_site", "other_categories"),
}

PER_GROUP = 2
REFERENCE = "minmax__all_objects__primary_trunk"
NORMALIZATION_ONLY = "divide_by_maximum__all_objects__primary_trunk"

CASES = {
    "__".join(values)
    for values in product(
        ("minmax", "divide_by_maximum"),
        ("all_objects", "accommodation_visitor"),
        ("primary_trunk", "with_motorway"),
    )
}

DIAGNOSTIC_EXPORTS = {
    "diagnostic_proxy_weights.csv",
    "raw_and_normalized_scores.csv",
    "normalization_references.csv",
    "case_summary.csv",
    "differences_from_plan.csv",
    "normalization_review_flags.csv",
}

REVIEW_EXPORTS = {
    "district_feature_review.csv",
    "feature_statistics.csv",
    "feature_definitions.csv",
    "spatial_review_worklist.csv",
}

EXAMPLE_FIELDS = (
    "example_id",
    "district_id",
    "district_name",
    "layer",
    "stratum",
    "osm_key",
    "object_url",
    "history_url",
    "source_audit",
    "source_record_json",
    "review_state",
)

QUESTIONS = {
    "landuse": (
        "Inspect commercial/retail and residential source tags and geometries, "
        "the saved district unions and related overlap issues. Record what was "
        "actually inspected; selected land share is not mapping completeness."
    ),
    "roads": (
        "Inspect primary, trunk and available motorway examples and related "
        "outside-boundary portions. Record the class scope and carriageway "
        "representation; mapped way length does not measure traffic."
    ),
    "tourism": (
        "Inspect accommodation, visitor-site and other-category examples and "
        "related duplicate/tag/assignment issues. Record physical-entity evidence "
        "for any proposed change; a candidate pair is not an automatic duplicate."
    ),
    "normalization": (
        "Compare the eight saved proxy compositions and any positive-raw zero "
        "scores. Explain the zero/minimum effect and whether the indicator "
        "meaning supports a future allocation assumption; these are not "
        "measured EV shares."
    ),
}


def osm_urls(key):
    require(
        re.fullmatch(r"[nwr]/[1-9][0-9]*", key) is not None,
        "Invalid OSM source identity: " + str(key),
    )
    prefix, identifier = key.split("/")
    kind = {"n": "node", "w": "way", "r": "relation"}[prefix]
    url = f"https://www.openstreetmap.org/{kind}/{identifier}"
    return url, url + "/history"


def related_districts(row, district_ids):
    values = json.loads(
        row.get("intersecting_district_ids_json", "[]")
    )
    require(
        isinstance(values, list)
        and all(isinstance(v, str) for v in values),
        "Invalid source/district review links.",
    )

    result = set(values)
    if row.get("district_id"):
        result.add(row["district_id"])

    require(
        result <= district_ids,
        "Source links to an unknown district.",
    )
    return result


def select_districts(rows, differences):
    choices = []

    for column, direction, reason in (
        (
            "mapped_commercial_retail_km2",
            "maximum",
            "commercial_maximum",
        ),
        (
            "mapped_residential_km2",
            "maximum",
            "residential_maximum",
        ),
        (
            "mapped_tourism_objects_raw",
            "maximum",
            "tourism_maximum",
        ),
        (
            "mapped_commercial_retail_km2",
            "minimum",
            "commercial_minimum",
        ),
    ):
        values = [number(r[column], column) for r in rows]
        extreme = (
            max if direction == "maximum" else min
        )(values)

        ties = sorted(
            r["district_id"]
            for r, value in zip(rows, values)
            if value == extreme
        )

        choices.append(
            (ties[0], reason, column, extreme, ties)
        )

    comparisons = [
        r
        for r in differences
        if r["case_id"] == NORMALIZATION_ONLY
        and r["comparison_status"] == "defined"
    ]

    require(
        len(comparisons) == len(rows),
        "Missing normalization-only comparisons.",
    )
    keyed(
        comparisons,
        "district_id",
        "normalization comparisons",
    )

    largest = max(
        number(r["maximum_absolute_shift_pp"], "weight shift")
        for r in comparisons
    )

    ties = sorted(
        r["district_id"]
        for r in comparisons
        if number(
            r["maximum_absolute_shift_pp"],
            "weight shift",
        ) == largest
    )

    choices.append(
        (
            ties[0],
            "largest_normalization_only_shift",
            "maximum_absolute_shift_pp",
            largest,
            ties,
        )
    )

    require(
        len({choice[0] for choice in choices}) == 5,
        "Selection roles overlap. Review the five-district "
        "selection explicitly for this snapshot.",
    )

    index = keyed(rows, "district_id", "district features")

    return [
        dict(
            district_id=d,
            district_name=index[d]["district_name"],
            selection_reason=reason,
            selection_metric=metric,
            metric_value=value,
            tied_district_ids_json=json.dumps(ties),
            selection_type="purposive_source_spot_check",
        )
        for d, reason, metric, value, ties in choices
    ]


def source_group(layer, row):
    if layer == "landuse":
        return (
            row["group"]
            if row["geometry_status"] == "valid_area"
            else None
        )

    if layer == "roads":
        return (
            row["highway_class"]
            if row["geometry_status"] == "valid_line"
            else None
        )

    if row["assignment_status"] != "assigned":
        return None

    category = row["category"]

    return (
        category
        if category in {"accommodation", "visitor_site"}
        else "other_categories"
    )


def make_examples(
    selection,
    audits,
    source_paths,
    district_ids,
):
    examples = []
    coverage = []

    for district in selection:
        d = district["district_id"]

        for layer, strata in GROUPS.items():
            for stratum in strata:
                candidates = [
                    r
                    for r in audits[layer]
                    if source_group(layer, r) == stratum
                    and d in related_districts(r, district_ids)
                ]

                def order(row):
                    key = (
                        f"spatial-source-review-v1/"
                        f"{d}/{layer}/{row['osm_key']}"
                    )
                    return (
                        hashlib.sha256(
                            key.encode("utf-8")
                        ).hexdigest(),
                        row["osm_key"],
                    )

                chosen = sorted(
                    candidates,
                    key=order,
                )[:PER_GROUP]

                coverage.append(
                    dict(
                        district_id=d,
                        district_name=district["district_name"],
                        layer=layer,
                        stratum=stratum,
                        candidate_objects=len(candidates),
                        example_objects=len(chosen),
                        sampling_rule=(
                            "First two source IDs in deterministic "
                            "SHA256 order; not a probability sample"
                        ),
                    )
                )

                for row in chosen:
                    url, history = osm_urls(row["osm_key"])

                    examples.append(
                        dict(
                            example_id=(
                                f"EXAMPLE-{len(examples) + 1:03d}"
                            ),
                            district_id=d,
                            district_name=district["district_name"],
                            layer=layer,
                            stratum=stratum,
                            osm_key=row["osm_key"],
                            object_url=url,
                            history_url=history,
                            source_audit=source_paths[layer],
                            source_record_json=json.dumps(
                                row,
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                            review_state="pending",
                        )
                    )

    return examples, coverage


def make_worklist(
    rows,
    selected,
    audits,
    district_ids,
):
    result = []
    keyed(rows, "review_id", "spatial worklist")

    indexes = {
        layer: keyed(table, "osm_key", layer + " audit")
        for layer, table in audits.items()
    }

    for row in rows:
        require(
            row["review_status"] == "pending",
            "Use the original pending worklist.",
        )

        related = json.loads(
            row["related_district_ids_json"]
        )
        keys = json.loads(row["osm_keys_json"])

        require(
            isinstance(related, list)
            and all(isinstance(v, str) for v in related)
            and set(related) <= district_ids
            and isinstance(keys, list),
            "Invalid worklist links.",
        )

        require(
            row["layer"] in indexes
            and all(
                key in indexes[row["layer"]]
                for key in keys
            ),
            "Worklist/source identity mismatch.",
        )

        links = [
            dict(
                osm_key=key,
                object_url=osm_urls(key)[0],
                history_url=osm_urls(key)[1],
            )
            for key in keys
        ]

        result.append(
            dict(
                **row,
                selected_district_related=bool(
                    set(related) & selected
                ),
                no_district_link=not related,
                source_links_json=json.dumps(links),
            )
        )

    return result


def prepare(diagnostics_folder, project_root=ROOT):
    root = Path(project_root).resolve()
    watched = {}
    base = root / "data/processed/reference"

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

    def load(
        folder,
        filename,
        schema,
        status,
        exports,
    ):
        folder = (root / folder).resolve()

        require(
            folder.is_relative_to(base),
            "Use processed public-reference inputs.",
        )

        report = read_json(watch(folder / filename))

        require(
            report.get("schema_version") == schema
            and report.get("status") == status
            and report.get("data_origin") == "PUBLIC_REFERENCE"
            and report.get("checks")
            and all(
                v is True
                for v in report["checks"].values()
            ),
            "Unexpected upstream report: " + filename,
        )

        require(
            report.get("input_sha256")
            and set(
                report.get("output_sha256", {})
            ) == exports,
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

        return folder, report

    watch(
        "src/ev_demand/geography/"
        "prepare_spatial_source_review.py"
    )
    watch(
        "src/ev_demand/geography/"
        "build_district_feature_review.py"
    )
    watch("docs/research_pipeline_specification.md")

    folder, diagnostic = load(
        diagnostics_folder,
        "spatial_proxy_sensitivity_report.json",
        "spatial_proxy_sensitivity_v1",
        "diagnostic_proxy_cases_prepared",
        DIAGNOSTIC_EXPORTS,
    )

    review_folder = (
        root / diagnostic["source_review_folder"]
    ).resolve()

    require(
        review_folder / "district_feature_review_report.json"
        in watched,
        "Diagnostic report does not pin its feature-review report.",
    )

    review_folder, review = load(
        review_folder,
        "district_feature_review_report.json",
        "district_feature_review_v1",
        "feature_review_bundle_prepared",
        REVIEW_EXPORTS,
    )

    require(
        diagnostic.get("districts")
        == review.get("districts")
        == 25
        and diagnostic.get("cases") == 8
        and diagnostic.get("reference_case") == REFERENCE
        and diagnostic.get("production_weights_activated") is False
        and diagnostic.get("district_demand_ready") is False
        and review.get("normalization_applied") is False
        and review.get("typology_weights_created") is False,
        "Unexpected upstream scope.",
    )

    require(
        all(
            diagnostic[k] == review[k]
            for k in (
                "osm_data_timestamp",
                "boundary_reference_year",
            )
        ),
        "Source vintage mismatch.",
    )

    features = sorted(
        read_csv(
            review_folder / "district_feature_review.csv"
        ),
        key=lambda r: r["district_id"],
    )

    feature_index = keyed(
        features,
        "district_id",
        "district features",
    )
    ids = set(feature_index)

    require(
        len(ids) == 25
        and all(
            r["data_origin"] == "PUBLIC_REFERENCE"
            for r in features
        ),
        "Incomplete public-reference district grid.",
    )

    weights = read_csv(
        folder / "diagnostic_proxy_weights.csv"
    )
    differences = read_csv(
        folder / "differences_from_plan.csv"
    )

    require(
        len(weights) == 200
        and {
            (r["case_id"], r["district_id"])
            for r in weights
        } == set(product(CASES, ids)),
        "Incomplete diagnostic case/district grid.",
    )

    require(
        len(differences) == 175
        and {
            (r["case_id"], r["district_id"])
            for r in differences
        } == set(product(CASES - {REFERENCE}, ids)),
        "Incomplete comparison grid.",
    )

    require(
        all(
            r["district_name"]
            == feature_index[r["district_id"]]["district_name"]
            for r in weights + differences
        ),
        "Diagnostic district names disagree.",
    )

    selection = select_districts(
        features,
        differences,
    )
    selected = {
        r["district_id"] for r in selection
    }

    audits = {}
    paths = {}

    for layer, name in AUDITS.items():
        source_folder = (
            root / review["source_folders"][layer]
        ).resolve()

        path = source_folder / name

        require(
            source_folder.is_relative_to(base)
            and path in watched,
            "Source audit is not pinned by the review: " + layer,
        )

        paths[layer] = path.relative_to(root).as_posix()
        audits[layer] = read_csv(path)

        keyed(
            audits[layer],
            "osm_key",
            layer + " source audit",
        )

        for row in audits[layer]:
            osm_urls(row["osm_key"])
            related_districts(row, ids)

    examples, coverage = make_examples(
        selection,
        audits,
        paths,
        ids,
    )

    worklist = make_worklist(
        read_csv(
            review_folder / "spatial_review_worklist.csv"
        ),
        selected,
        audits,
        ids,
    )

    require(
        len(worklist) == review["review_worklist_items"],
        "Worklist count disagreement.",
    )

    checklist = []

    for district in selection:
        d = district["district_id"]

        for topic, question in QUESTIONS.items():
            checklist.append(
                dict(
                    task_id=f"CHECK-{len(checklist) + 1:02d}",
                    district_id=d,
                    district_name=district["district_name"],
                    topic=topic,
                    question=question,
                    example_ids_json=json.dumps(
                        [
                            r["example_id"]
                            for r in examples
                            if r["district_id"] == d
                            and r["layer"] == topic
                        ]
                    ),
                    related_worklist_ids_json=json.dumps(
                        [
                            r["review_id"]
                            for r in worklist
                            if r["layer"] == topic
                            and d in json.loads(
                                r["related_district_ids_json"]
                            )
                        ]
                    ),
                    review_state="pending",
                    inspected_records="",
                    findings="",
                    evidence_reference="",
                    live_source_accessed_utc="",
                    reviewer="",
                    reviewed_utc="",
                )
            )

    require(
        len(coverage) == 40
        and len(checklist) == 20
        and len(examples) <= 80,
        "Unexpected bounded review size.",
    )

    require(
        all(
            digest(p) == h
            for p, h in watched.items()
        ),
        "Source changed during preparation.",
    )

    now = datetime.now(timezone.utc)

    output = base / (
        "spatial_source_review_"
        + now.strftime("%Y%m%dT%H%M%S_%fZ")
    )
    output.mkdir(parents=True, exist_ok=False)

    for name, table in (
        ("district_selection.csv", selection),
        (
            "selected_district_features.csv",
            [
                r
                for r in features
                if r["district_id"] in selected
            ],
        ),
        (
            "selected_district_proxy_weights.csv",
            [
                r
                for r in weights
                if r["district_id"] in selected
            ],
        ),
        ("source_example_coverage.csv", coverage),
        ("spatial_worklist_with_links.csv", worklist),
        ("manual_review_template.csv", checklist),
    ):
        write_csv(output / name, table)

    write_csv(
        output / "source_examples.csv",
        examples,
        EXAMPLE_FIELDS,
    )

    guide = [
        "# Five-district source spot-check guide",
        "",
        "This packet prepares checks; no human finding has been entered.",
        "",
        "## District selection",
        "",
        "| District | Reason |",
        "|---|---|",
    ]

    guide += [
        f"| {r['district_name']} | {r['selection_reason']} |"
        for r in selection
    ]

    guide += [
        "",
        (
            "Ties use district-ID order and are recorded in "
            "district_selection.csv. This is a purposive extreme-case "
            "check, not a representative district sample."
        ),
        "",
        "## Review once, record the evidence",
        "",
        (
            "1. Keep this generated packet unchanged. Copy "
            "manual_review_template.csv to "
            "docs/spatial_source_review_responses.csv for your findings."
        ),
        (
            "2. Work through its 20 district/topic tasks. Use "
            "source_examples.csv for saved source records and OSM "
            "object/history links; the original audit paths remain available."
        ),
        (
            "3. source_example_coverage.csv shows which mapped groups "
            "have examples. An empty group means no eligible example in "
            "the saved extraction, not absence of real activity. Inspect "
            "the original saved land-use union geometry when checking "
            "area extent and overlap."
        ),
        (
            "4. spatial_worklist_with_links.csv retains every original "
            "review entry. Filter selected_district_related=True for "
            "these five districts. Rows with no_district_link=True "
            "still require separate geographic review; no district "
            "was guessed."
        ),
        (
            "5. Record inspected IDs, findings and evidence. Use "
            "reviewed_no_change, change_requested or unresolved only "
            "after inspection. Leave uninspected tasks pending. Add "
            "reviewer and UTC review time."
        ),
        (
            "6. Live OSM pages may have changed since the saved snapshot. "
            "Record access time and distinguish live observations from "
            "saved tags/geometry. This same-source check is not "
            "independent ground-truth validation."
        ),
        (
            "7. Do not change source counts or choose weights to obtain "
            "expected rankings. Proposed fixes need a separate reviewed "
            "transformation, with before/after evidence and "
            "sensitivity checks."
        ),
        "",
        "## Boundaries of this check",
        "",
        (
            "At most two deterministic examples per district/layer "
            "group; these are spot checks, not a completeness estimate "
            "or an exhaustive object review. Global issues remain "
            "pending even when local examples look correct."
        ),
        (
            "The eight compositions retain their original 25-district "
            "references; weights are not renormalized within these five "
            "districts. Geographic P1-P4 dimensions remain separate "
            "from charging contexts T1-T4."
        ),
        (
            "The next decision is a documented geographic proxy "
            "definition and its allocation role, informed by the "
            "source review and public evidence. This packet activates "
            "no production weights, fleet scaling or demand simulations."
        ),
        "",
        "## Source attribution",
        "",
        f"OSM snapshot: {review['osm_data_timestamp']}",
        str(review["attribution"]),
        str(review["source_licence_url"]),
        "",
        f"Boundary reference year: {review['boundary_reference_year']}",
        str(review["boundary_attribution"]),
        str(review["boundary_licence_url"]),
        "",
    ]

    (output / "review_guide.md").write_text(
        "\n".join(guide),
        encoding="utf-8",
    )

    report = dict(
        schema_version="spatial_source_review_v1",
        status="human_review_packet_prepared",
        data_origin="PUBLIC_REFERENCE",
        created_utc=now.isoformat(),
        selected_districts=5,
        district_selection=selection,
        diagnostic_cases_retained=8,
        selected_district_case_rows=40,
        source_examples=len(examples),
        example_coverage_rows=len(coverage),
        manual_review_tasks=len(checklist),
        original_worklist_items=len(worklist),
        selected_district_worklist_items=sum(
            r["selected_district_related"]
            for r in worklist
        ),
        worklist_items_without_district_link=sum(
            r["no_district_link"]
            for r in worklist
        ),
        worklist_topics=dict(
            Counter(
                r["review_topic"] for r in worklist
            )
        ),
        source_diagnostics_folder=(
            folder.relative_to(root).as_posix()
        ),
        source_review_folder=(
            review_folder.relative_to(root).as_posix()
        ),
        osm_data_timestamp=review["osm_data_timestamp"],
        boundary_reference_year=review["boundary_reference_year"],
        attribution=review["attribution"],
        source_licence=review["source_licence"],
        source_licence_url=review["source_licence_url"],
        boundary_attribution=review["boundary_attribution"],
        boundary_licence=review["boundary_licence"],
        boundary_licence_url=review["boundary_licence_url"],
        human_review_completed=False,
        human_decisions_recorded=0,
        source_objects_changed=0,
        production_weights_activated=False,
        district_demand_ready=False,
        new_simulations=0,
        rules=dict(
            selection=(
                "Four measured feature extrema plus the largest "
                "normalization-only effect; ties use district-ID order"
            ),
            examples=(
                "At most two source IDs per saved district/layer group "
                "in fixed SHA256 order; no random draws or "
                "representativeness claim"
            ),
            issues=(
                "Retain the entire original worklist; a district "
                "review link is not a new allocation"
            ),
            live_links=(
                "Unfrozen OSM object and history views; saved source "
                "records remain the calculation evidence"
            ),
            findings=(
                "All findings remain pending; keep this packet "
                "unchanged and enter human findings in a separate "
                "response copy"
            ),
        ),
        software={
            "python": platform.python_version(),
        },
        checks=dict(
            upstream_hashes_verified=True,
            source_diagnostic_grid_complete=True,
            five_distinct_districts_selected=True,
            saved_case_weights_retained=True,
            source_identity_and_district_links_verified=True,
            full_worklist_retained=True,
            all_manual_decisions_pending=True,
            input_files_unchanged=True,
        ),
        input_sha256={
            p.relative_to(root).as_posix(): h
            for p, h in watched.items()
        },
        output_sha256={
            p.name: digest(p)
            for p in sorted(output.iterdir())
        },
    )

    require(
        all(
            digest(p) == h
            for p, h in watched.items()
        ),
        "Source changed during export.",
    )

    (output / "spatial_source_review_report.json").write_text(
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
    parser = argparse.ArgumentParser(
        description=__doc__
    )
    parser.add_argument(
        "--diagnostics",
        required=True,
    )
    args = parser.parse_args()

    output, report = prepare(args.diagnostics)

    print("Five-district source-review packet complete.")
    print(
        "Selected districts: "
        + ", ".join(
            r["district_name"]
            for r in report["district_selection"]
        )
    )

    for key in (
        "source_examples",
        "example_coverage_rows",
        "manual_review_tasks",
        "original_worklist_items",
        "selected_district_worklist_items",
        "worklist_items_without_district_link",
    ):
        print(f"{key}: {report[key]}")

    print(
        "Source hashes, case grid, source links "
        "and worklist checks passed."
    )
    print("human_review_completed: False")
    print("production_weights_activated: False")
    print("district_demand_ready: False")
    print(f"Output: {output.relative_to(ROOT)}")
    print(
        "Review preparation only; all human findings pending; "
        "no new demand simulations."
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
            f"Spatial source-review preparation failed: {exc}"
        )