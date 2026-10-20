"""Prepare linked survey evidence for a population model, without fitting it."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from ev_demand.parameters import build_simulator_parameters as baseline

CONTEXTS = dict(
    T1="WORKPLACE_ROUTINE",
    T2="HOME",
    T3="DESTINATION",
    T4="EN_ROUTE",
)

NUMERIC = (
    ("battery_kwh", "battery_capacity_answer", "kWh", ""),
    ("sessions_per_week", "weekly_frequency_answer", "sessions", "week"),
    ("distance_km", "distance_since_charge_answer", "km", ""),
    ("start_soc_fraction", "starting_soc_answer", "fraction", ""),
    ("target_soc_fraction", "target_soc_answer", "fraction", ""),
    ("usual_energy_kwh", "usual_energy_answer", "kWh", ""),
    ("home_power_kw", "home_equipment_answer", "kW", ""),
)

CATEGORICAL = (
    "vehicle_class_answer",
    "vehicle_use_answer",
    "home_access_answer",
    "home_equipment_answer",
    "tou_meter_answer",
    "intentional_delay_answer",
    "away_geography_answer",
    "outside_district_answer",
)

KINDS = (
    "finite_interval",
    "point",
    "lower_bound_only",
    "upper_bound_only",
    "unresolved",
    "not_applicable",
    "invalid_encoding",
    "conflict_review",
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def write_csv(path, rows):
    require(bool(rows), "Cannot export an empty evidence table.")
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=tuple(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def interval_kind(low_text, high_text, state, unit):
    try:
        low, high = [
            None if not value.strip() else float(value)
            for value in (low_text, high_text)
        ]
    except ValueError:
        return "invalid_encoding"

    if any(
        value is not None
        and (not math.isfinite(value) or value < 0)
        for value in (low, high)
    ):
        return "invalid_encoding"

    if unit == "fraction" and any(
        value is not None and value > 1
        for value in (low, high)
    ):
        return "invalid_encoding"

    if state == "not_applicable":
        return (
            "not_applicable"
            if low is None and high is None
            else "invalid_encoding"
        )

    if low is None and high is None:
        return "unresolved"
    if low is None:
        return "upper_bound_only"
    if high is None:
        return "lower_bound_only"
    if low > high:
        return "invalid_encoding"

    return "point" if low == high else "finite_interval"


def window_kind(row):
    if row["nonuse_conflict"] not in {"False", "false", "0"}:
        return "conflict_review"

    if row["context_frequency_state"] == "reported_zero":
        return "not_applicable_reported_nonuse"

    if row["window_state"] != "reported_window":
        return "unresolved"

    try:
        start, end, length = [
            float(row[key])
            for key in (
                "window_start_hour",
                "window_end_hour",
                "window_length_hours",
            )
        ]
    except ValueError:
        return "invalid_encoding"

    valid = (
        all(math.isfinite(value) for value in (start, end, length))
        and 0 <= start < 24
        and 0 <= end < 24
        and 0 < length <= 24
        and math.isclose(
            (end - start) % 24,
            length % 24,
            abs_tol=1e-8,
        )
    )

    return "reported_circular_window" if valid else "invalid_encoding"


def build_evidence(features_folder, parameters_folder, project_root=ROOT):
    root = Path(project_root).resolve()
    allowed = root / "data/processed/synthetic"
    watched = {}

    def watch(value):
        path = (root / value).resolve()
        require(
            path.is_relative_to(root) and path.is_file(),
            "Missing project input: " + str(value),
        )
        digest = baseline.sha256(path)
        require(
            path not in watched or watched[path] == digest,
            "Input changed while being read.",
        )
        watched[path] = digest
        return path

    for name in (
        "docs/research_pipeline_specification.md",
        "src/ev_demand/parameters/build_simulator_parameters.py",
        "src/ev_demand/parameters/build_population_evidence.py",
    ):
        watch(name)

    features, parameters = [
        (root / value).resolve()
        for value in (features_folder, parameters_folder)
    ]
    require(
        all(
            path.is_relative_to(allowed) and path.is_dir()
            for path in (features, parameters)
        ),
        "Use the existing synthetic feature and original bounded-parameter folders.",
    )

    paths = {
        name: watch(features / name)
        for name in (
            "owners_features.csv",
            "owner_contexts_features.csv",
            "recent_sessions_features.csv",
            "feature_report.json",
        )
    }

    package = json.loads(
        watch(parameters / "simulator_parameters.json").read_text(
            encoding="utf-8-sig"
        )
    )
    readiness = baseline.read_csv(
        watch(parameters / "owner_readiness.csv")
    )
    feature_report = json.loads(
        paths["feature_report.json"].read_text(encoding="utf-8-sig")
    )

    require(
        package.get("schema_version") == "bounded_test_parameters_v1"
        and package.get("data_origin")
        == feature_report.get("data_origin")
        == "SYNTHETIC",
        "Expected synthetic features and the original bounded-test parameter package.",
    )

    for name, path in paths.items():
        require(
            package["input_sha256"].get(name) == watched[path],
            "Features differ from the original parameter inputs: " + name,
        )

    owners = baseline.read_csv(paths["owners_features.csv"])
    contexts = baseline.read_csv(paths["owner_contexts_features.csv"])
    recent = baseline.read_csv(paths["recent_sessions_features.csv"])

    for key, rows in (
        ("owners", owners),
        ("owner_contexts", contexts),
        ("recent_sessions", recent),
    ):
        require(
            len(rows) == feature_report["counts"][key],
            "Feature count mismatch: " + key,
        )
        require(
            all(
                row["data_origin"] == "SYNTHETIC"
                and row["source_sha256"] == feature_report["source_sha256"]
                for row in rows
            ),
            "Feature provenance mismatch: " + key,
        )

    ids = {row["owner_id"] for row in owners}
    require(
        bool(owners) and "" not in ids and len(ids) == len(owners),
        "Invalid or duplicate owner IDs.",
    )

    grouped = defaultdict(list)
    for row in contexts:
        require(
            row["owner_id"] in ids,
            "Context has no matching owner.",
        )
        grouped[row["owner_id"]].append(row)

    require(
        all(
            len(grouped[owner_id]) == 4
            and {
                row["context"] for row in grouped[owner_id]
            } == set(CONTEXTS)
            for owner_id in ids
        ),
        "Each owner must have exactly the four legacy contexts.",
    )

    require(
        len({row["owner_id"] for row in recent}) == len(recent)
        and all(row["owner_id"] in ids for row in recent),
        "Invalid recent-session relationships.",
    )
    require(
        len(readiness) == len(ids)
        and {row["owner_id"] for row in readiness} == ids,
        "Readiness records do not match retained owners.",
    )

    original = {row["owner_id"]: row for row in readiness}

    for owner in owners:
        try:
            baseline.make_profile(owner, grouped[owner["owner_id"]])
            status, reason = "ready_for_bounded_test", ""
        except ValueError as error:
            status, reason = "deferred_for_review", str(error)

        saved = original[owner["owner_id"]]
        require(
            saved["data_origin"] == "SYNTHETIC"
            and saved["status"] == status
            and saved["first_deferral_reason"] == reason,
            "Original readiness did not reproduce.",
        )

    status_counts = Counter(row["status"] for row in readiness)
    require(
        package["summary"]["owners_retained_in_audit"] == len(owners)
        and package["summary"]["owners_ready_for_bounded_test"]
        == status_counts["ready_for_bounded_test"]
        and package["summary"]["owners_deferred"]
        == status_counts["deferred_for_review"],
        "Original parameter summary disagrees with readiness.",
    )

    ready_ids = {
        row["owner_id"]
        for row in readiness
        if row["status"] == "ready_for_bounded_test"
    }
    require(
        len(package["owner_profiles"]) == len(ready_ids)
        and {
            row["owner_id"] for row in package["owner_profiles"]
        } == ready_ids,
        "Original ready-profile IDs disagree with readiness.",
    )

    numeric, windows, donors, categories = [], [], [], Counter()

    def add_numeric(
        row, prefix, answer, unit, period, context="", conflict=False
    ):
        low, high, state = [
            row[prefix + suffix]
            for suffix in ("_min", "_max", "_state")
        ]
        kind = interval_kind(low, high, state, unit)

        if conflict:
            kind = "conflict_review"

        numeric.append(
            dict(
                owner_id=row["owner_id"],
                data_origin="SYNTHETIC",
                original_readiness=original[row["owner_id"]]["status"],
                parameter_id=prefix,
                legacy_context=context,
                charging_context=CONTEXTS.get(context, ""),
                unit=unit,
                reported_period=period,
                raw_answer=answer,
                raw_min=low,
                raw_max=high,
                raw_state=state,
                raw_boundary=row.get(prefix + "_boundary", ""),
                evidence_kind=kind,
            )
        )

    for owner in owners:
        owner_id = owner["owner_id"]

        for prefix, answer_field, unit, period in NUMERIC:
            add_numeric(
                owner, prefix, owner[answer_field], unit, period
            )

        for field in CATEGORICAL:
            categories[(field, "", "", "", owner[field])] += 1

        ordered = sorted(
            grouped[owner_id],
            key=lambda row: row["context"],
        )

        for row in ordered:
            code = row["context"]

            require(
                row["nonuse_conflict"]
                in {"False", "false", "0", "True", "true", "1"},
                "Unrecognised nonuse-conflict flag.",
            )

            add_numeric(
                row,
                "context_frequency",
                row["frequency_answer"],
                "sessions",
                row["context_frequency_period"],
                code,
                row["nonuse_conflict"] not in {"False", "false", "0"},
            )

            windows.append(
                dict(
                    owner_id=owner_id,
                    data_origin="SYNTHETIC",
                    original_readiness=original[owner_id]["status"],
                    legacy_context=code,
                    charging_context=CONTEXTS[code],
                    context_frequency_state=row["context_frequency_state"],
                    nonuse_conflict=row["nonuse_conflict"],
                    raw_window_state=row["window_state"],
                    raw_start_hour=row["window_start_hour"],
                    raw_end_hour=row["window_end_hour"],
                    raw_length_hours=row["window_length_hours"],
                    evidence_kind=window_kind(row),
                    charger_code=row["charger_code"],
                )
            )

            categories[
                (
                    "charger_code",
                    code,
                    row["context_frequency_state"],
                    row["nonuse_conflict"],
                    row["charger_code"],
                )
            ] += 1

        donors.append(
            dict(
                owner_id=owner_id,
                data_origin="SYNTHETIC",
                original_readiness=original[owner_id],
                owner_features=owner,
                context_features=ordered,
            )
        )

    coverage = []
    buckets = defaultdict(list)

    for row in numeric:
        for scope in ("ALL_RETAINED", row["original_readiness"]):
            buckets[
                (row["parameter_id"], row["legacy_context"], scope)
            ].append(row)

    for (parameter, context, scope), rows in sorted(buckets.items()):
        counts = Counter(row["evidence_kind"] for row in rows)
        require(
            sum(counts.values()) == len(rows),
            "Evidence accounting mismatch.",
        )

        coverage.append(
            dict(
                parameter_id=parameter,
                legacy_context=context,
                charging_context=CONTEXTS.get(context, ""),
                owner_scope=scope,
                n_owner_records=len(rows),
                **{
                    "n_" + kind: counts[kind]
                    for kind in KINDS
                },
            )
        )

    answers = [
        dict(
            parameter_id=parameter,
            legacy_context=context,
            charging_context=CONTEXTS.get(context, ""),
            context_frequency_state=state,
            nonuse_conflict=conflict,
            raw_answer=answer,
            n_owner_records=count,
        )
        for (
            parameter, context, state, conflict, answer
        ), count in sorted(categories.items())
    ]

    require(
        len(numeric) == len(owners) * (len(NUMERIC) + 4)
        and len(windows) == len(owners) * 4
        and len(donors) == len(owners),
        "Owner/parameter coverage is incomplete.",
    )
    require(
        all(
            baseline.sha256(path) == old
            for path, old in watched.items()
        ),
        "An input changed during processing.",
    )

    stamp = datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%S_%fZ"
    )
    output = allowed / ("population_evidence_" + stamp)
    output.mkdir(parents=True, exist_ok=False)

    for name, rows in (
        ("numeric_evidence.csv", numeric),
        ("numeric_coverage.csv", coverage),
        ("context_windows.csv", windows),
        ("categorical_answer_counts.csv", answers),
    ):
        write_csv(output / name, rows)

    with (output / "linked_owner_evidence.jsonl").open(
        "w", encoding="utf-8"
    ) as handle:
        for donor in donors:
            handle.write(
                json.dumps(
                    donor,
                    ensure_ascii=False,
                    allow_nan=False,
                ) + "\n"
            )

    spec = dict(
        schema_version="behaviour_population_evidence_v1",
        status="evidence_prepared",
        data_origin="SYNTHETIC",
        owners_retained=len(owners),
        context_rows_retained=len(contexts),
        original_ready_owners=status_counts["ready_for_bounded_test"],
        original_deferred_owners=status_counts["deferred_for_review"],
        numeric_evidence_rows=len(numeric),
        context_window_rows=len(windows),
        recent_sessions_retained_upstream=len(recent),
        recent_sessions_used_for_fitting=0,
        context_labels=CONTEXTS,
        place_typologies=["P1", "P2", "P3", "P4"],
        numeric_parameter_contract=[
            dict(
                parameter_id=prefix,
                answer_field=answer,
                unit=unit,
                reported_period=period,
            )
            for prefix, answer, unit, period in NUMERIC
        ],
        retained_baseline_behaviour_constraints=package.get(
            "behaviour_constraints", []
        ),
        population_contract=dict(
            estimation_pool=(
                "Use parameter-specific evidence from all retained owners, "
                "with explicit denominators."
            ),
            dependencies=(
                "Owner and four context rows remain linked; "
                "no independent marginal generator is implemented."
            ),
            missing_and_open_bounds=(
                "Retain raw answers and censoring metadata; "
                "no midpoint substitution, tail cap or imputation."
            ),
            time=(
                "Circular reported start windows; within-window timing "
                "requires an explicit sampling assumption."
            ),
            categories=(
                "Counts include unknown/mixed answers and nonuse strata; "
                "they are not fitted charger probabilities."
            ),
            home_equipment=(
                "Unknown type or power detail is not automatically "
                "a contradiction."
            ),
            frequency=(
                "Completed sessions versus attempted starts must be "
                "resolved and checked against accepted sessions."
            ),
            energy=(
                "Choose one primary energy route and electrical boundary "
                "before population generation."
            ),
            tou=(
                "Reported start windows may include intentional delay; "
                "avoid applying a second shift."
            ),
            geography=(
                "Residence is metadata; no district behaviour fits, fleet "
                "shares or charging-place weights are inferred."
            ),
            sampling_unit=(
                "Respondent owner; contexts and parameters are repeated "
                "evidence, not extra independent respondents."
            ),
            future_ids=(
                "Generated virtual-owner IDs must be separate "
                "from respondent IDs."
            ),
            interviews=(
                "Retained baseline constraints are archival; "
                "numerical interview effects require evidence review."
            ),
        ),
        parameter_fitting_performed=False,
        owner_eligibility_changed=False,
        population_generation_ready=False,
        district_demand_ready=False,
        empirical_validation=False,
        checks=dict(
            feature_hashes_match_baseline=True,
            original_readiness_reproduced=True,
            complete_owner_context_links=True,
            complete_parameter_accounting=True,
            input_hashes_unchanged=True,
        ),
        input_sha256={
            path.relative_to(root).as_posix(): old
            for path, old in watched.items()
        },
        output_sha256={
            path.name: baseline.sha256(path)
            for path in sorted(output.iterdir())
        },
    )

    require(
        all(
            baseline.sha256(path) == old
            for path, old in watched.items()
        ),
        "An input changed during export.",
    )

    (output / "behaviour_population_spec.json").write_text(
        json.dumps(
            spec,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        ) + "\n",
        encoding="utf-8",
    )

    return output, spec


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", required=True)
    parser.add_argument(
        "--parameters",
        required=True,
        help="Original bounded parameters, before the public-power layer",
    )
    args = parser.parse_args()

    output, spec = build_evidence(
        args.features,
        args.parameters,
    )

    print("Behavioural population evidence complete.")

    for key in (
        "owners_retained",
        "context_rows_retained",
        "original_ready_owners",
        "original_deferred_owners",
        "numeric_evidence_rows",
        "context_window_rows",
    ):
        print(f"{key}: {spec[key]}")

    print("Parameter coverage across all retained owners:")

    for row in baseline.read_csv(output / "numeric_coverage.csv"):
        if row["owner_scope"] != "ALL_RETAINED":
            continue

        label = row["parameter_id"] + (
            "/" + row["legacy_context"]
            if row["legacy_context"]
            else ""
        )
        finite = (
            int(row["n_finite_interval"])
            + int(row["n_point"])
        )
        one_sided = (
            int(row["n_lower_bound_only"])
            + int(row["n_upper_bound_only"])
        )

        print(
            f"  {label}: finite={finite}, one_sided={one_sided}, "
            f"unresolved={row['n_unresolved']}, "
            f"not_applicable={row['n_not_applicable']}, "
            f"invalid={row['n_invalid_encoding']}, "
            f"conflict={row['n_conflict_review']}"
        )

    print(
        "All feature-integrity, original-readiness "
        "and evidence-accounting checks passed."
    )
    print(f"Output: {output.relative_to(ROOT)}")
    print(
        "Evidence preparation only; no fitting, imputation, "
        "eligibility changes or new simulations."
    )


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(
            f"Population evidence failed: {error}",
            file=sys.stderr,
        )
        raise SystemExit(1)