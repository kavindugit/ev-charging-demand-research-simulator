"""Prepare a paired, owner-balanced time-budget screening sensitivity."""
from __future__ import annotations

import argparse
from collections import defaultdict
import csv
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from statistics import mean
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from ev_demand.parameters import build_simulator_parameters as baseline
from ev_demand.parameters.build_population_evidence import require, write_csv

MODEL_FILES = {
    "population_owner_case_audit.csv",
    "population_context_audit.csv",
    "population_case_summary.csv",
    "joint_owner_parameter_test_draws.csv",
}
BUDGET_FILES = {
    "owner_time_budget_draws.csv",
    "time_budget_case_summary.csv",
    "time_budget_owner_summary.csv",
}
ALL_CASES = (
    "bounded_uniform_test",
    "conditional_gamma_test",
    "conditional_lognormal_test",
)


def unique_index(rows, fields):
    keys = [tuple(r[f] for f in fields) for r in rows]
    require(
        len(keys) == len(set(keys)),
        "Duplicate parameter/assessment records.",
    )
    return dict(zip(keys, rows))


def distribution_summary(source, admitted, groups, n):
    """Each owner contributes one mean regardless of admitted draw count."""
    rows = []

    for cohort, (ids, cases) in groups.items():
        if not ids:
            continue

        complete = all(admitted[oid] for oid in ids)

        for case in cases:
            for field in (
                "usual_energy_kwh",
                "total_weekly_rate",
                "home_power_proxy_kw",
            ):
                before, after = [], []

                for oid in sorted(ids):
                    initial = [
                        float(source[(oid, case, str(i))][field])
                        for i in range(1, n + 1)
                        if source[(oid, case, str(i))][field].strip()
                    ]
                    if not initial:
                        continue

                    before.append(mean(initial))
                    kept = [
                        float(source[(oid, case, str(i))][field])
                        for i in admitted[oid]
                    ]
                    if kept:
                        after.append(mean(kept))

                rows.append(dict(
                    cohort=cohort,
                    energy_case=case,
                    parameter=field,
                    source_owners=len(ids),
                    owners_with_parameter=len(before),
                    owner_cohort_preserved=complete,
                    before_equal_owner_mean=(
                        mean(before) if before else None
                    ),
                    after_equal_owner_mean=(
                        mean(after) if complete and after else None
                    ),
                ))

    return rows


def build_sensitivity(
    model_folder, budget_folder, project_root=ROOT
):
    root, watched = Path(project_root).resolve(), {}
    allowed = (root / "data/processed/synthetic").resolve()

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

    def package(value, report_name, schema, status, files):
        folder = (root / value).resolve()
        require(
            folder.is_relative_to(allowed) and folder.is_dir(),
            "Use a completed synthetic input folder.",
        )
        report = json.loads(
            watch(folder / report_name).read_text(
                encoding="utf-8-sig"
            )
        )
        require(
            report.get("schema_version") == schema
            and report.get("status") == status
            and report.get("data_origin") == "SYNTHETIC",
            "Unexpected input: " + report_name,
        )
        require(
            bool(report.get("checks"))
            and all(v is True for v in report["checks"].values()),
            "An upstream check failed: " + report_name,
        )
        require(
            set(report["output_sha256"]) == files,
            "Unexpected export manifest: " + report_name,
        )

        for name in sorted(files):
            path = watch(folder / name)
            require(
                watched[path] == report["output_sha256"][name],
                "Export hash mismatch: " + name,
            )

        return folder, report

    for name in (
        "docs/research_pipeline_specification.md",
        "src/ev_demand/parameters/build_simulator_parameters.py",
        "src/ev_demand/parameters/build_population_evidence.py",
        "src/ev_demand/parameters/build_time_screened_sensitivity.py",
    ):
        watch(name)

    model, population = package(
        model_folder,
        "behaviour_population_model.json",
        "behaviour_population_model_v1",
        "components_assembled_for_review",
        MODEL_FILES,
    )
    budget, assessed = package(
        budget_folder,
        "owner_time_budget_report.json",
        "owner_time_budget_assessment_v1",
        "proxy_time_budget_assessed",
        BUDGET_FILES,
    )

    for name in MODEL_FILES | {"behaviour_population_model.json"}:
        path = model / name
        require(
            assessed["input_sha256"].get(
                path.relative_to(root).as_posix()
            ) == watched[path],
            "Time-budget assessment uses a different "
            "population snapshot: " + name,
        )

    hours = float(assessed["hours_per_week"])
    tolerance = float(assessed["tolerance_hours"])
    require(
        hours == 168
        and math.isfinite(tolerance)
        and 0 <= tolerance < 1e-4,
        "Unexpected time-budget threshold or numerical tolerance.",
    )
    limit = hours - tolerance

    scenarios = population["power_scenarios"]
    require(
        scenarios == ["reduced_test", "rated_ceiling_test"],
        "Unexpected power cases.",
    )

    cohorts = population["comparison_cohort_ids"]
    common = set(cohorts["all_energy_cases_common"])
    extension = set(cohorts["conditional_open_upper_extension"])
    require(
        not common & extension
        and set(cohorts["conditional_cases_common"])
        == common | extension,
        "Comparison cohorts overlap or differ.",
    )

    groups = {
        "common_energy_cases": (common, ALL_CASES),
        "open_upper_extension": (extension, ALL_CASES[1:]),
    }
    group_by_owner = {
        oid: name
        for name, (ids, cases) in groups.items()
        for oid in ids
    }

    owners = {r["owner_id"]: r for r in population["owners"]}
    require(
        len(owners) == len(population["owners"])
        == population["owners_retained"]
        and set(group_by_owner) <= set(owners),
        "Invalid owner links.",
    )

    n = population["draws_per_supported_owner_case"]
    require(
        type(n) is int and n > 0,
        "Invalid parameter draw count.",
    )

    expected = {
        (oid, case, str(i))
        for ids, cases in groups.values()
        for oid in ids
        for case in cases
        for i in range(1, n + 1)
    }

    for case in ALL_CASES:
        available = {
            oid for oid, record in owners.items()
            if record["case_component_status"][case][
                "components_available"
            ]
        }
        require(
            available == (
                common if case == ALL_CASES[0]
                else common | extension
            ),
            "Component coverage differs from comparison cohorts.",
        )

    original_rows = baseline.read_csv(
        model / "joint_owner_parameter_test_draws.csv"
    )
    source = unique_index(
        original_rows,
        ("owner_id", "energy_case", "parameter_draw"),
    )
    require(
        bool(source)
        and set(source) == expected
        and len(source) == population["joined_parameter_test_draws"]
        == assessed["source_joined_parameter_draws"],
        "Joined parameter grid differs.",
    )

    assessment_rows = baseline.read_csv(
        budget / "owner_time_budget_draws.csv"
    )
    assessments = unique_index(
        assessment_rows,
        (
            "owner_id", "energy_case",
            "parameter_draw", "power_scenario",
        ),
    )
    expected_assessments = {
        (*key, scenario)
        for key in expected
        for scenario in scenarios
    }
    require(
        set(assessments) == expected_assessments
        and len(assessments) == assessed["power_case_assessments"],
        "Assessment case/draw grid differs.",
    )

    for (oid, case, i, scenario), row in assessments.items():
        original = source[(oid, case, i)]
        low, expected_hours, high = (
            float(row[key])
            for key in (
                "minimum_weekly_hours",
                "expected_weekly_hours",
                "maximum_weekly_hours",
            )
        )
        require(
            all(
                math.isfinite(v) and v >= 0
                for v in (low, expected_hours, high)
            )
            and low <= expected_hours + tolerance
            and expected_hours <= high + tolerance,
            "Invalid time-budget bounds.",
        )
        require(
            row["cohort"] == group_by_owner[oid]
            and row["expected_no_spare_time"]
            == str(expected_hours >= limit)
            and row["expected_time_budget_exceeded"]
            == str(expected_hours > hours + tolerance),
            "Assessment flags/cohort differ from the declared rule.",
        )
        require(
            all(
                float(row[key]) == float(original[key])
                for key in (
                    "usual_energy_kwh", "total_weekly_rate"
                )
            )
            and row["exceeds_reported_battery_upper"]
            == original["exceeds_reported_battery_upper"],
            "Assessment and source parameter links differ.",
        )
        require(
            (
                not row["home_power_proxy_kw"].strip()
                and not original["home_power_proxy_kw"].strip()
            )
            or (
                bool(row["home_power_proxy_kw"].strip())
                and bool(original["home_power_proxy_kw"].strip())
                and float(row["home_power_proxy_kw"])
                == float(original["home_power_proxy_kw"])
            ),
            "Assessment and source home-power links differ.",
        )

    block_audit, owner_audit, group_summary = [], [], []
    admitted = {oid: [] for oid in group_by_owner}
    retained_maximum = defaultdict(list)

    for cohort, (ids, cases) in groups.items():
        for oid in sorted(ids):
            for i in range(1, n + 1):
                cells = [
                    assessments[(oid, case, str(i), scenario)]
                    for case in cases
                    for scenario in scenarios
                ]
                blocked = [
                    dict(
                        energy_case=row["energy_case"],
                        power_scenario=row["power_scenario"],
                        expected_weekly_hours=float(
                            row["expected_weekly_hours"]
                        ),
                    )
                    for row in cells
                    if float(row["expected_weekly_hours"]) >= limit
                ]

                passed = not blocked
                maximum = max(
                    float(row["expected_weekly_hours"])
                    for row in cells
                )

                if passed:
                    admitted[oid].append(i)
                    retained_maximum[cohort].append(maximum)

                block_audit.append(dict(
                    owner_id=oid,
                    cohort=cohort,
                    parameter_draw=i,
                    assessment_cells=len(cells),
                    maximum_expected_weekly_hours=maximum,
                    admitted=passed,
                    blocking_cases_json=json.dumps(blocked),
                ))

            owner_audit.append(dict(
                owner_id=oid,
                cohort=cohort,
                source_blocks=n,
                admitted_blocks=len(admitted[oid]),
                rejected_blocks=n - len(admitted[oid]),
                admission_percent=100 * len(admitted[oid]) / n,
                admitted_parameter_draws_json=json.dumps(
                    admitted[oid]
                ),
            ))

        if ids:
            total = n * len(ids)
            kept = sum(len(admitted[oid]) for oid in ids)

            group_summary.append(dict(
                cohort=cohort,
                source_owners=len(ids),
                owners_with_admitted_draws=sum(
                    bool(admitted[oid]) for oid in ids
                ),
                owners_without_admitted_draws=sum(
                    not admitted[oid] for oid in ids
                ),
                source_blocks=total,
                admitted_blocks=kept,
                rejected_blocks=total - kept,
                admission_percent=100 * kept / total,
                all_owners_have_admitted_draws=all(
                    admitted[oid] for oid in ids
                ),
                maximum_retained_expected_weekly_hours=max(
                    retained_maximum[cohort], default=None
                ),
            ))

    retained = []

    for (oid, case, i), row in source.items():
        if int(i) in admitted[oid]:
            retained.append(dict(
                row,
                screen_cohort=group_by_owner[oid],
                within_owner_draw_probability=(
                    1 / len(admitted[oid])
                ),
            ))

    require(
        len(retained) == sum(
            len(admitted[oid]) * len(cases)
            for ids, cases in groups.values()
            for oid in ids
        ),
        "Retained case pairing failed.",
    )

    changes = distribution_summary(
        source, admitted, groups, n
    )
    require(
        all(
            baseline.sha256(path) == old
            for path, old in watched.items()
        ),
        "Input changed during screening.",
    )

    output = allowed / (
        "time_screened_parameters_"
        + datetime.now(timezone.utc).strftime(
            "%Y%m%dT%H%M%S_%fZ"
        )
    )
    output.mkdir(parents=True, exist_ok=False)

    tables = {
        "time_screen_block_audit.csv": block_audit,
        "time_screen_owner_audit.csv": owner_audit,
        "time_screen_group_summary.csv": group_summary,
        "parameter_distribution_changes.csv": changes,
    }
    for name, rows in tables.items():
        write_csv(output / name, rows)

    with (
        output / "screened_parameter_draws.csv"
    ).open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=(
                *original_rows[0],
                "screen_cohort",
                "within_owner_draw_probability",
            ),
        )
        writer.writeheader()
        writer.writerows(retained)

    blocked_owners = sorted(
        oid for oid, indices in admitted.items()
        if not indices
    )

    report = dict(
        schema_version="paired_time_screen_sensitivity_v1",
        status="screening_sensitivity_prepared",
        data_origin="SYNTHETIC",
        behaviour_input_origin="SYNTHETIC",
        output_origin="COMPUTED_PARAMETER_MODEL",
        source_population_folder=model.relative_to(root).as_posix(),
        source_budget_folder=budget.relative_to(root).as_posix(),
        source_retained_owner_records=population["owners_retained"],
        screened_cohort_owners=len(group_by_owner),
        source_parameter_rows=len(source),
        source_owner_draw_blocks=len(block_audit),
        retained_parameter_rows=len(retained),
        admitted_owner_draw_blocks=sum(
            map(len, admitted.values())
        ),
        group_summary=group_summary,
        owner_screens=owner_audit,
        owners_without_admitted_draws=blocked_owners,
        all_cohort_owners_have_admitted_draws=not blocked_owners,
        threshold_hours=hours,
        numerical_tolerance_hours=tolerance,
        rule=(
            "Admit an owner/draw block only if expected weekly "
            "charging time is below 168 hours minus numerical "
            "tolerance in every required energy/power case."
        ),
        interpretation=[
            (
                "This is a separate screening sensitivity using "
                "the existing constant-power proxy assumptions."
            ),
            (
                "The screen conditions jointly on all candidate "
                "cases: six cells per common-cohort block and four "
                "per extension block. It is not each family's "
                "separate conditional distribution."
            ),
            (
                "Within each owner, the admitted finite set "
                "approximates a constrained parameter law; it is "
                "not a fitted dependence model or "
                "parameter-estimation uncertainty."
            ),
            (
                "The finite bank reuses the saved proposal indices; "
                "its size does not establish convergence to a "
                "continuous constrained sampling law."
            ),
            (
                "Retain one test vehicle per source owner and "
                "choose an admitted index uniformly within that "
                "owner, using the same index across compared cases. "
                "Never sample owners by flattening the retained "
                "row pool."
            ),
            (
                "The distribution-change table averages within "
                "owners first, then gives each source owner equal "
                "weight. After-screen means are blank if any owner "
                "in that cohort has no admitted draw."
            ),
            (
                "An owner with no admitted index stays in the audit "
                "and blocks a pilot for the full cohort; it is not "
                "silently dropped or imputed."
            ),
            (
                "Admitted values and battery flags are copied "
                "exactly; excluded blocks remain in the audit "
                "and original source package."
            ),
            (
                "This necessary mean-time screen does not establish "
                "actual scheduling feasibility, completed-frequency "
                "agreement, battery compatibility or an observed "
                "electrical boundary."
            ),
            (
                "The 168-hour allowance is optimistic: driving, dwell "
                "availability, acceptance limits, taper and station "
                "constraints require further modelling."
            ),
            (
                "The selected source cohorts and equal-owner test "
                "weights do not estimate national fleet composition."
            ),
        ],
        new_random_draws=0,
        new_demand_simulations=0,
        retained_parameter_values_modified=False,
        original_owner_eligibility_changed=False,
        simulator_activation=False,
        population_generation_ready=False,
        district_demand_ready=False,
        empirical_validation=False,
        checks=dict(
            source_manifests_match=True,
            shared_population_snapshot_verified=True,
            complete_case_draw_grids_verified=True,
            assessment_flags_checked=True,
            paired_block_accounting_checked=True,
            owner_balanced_summaries_used=True,
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
        "Input changed during export.",
    )
    (output / "time_screen_sensitivity.json").write_text(
        json.dumps(
            report, indent=2,
            ensure_ascii=False, allow_nan=False,
        ) + "\n",
        encoding="utf-8",
    )

    return output, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--budget", required=True)
    args = parser.parse_args()

    output, report = build_sensitivity(
        args.model, args.budget
    )

    print("Paired time-budget screening sensitivity complete.")
    print(
        f"source_retained_owner_records: "
        f"{report['source_retained_owner_records']}"
    )
    print(
        f"screened_cohort_owners: "
        f"{report['screened_cohort_owners']}"
    )

    for row in report["group_summary"]:
        print(
            f"{row['cohort']}: "
            f"owners={row['source_owners']}, "
            f"admitted_blocks={row['admitted_blocks']}/"
            f"{row['source_blocks']}, "
            f"admission={row['admission_percent']:.2f}%, "
            f"owners_without_draws="
            f"{row['owners_without_admitted_draws']}"
        )

    print(
        f"retained_parameter_rows: "
        f"{report['retained_parameter_rows']}"
    )
    print(
        f"all_cohort_owners_have_admitted_draws: "
        f"{report['all_cohort_owners_have_admitted_draws']}"
    )
    print(
        "Source, assessment-grid, paired-block and "
        "owner-weighting checks passed."
    )
    print(f"Output: {output.relative_to(ROOT)}")
    print(
        "Separate screening sensitivity; no new random draws "
        "or demand simulations; scheduling remains unverified."
    )


if __name__ == "__main__":
    try:
        main()
    except (
        ValueError, OSError, KeyError, TypeError, RuntimeError
    ) as exc:
        print(
            f"Time-screen sensitivity failed: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1)