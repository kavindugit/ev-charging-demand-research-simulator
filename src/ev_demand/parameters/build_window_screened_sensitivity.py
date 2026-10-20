"""Prepare a paired start-window screening sensitivity from the saved time-screened bank."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from statistics import mean
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from ev_demand.parameters import build_simulator_parameters as baseline
from ev_demand.parameters.build_population_evidence import CONTEXTS, require
from ev_demand.parameters.assess_owner_time_budget import inverse_power_bounds
from ev_demand.simulation.run_session_simulator import close, write_csv
from ev_demand.simulation.assess_start_window_capacity import (
    optimistic_capacity, PAD, COUNT_TOLERANCE,
)

ALL_CASES = (
    "bounded_uniform_test",
    "conditional_gamma_test",
    "conditional_lognormal_test",
)

MODEL_FILES = {
    "population_owner_case_audit.csv",
    "population_context_audit.csv",
    "population_case_summary.csv",
    "joint_owner_parameter_test_draws.csv",
}

TIME_FILES = {
    "time_screen_block_audit.csv",
    "time_screen_owner_audit.csv",
    "time_screen_group_summary.csv",
    "parameter_distribution_changes.csv",
    "screened_parameter_draws.csv",
}

CAPACITY_FILES = {
    "context_capacity_audit.csv",
    "owner_parameter_capacity_audit.csv",
    "capacity_summary.csv",
    "owner_capacity_review.csv",
}

PARAMETERS = (
    "usual_energy_kwh",
    "total_weekly_rate",
    "home_power_proxy_kw",
    *(c + "_weekly_rate" for c in CONTEXTS),
)

ASSESSMENT_FIELDS = (
    "owner_id", "cohort", "parameter_draw", "energy_case",
    "power_scenario", "context", "weekly_target_rate",
    "target_expected_study_starts", "minimum_session_duration_hours",
    "context_target_exceeds_optimistic_capacity",
    "optimistic_maximum_starts",
)


def unique_index(rows, fields):
    result = {tuple(row[k] for k in fields): row for row in rows}
    require(
        len(result) == len(rows),
        "Duplicate parameter/assessment records.",
    )
    return result


def owner_balanced_changes(source, prior, admitted, groups):
    rows = []

    for cohort, (ids, cases) in groups.items():
        if not ids:
            continue

        before_complete = all(prior[oid] for oid in ids)
        after_complete = all(admitted[oid] for oid in ids)

        for case in cases:
            for field in PARAMETERS:
                before, after = [], []

                for oid in sorted(ids):
                    a = [
                        source[(oid, case, str(i))][field]
                        for i in prior[oid]
                    ]
                    b = [
                        source[(oid, case, str(i))][field]
                        for i in admitted[oid]
                    ]

                    require(
                        not any(v.strip() for v in a)
                        or all(v.strip() for v in a),
                        "Parameter availability changes within an owner's bank.",
                    )

                    if a and a[0].strip():
                        before.append(mean(float(v) for v in a))

                    if b and b[0].strip():
                        after.append(mean(float(v) for v in b))

                rows.append(dict(
                    cohort=cohort,
                    energy_case=case,
                    parameter=field,
                    source_owners=len(ids),
                    before_owners_with_parameter=len(before),
                    after_owners_with_parameter=len(after),
                    before_owner_cohort_preserved=before_complete,
                    after_owner_cohort_preserved=after_complete,
                    before_window_screen_equal_owner_mean=(
                        mean(before)
                        if before_complete and before else None
                    ),
                    after_window_screen_equal_owner_mean=(
                        mean(after)
                        if after_complete and after else None
                    ),
                ))

    return rows


def build_sensitivity(screen_folder, capacity_folder, project_root=ROOT):
    root, watched = Path(project_root).resolve(), {}
    allowed = (root / "data/processed/synthetic").resolve()

    def watch(value, expected=None):
        path = (root / value).resolve()

        require(
            path.is_relative_to(root) and path.is_file(),
            "Missing project input: " + str(value),
        )

        digest = baseline.sha256(path)

        require(
            (expected is None or digest == expected)
            and (path not in watched or watched[path] == digest),
            "Source hash mismatch: " + str(value),
        )

        watched[path] = digest
        return path

    def package(value, filename, schema, status, files):
        folder = (root / value).resolve()

        require(
            folder.is_relative_to(allowed) and folder.is_dir(),
            "Use a completed synthetic input folder.",
        )

        report = json.loads(
            watch(folder / filename).read_text(encoding="utf-8-sig")
        )

        require(
            report.get("schema_version") == schema
            and report.get("status") == status
            and report.get("data_origin") == "SYNTHETIC"
            and bool(report.get("checks"))
            and all(v is True for v in report["checks"].values()),
            "Unexpected input: " + filename,
        )

        require(
            set(report["output_sha256"]) == files,
            "Unexpected export manifest: " + filename,
        )

        for name, digest in report["output_sha256"].items():
            watch(folder / name, digest)

        return folder, report

    screen, previous = package(
        screen_folder,
        "time_screen_sensitivity.json",
        "paired_time_screen_sensitivity_v1",
        "screening_sensitivity_prepared",
        TIME_FILES,
    )

    capacity, audited = package(
        capacity_folder,
        "start_window_capacity_report.json",
        "start_window_capacity_audit_v1",
        "complete",
        CAPACITY_FILES,
    )

    model, population = package(
        previous["source_population_folder"],
        "behaviour_population_model.json",
        "behaviour_population_model_v1",
        "components_assembled_for_review",
        MODEL_FILES,
    )

    for report in (previous, audited):
        for path, digest in report["input_sha256"].items():
            watch(path, digest)

    watch("src/ev_demand/parameters/build_window_screened_sensitivity.py")

    for folder, names in (
        (model, MODEL_FILES | {"behaviour_population_model.json"}),
        (screen, TIME_FILES | {"time_screen_sensitivity.json"}),
    ):
        for name in names:
            path = folder / name

            require(
                audited["input_sha256"].get(
                    path.relative_to(root).as_posix()
                ) == watched[path],
                "The capacity audit uses a different model or time screen: "
                + name,
            )

    require(
        audited["numerical_relaxation_hours"] == float(PAD)
        and audited["expected_count_comparison_tolerance"]
        == COUNT_TOLERANCE,
        "Capacity numerical policy differs from the recorded audit.",
    )

    days = audited["study_days"]
    require(
        type(days) is int and 1 <= days <= 56,
        "Invalid capacity reference duration.",
    )

    threshold = float(previous["threshold_hours"])
    tolerance = float(previous["numerical_tolerance_hours"])

    require(
        threshold == 168
        and math.isfinite(tolerance)
        and 0 <= tolerance < 1e-4,
        "Unexpected prior time-screen threshold.",
    )

    scenarios = population["power_scenarios"]

    require(
        scenarios == ["reduced_test", "rated_ceiling_test"],
        "Unexpected power scenarios.",
    )

    cohorts = population["comparison_cohort_ids"]
    common = set(cohorts["all_energy_cases_common"])
    extension = set(cohorts["conditional_open_upper_extension"])

    require(
        not common & extension
        and set(cohorts["conditional_cases_common"])
        == common | extension,
        "Comparison cohorts differ or overlap.",
    )

    groups = {
        "common_energy_cases": (common, ALL_CASES),
        "open_upper_extension": (extension, ALL_CASES[1:]),
    }

    group_by_owner = {
        oid: name
        for name, (ids, _) in groups.items()
        for oid in ids
    }

    owners = {r["owner_id"]: r for r in population["owners"]}

    require(
        group_by_owner
        and len(owners)
        == len(population["owners"])
        == population["owners_retained"]
        and set(group_by_owner) <= set(owners),
        "Invalid source owner grid.",
    )

    n = population["draws_per_supported_owner_case"]
    require(
        type(n) is int and n > 0,
        "Invalid saved proposal-bank size.",
    )

    original_rows = baseline.read_csv(
        model / "joint_owner_parameter_test_draws.csv"
    )

    original = unique_index(
        original_rows,
        ("owner_id", "energy_case", "parameter_draw"),
    )

    expected = {
        (oid, case, str(i))
        for ids, cases in groups.values()
        for oid in ids
        for case in cases
        for i in range(1, n + 1)
    }

    require(
        set(original) == expected
        and len(original) == population["joined_parameter_test_draws"],
        "Original joined parameter grid differs.",
    )

    prior = {}

    for row in previous["owner_screens"]:
        oid = row["owner_id"]
        values = json.loads(row["admitted_parameter_draws_json"])

        require(
            oid in group_by_owner
            and oid not in prior
            and row["cohort"] == group_by_owner[oid]
            and isinstance(values, list)
            and all(type(i) is int and 1 <= i <= n for i in values)
            and values == sorted(set(values)),
            "Invalid prior owner index bank.",
        )

        prior[oid] = values

    require(
        set(prior) == set(group_by_owner),
        "The prior screen lost an owner.",
    )

    source_rows = baseline.read_csv(
        screen / "screened_parameter_draws.csv"
    )

    source = unique_index(
        source_rows,
        ("owner_id", "energy_case", "parameter_draw"),
    )

    require(
        set(source) == {
            (oid, case, str(i))
            for ids, cases in groups.values()
            for oid in ids
            for case in cases
            for i in prior[oid]
        }
        and len(source) == previous["retained_parameter_rows"],
        "Prior retained row grid differs.",
    )

    for key, row in source.items():
        require(
            all(
                row[k] == value
                for k, value in original[key].items()
            )
            and row["screen_cohort"] == group_by_owner[key[0]]
            and math.isclose(
                float(row["within_owner_draw_probability"]),
                1 / len(prior[key[0]]),
                rel_tol=1e-10,
                abs_tol=1e-12,
            ),
            "Prior values or owner probabilities differ.",
        )

    admitted = {oid: [] for oid in prior}
    blocks, assessments, owner_screens, summaries = [], [], [], []
    assessment_map, cache = {}, {}
    completed = 0

    for cohort, (ids, cases) in groups.items():
        for oid in sorted(ids):
            contexts = {
                r["legacy_context"]: r
                for r in owners[oid]["contexts"]
            }

            require(
                set(contexts) == set(CONTEXTS),
                "Incomplete source context model.",
            )

            for i in range(1, n + 1):
                prior_pass = i in prior[oid]
                blocked = []
                checked, active = 0, 0
                maximum_hours = None

                if prior_pass:
                    for case in cases:
                        parameter = source[(oid, case, str(i))]
                        energy = float(parameter["usual_energy_kwh"])
                        total = float(parameter["total_weekly_rate"])
                        rates = {
                            c: float(parameter[c + "_weekly_rate"])
                            for c in CONTEXTS
                        }

                        require(
                            math.isfinite(energy) and energy > 0
                            and math.isfinite(total) and total > 0
                            and all(
                                math.isfinite(v) and v >= 0
                                for v in rates.values()
                            )
                            and close(math.fsum(rates.values()), total),
                            "Invalid saved energy/rate vector.",
                        )

                        home = (
                            float(parameter["home_power_proxy_kw"])
                            if parameter["home_power_proxy_kw"].strip()
                            else None
                        )

                        for scenario in scenarios:
                            expected_parts = []

                            for code in CONTEXTS:
                                rate = rates[code]
                                context = contexts[code]
                                target = rate * days / 7
                                duration = None
                                failed, upper = False, None

                                if rate > 0:
                                    require(
                                        context["use_status"]
                                        != "reported_nonuse"
                                        and context["power_status"]
                                        == "supported_test"
                                        and context["timing_rule"] is not None,
                                        "Unsupported positive-use context.",
                                    )

                                    inverse = inverse_power_bounds(
                                        context["power_models"][scenario],
                                        home,
                                    )

                                    duration = energy * inverse[0]
                                    expected_parts.append(
                                        rate * energy * inverse[1]
                                    )

                                    window = context["timing_rule"]
                                    cache_key = (
                                        duration,
                                        float(window["start_hour"]),
                                        float(window["length_hours"]),
                                        target,
                                    )

                                    if cache_key not in cache:
                                        cache[cache_key] = optimistic_capacity(
                                            duration, window, days, target
                                        )

                                    result = cache[cache_key]
                                    failed = result[
                                        "context_target_exceeds_optimistic_capacity"
                                    ]
                                    upper = result[
                                        "optimistic_maximum_starts"
                                    ]
                                    active += 1

                                row = dict(zip(
                                    ASSESSMENT_FIELDS,
                                    (
                                        oid, cohort, i, case, scenario, code,
                                        rate, target, duration, failed, upper,
                                    ),
                                ))

                                assessments.append(row)
                                assessment_map[
                                    (oid, case, i, scenario, code)
                                ] = row
                                checked += 1

                                if failed:
                                    blocked.append(dict(
                                        energy_case=case,
                                        power_scenario=scenario,
                                        context=code,
                                        target_expected_starts=target,
                                        optimistic_maximum_starts=upper,
                                    ))

                            expected_hours = math.fsum(expected_parts)

                            require(
                                expected_hours < threshold - tolerance,
                                "A prior-admitted vector fails its time screen.",
                            )

                            maximum_hours = (
                                expected_hours
                                if maximum_hours is None
                                else max(maximum_hours, expected_hours)
                            )

                passed = prior_pass and not blocked

                if passed:
                    admitted[oid].append(i)

                blocks.append(dict(
                    owner_id=oid,
                    cohort=cohort,
                    parameter_draw=i,
                    prior_time_screen_admitted=prior_pass,
                    checked_context_cells=checked,
                    active_context_cells=active,
                    maximum_expected_weekly_hours=maximum_hours,
                    admitted=passed,
                    status=(
                        "admitted_necessary_checks_only"
                        if passed else (
                            "window_bound_failed"
                            if prior_pass else
                            "not_evaluated_prior_time_screen_failed"
                        )
                    ),
                    blocking_cases_json=json.dumps(blocked),
                ))

            owner_screens.append(dict(
                owner_id=oid,
                cohort=cohort,
                original_proposal_blocks=n,
                prior_admitted_blocks=len(prior[oid]),
                admitted_blocks=len(admitted[oid]),
                newly_rejected_blocks=(
                    len(prior[oid]) - len(admitted[oid])
                ),
                prior_admitted_parameter_draws_json=json.dumps(prior[oid]),
                admitted_parameter_draws_json=json.dumps(admitted[oid]),
            ))

            completed += 1
            print(
                f"Window screen owner {completed}/{len(prior)} complete",
                flush=True,
            )

        if ids:
            before = sum(len(prior[o]) for o in ids)
            after = sum(len(admitted[o]) for o in ids)

            summaries.append(dict(
                cohort=cohort,
                source_owners=len(ids),
                original_proposal_blocks=n * len(ids),
                prior_admitted_blocks=before,
                admitted_blocks=after,
                newly_rejected_blocks=before - after,
                admission_percent_of_prior=(
                    100 * after / before if before else None
                ),
                owners_without_admitted_draws=sum(
                    not admitted[o] for o in ids
                ),
                all_owners_have_admitted_draws=all(
                    admitted[o] for o in ids
                ),
            ))

    require(
        len(assessments)
        == len(source) * len(scenarios) * len(CONTEXTS)
        and len(assessment_map) == len(assessments),
        "Incomplete context-assessment grid.",
    )

    replayed = baseline.read_csv(
        capacity / "context_capacity_audit.csv"
    )

    require(
        len(replayed) == audited["context_assessments"],
        "Capacity audit row count differs.",
    )

    for row in replayed:
        key = (
            row["owner_id"],
            audited["energy_case"],
            int(row["parameter_draw"]),
            audited["power_scenario"],
            row["context"],
        )

        value = assessment_map[key]

        require(
            row["context_target_exceeds_optimistic_capacity"]
            == str(value["context_target_exceeds_optimistic_capacity"])
            and close(
                float(row["target_expected_study_starts"]),
                value["target_expected_study_starts"],
            )
            and float(row["usual_energy_kwh"]) == float(
                source[(key[0], key[1], str(key[2]))]["usual_energy_kwh"]
            ),
            "Saved pilot capacity result did not reproduce.",
        )

    retained = [
        dict(
            row,
            prior_within_owner_draw_probability=(
                row["within_owner_draw_probability"]
            ),
            within_owner_draw_probability=1 / len(admitted[oid]),
        )
        for (oid, case, i), row in source.items()
        if int(i) in admitted[oid]
    ]

    require(
        len(retained) == sum(
            len(admitted[o]) * len(cases)
            for ids, cases in groups.values()
            for o in ids
        ),
        "Retained parameter-case pairing failed.",
    )

    changes = owner_balanced_changes(
        source, prior, admitted, groups
    )
    blocked_owners = sorted(o for o in prior if not admitted[o])

    require(
        all(
            baseline.sha256(p) == d
            for p, d in watched.items()
        ),
        "An input changed during screening.",
    )

    output = allowed / (
        "window_screened_parameters_"
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    )
    output.mkdir(parents=True, exist_ok=False)

    for name, rows in (
        ("window_screen_block_audit.csv", blocks),
        ("window_screen_owner_audit.csv", owner_screens),
        ("window_screen_group_summary.csv", summaries),
        ("window_parameter_distribution_changes.csv", changes),
    ):
        write_csv(output / name, rows, tuple(rows[0]))

    write_csv(
        output / "window_capacity_assessments.csv",
        assessments,
        ASSESSMENT_FIELDS,
    )

    write_csv(
        output / "screened_parameter_draws.csv",
        retained,
        (
            *original_rows[0],
            "screen_cohort",
            "within_owner_draw_probability",
            "prior_within_owner_draw_probability",
        ),
    )

    require(
        all(
            baseline.sha256(p) == d
            for p, d in watched.items()
        ),
        "An input changed during export.",
    )

    report = dict(
        schema_version="paired_window_screen_sensitivity_v1",
        status="screening_sensitivity_prepared",
        data_origin="SYNTHETIC",
        behaviour_input_origin="SYNTHETIC",
        output_origin="COMPUTED_PARAMETER_MODEL",
        source_population_folder=model.relative_to(root).as_posix(),
        source_time_screen_folder=screen.relative_to(root).as_posix(),
        source_capacity_audit_folder=capacity.relative_to(root).as_posix(),
        source_retained_owner_records=population["owners_retained"],
        screened_cohort_owners=len(prior),
        original_owner_draw_blocks=len(blocks),
        prior_admitted_blocks=sum(map(len, prior.values())),
        admitted_owner_draw_blocks=sum(map(len, admitted.values())),
        source_parameter_rows=len(source),
        retained_parameter_rows=len(retained),
        window_capacity_assessment_rows=len(assessments),
        unique_capacity_calculations=len(cache),
        replayed_pilot_context_rows=len(replayed),
        capacity_reference_study_days=days,
        numerical_relaxation_hours=float(PAD),
        expected_count_comparison_tolerance=COUNT_TOLERANCE,
        threshold_hours=threshold,
        numerical_tolerance_hours=tolerance,
        group_summary=summaries,
        owner_screens=owner_screens,
        owners_without_admitted_draws=blocked_owners,
        all_cohort_owners_have_admitted_draws=not blocked_owners,
        new_random_draws=0,
        new_demand_simulations=0,
        retained_parameter_values_modified=False,
        within_owner_probabilities_recomputed=True,
        original_owner_eligibility_changed=False,
        simulator_activation=False,
        joint_schedulability_established=False,
        completed_frequency_agreement_established=False,
        population_generation_ready=False,
        district_demand_ready=False,
        empirical_validation=False,
        interpretation=[
            (
                "Separate sensitivity: retain a previously time-admitted "
                "owner/draw index only if every active context passes the "
                "optimistic window bound in every required energy/power case."
            ),
            (
                "Common-cohort blocks share three energy families and two "
                "power cases; extension blocks share two families and two "
                "power cases. Reject the whole block if any cell fails."
            ),
            (
                "This conditions on the intersection of candidate-case "
                "constraints, not each family's own conditional law. "
                "Record the resulting parameter-distribution changes."
            ),
            (
                "Capacity uses an empty calendar, the fastest supported power "
                "and daily hard start windows. Passing remains necessary "
                "only; competing contexts, typical power, dwell and "
                "stationarity remain unresolved."
            ),
            (
                "The finite-horizon capacity reference is saved explicitly. "
                "Do not claim this screen establishes feasibility for "
                "another horizon or a production calendar."
            ),
            (
                "Choose an admitted index uniformly within each owner, "
                "reusing that index across compared cases. Never sample "
                "owners by flattening retained parameter rows."
            ),
            (
                "Distribution changes average within owners first, then "
                "weight owners equally. A missing owner blocks the "
                "full-cohort after mean and the pilot for that cohort; "
                "no owner is silently dropped."
            ),
            (
                "All original numerical parameter values, owner links and "
                "battery flags are copied exactly. Only within-owner "
                "sampling probabilities are recomputed; previous "
                "probabilities are retained."
            ),
            (
                "Replay the saved pilot's capacity flags, including "
                "duplicate indices, but do not use observed acceptance "
                "or load outcomes to select indices."
            ),
            (
                "Saved finite proposal indices are reused without new draws, "
                "fitting, imputation, battery clipping or eligibility changes. "
                "This is not a fitted dependence model or evidence "
                "of national composition."
            ),
        ],
        checks=dict(
            source_manifests_match=True,
            shared_population_and_screen_verified=True,
            full_prior_bank_assessed=True,
            prior_time_rule_rechecked=True,
            pilot_capacity_flags_replayed=True,
            paired_case_blocks_preserved=True,
            within_owner_weights_used=True,
            input_hashes_unchanged=True,
        ),
        input_sha256={
            p.relative_to(root).as_posix(): d
            for p, d in watched.items()
        },
        output_sha256={
            p.name: baseline.sha256(p)
            for p in sorted(output.iterdir())
        },
    )

    (output / "window_screen_sensitivity.json").write_text(
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
    parser.add_argument("--screen", required=True)
    parser.add_argument("--capacity", required=True)
    args = parser.parse_args()

    output, report = build_sensitivity(args.screen, args.capacity)

    print("Paired start-window screening sensitivity complete.")

    for field in (
        "source_retained_owner_records",
        "screened_cohort_owners",
        "capacity_reference_study_days",
    ):
        print(f"{field}: {report[field]}")

    for row in report["group_summary"]:
        print(
            f"{row['cohort']}: owners={row['source_owners']}, "
            f"admitted_blocks={row['admitted_blocks']}/"
            f"{row['prior_admitted_blocks']} prior-admitted, "
            f"added_rejections={row['newly_rejected_blocks']}, "
            f"owners_without_draws={row['owners_without_admitted_draws']}"
        )

    for field in (
        "retained_parameter_rows",
        "replayed_pilot_context_rows",
        "all_cohort_owners_have_admitted_draws",
    ):
        print(f"{field}: {report[field]}")

    print(
        "Source, full-bank capacity, paired-block "
        "and owner-weight checks passed."
    )
    print(f"Output: {output.relative_to(ROOT)}")
    print(
        "Separate sampling sensitivity; no new random draws or "
        "simulations; joint scheduling remains unverified."
    )


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError, RuntimeError) as exc:
        print(
            f"Window-screen sensitivity failed: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1)