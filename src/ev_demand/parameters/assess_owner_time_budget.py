"""Assess weekly charging-time requirements under the saved proxy models."""
from __future__ import annotations

import argparse
from collections import defaultdict
import csv
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from statistics import median
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from ev_demand.parameters import build_simulator_parameters as baseline
from ev_demand.parameters.build_population_evidence import (
    CONTEXTS, require, write_csv,
)

HOURS_PER_WEEK = 168.0
TOLERANCE_HOURS = 1e-8

INPUT_FILES = {
    "population_owner_case_audit.csv",
    "population_context_audit.csv",
    "population_case_summary.csv",
    "joint_owner_parameter_test_draws.csv",
}

DRAW_FIELDS = (
    "owner_id", "cohort", "energy_case", "power_scenario",
    "parameter_draw", "total_weekly_rate", "usual_energy_kwh",
    "home_power_proxy_kw", "home_session_duration_hours",
    "minimum_weekly_hours", "expected_weekly_hours",
    "maximum_weekly_hours", "expected_busy_fraction",
    "best_case_time_budget_exceeded",
    "expected_time_budget_exceeded", "expected_no_spare_time",
    *(c + "_expected_weekly_hours" for c in CONTEXTS),
    "exceeds_reported_battery_upper",
)


def positive(value):
    require(
        not isinstance(value, bool),
        "A power value cannot be Boolean.",
    )
    value = float(value)
    require(
        math.isfinite(value) and value > 0,
        "Power must be finite and positive.",
    )
    return value


def inverse_power_bounds(model, home_power=None):
    """Return min/mean/max of 1/P in hours/kWh."""
    if model["kind"] == "reported_home_uniform":
        value = positive(home_power)
        low = positive(model["effective_average_kw_min"])
        high = positive(model["effective_average_kw_max"])
        require(
            low <= value <= high,
            "Saved home power lies outside its reported range.",
        )
        return 1 / value, 1 / value, 1 / value

    require(
        model["kind"] == "nonhome_rating_mixture",
        "Unsupported power model.",
    )

    parts = []
    probability_sum = 0.0

    for option in model["options"]:
        probability = float(option["selection_probability"])
        require(
            math.isfinite(probability) and probability >= 0,
            "Invalid rating probability.",
        )
        probability_sum += probability

        if probability == 0:
            continue

        rated = positive(option["rated_output_kw"])
        a = positive(option["power_fraction_min"])
        b = positive(option["power_fraction_max"])
        require(a <= b <= 1, "Invalid power fraction bounds.")

        low, high = rated * a, rated * b

        # Uniform power fraction gives uniform power within this option.
        mean_inverse = (
            1 / low
            if low == high
            else math.log1p((high - low) / low) / (high - low)
        )
        parts.append((probability, low, high, mean_inverse))

    require(
        parts
        and math.isclose(
            probability_sum, 1, abs_tol=1e-10, rel_tol=0
        ),
        "Rating probabilities must sum to one.",
    )

    return (
        1 / max(p[2] for p in parts),
        math.fsum(p[0] * p[3] for p in parts),
        1 / min(p[1] for p in parts),
    )


def weekly_time_bounds(energy, rates, inverse_powers):
    energy = positive(energy)
    require(
        set(rates) == set(inverse_powers) == set(CONTEXTS),
        "Incomplete context grid.",
    )

    context_hours = {}
    lower, expected, upper = [], [], []

    for code in CONTEXTS:
        rate = float(rates[code])
        require(
            math.isfinite(rate) and rate >= 0,
            "Invalid weekly rate.",
        )

        bounds = inverse_powers[code]
        require(
            len(bounds) == 3
            and all(math.isfinite(v) and v >= 0 for v in bounds)
            and bounds[0] <= bounds[1] <= bounds[2],
            "Invalid inverse-power bounds.",
        )
        require(
            rate == 0 or bounds[0] > 0,
            "Positive use requires a supported power model.",
        )

        hours = tuple(rate * energy * v for v in bounds)
        lower.append(hours[0])
        expected.append(hours[1])
        upper.append(hours[2])
        context_hours[code] = hours[1]

    totals = tuple(
        math.fsum(values)
        for values in (lower, expected, upper)
    )
    require(
        all(math.isfinite(v) for v in totals),
        "Nonfinite weekly time requirement.",
    )
    return totals, context_hours


def summarise(rows, by_owner=False):
    groups = defaultdict(list)
    keys = (
        "cohort", "energy_case", "power_scenario"
    ) + (("owner_id",) if by_owner else ())

    for row in rows:
        groups[tuple(row[k] for k in keys)].append(row)

    summaries = []

    for key, values in sorted(groups.items()):
        best = [
            r for r in values
            if r["best_case_time_budget_exceeded"]
        ]
        over = [
            r for r in values
            if r["expected_time_budget_exceeded"]
        ]
        no_spare = [
            r for r in values
            if r["expected_no_spare_time"]
        ]

        summaries.append(dict(
            zip(keys, key),
            owners=len({r["owner_id"] for r in values}),
            parameter_draws=len(values),
            best_case_exceeded_draws=len(best),
            expected_exceeded_draws=len(over),
            expected_no_spare_time_draws=len(no_spare),
            best_case_exceeded_percent=100 * len(best) / len(values),
            expected_exceeded_percent=100 * len(over) / len(values),
            owners_with_any_best_case_exceeded=len({
                r["owner_id"] for r in best
            }),
            owners_with_any_expected_exceeded=len({
                r["owner_id"] for r in over
            }),
            median_expected_busy_fraction=median(
                r["expected_busy_fraction"] for r in values
            ),
            maximum_expected_weekly_hours=max(
                r["expected_weekly_hours"] for r in values
            ),
        ))

    return summaries


def assess(model_folder, project_root=ROOT):
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

    for name in (
        "docs/research_pipeline_specification.md",
        "src/ev_demand/parameters/build_simulator_parameters.py",
        "src/ev_demand/parameters/build_population_evidence.py",
        "src/ev_demand/parameters/assess_owner_time_budget.py",
    ):
        watch(name)

    folder = (root / model_folder).resolve()
    require(
        folder.is_relative_to(allowed) and folder.is_dir(),
        "Use the assembled synthetic population folder.",
    )

    source = json.loads(
        watch(
            folder / "behaviour_population_model.json"
        ).read_text(encoding="utf-8-sig")
    )

    require(
        source.get("schema_version") == "behaviour_population_model_v1"
        and source.get("status") == "components_assembled_for_review"
        and source.get("data_origin") == "SYNTHETIC"
        and source.get("context_labels") == CONTEXTS,
        "Unexpected population parameter package.",
    )
    require(
        bool(source.get("checks"))
        and all(v is True for v in source["checks"].values()),
        "An upstream assembly check failed.",
    )
    require(
        set(source["output_sha256"]) == INPUT_FILES,
        "Unexpected assembly export manifest.",
    )

    for name in sorted(INPUT_FILES):
        path = watch(folder / name)
        require(
            watched[path] == source["output_sha256"][name],
            "Export hash mismatch: " + name,
        )

    cases = {r["energy_case"] for r in source["case_summary"]}
    require(
        cases == {
            "bounded_uniform_test",
            "conditional_gamma_test",
            "conditional_lognormal_test",
        },
        "Unexpected energy cases.",
    )

    scenarios = source["power_scenarios"]
    require(
        scenarios == ["reduced_test", "rated_ceiling_test"],
        "Unexpected power cases.",
    )

    owners = {r["owner_id"]: r for r in source["owners"]}
    require(
        len(owners) == len(source["owners"])
        == source["owners_retained"],
        "Invalid owner grid.",
    )

    covered = {
        case: {
            oid for oid, record in owners.items()
            if record["case_component_status"][case][
                "components_available"
            ]
        }
        for case in cases
    }

    common = set.intersection(*covered.values())
    conditional = (
        covered["conditional_gamma_test"]
        & covered["conditional_lognormal_test"]
    )
    extension = conditional - common
    cohorts = source["comparison_cohort_ids"]

    require(
        set(cohorts["all_energy_cases_common"]) == common
        and set(cohorts["conditional_cases_common"]) == conditional
        and set(
            cohorts["conditional_open_upper_extension"]
        ) == extension,
        "Comparison cohort definitions differ.",
    )
    require(
        all(ids <= common | extension for ids in covered.values()),
        "Unsupported comparison cohort.",
    )

    n = source["draws_per_supported_owner_case"]
    require(
        type(n) is int and n > 0,
        "Invalid source parameter draw count.",
    )

    input_rows = baseline.read_csv(
        folder / "joint_owner_parameter_test_draws.csv"
    )
    keys = [
        (
            r["owner_id"],
            r["energy_case"],
            int(r["parameter_draw"]),
        )
        for r in input_rows
    ]
    expected = {
        (oid, case, i)
        for case, owner_ids in covered.items()
        for oid in owner_ids
        for i in range(1, n + 1)
    }

    require(
        len(keys) == len(set(keys))
        == source["joined_parameter_test_draws"]
        and set(keys) == expected,
        "Incomplete or duplicate joined parameter grid.",
    )
    require(
        bool(input_rows),
        "No complete parameter draws are available "
        "for a time-budget assessment.",
    )

    contexts = {
        oid: {
            c["legacy_context"]: c
            for c in owner["contexts"]
        }
        for oid, owner in owners.items()
    }

    rows = []

    for row in input_rows:
        oid, case = row["owner_id"], row["energy_case"]
        linked = contexts[oid]
        require(
            set(linked) == set(CONTEXTS),
            "Incomplete owner context links.",
        )

        energy = positive(row["usual_energy_kwh"])
        total = positive(row["total_weekly_rate"])
        rates = {
            c: float(row[c + "_weekly_rate"])
            for c in CONTEXTS
        }

        require(
            all(math.isfinite(v) and v >= 0 for v in rates.values())
            and math.isclose(
                math.fsum(rates.values()), total,
                abs_tol=1e-8, rel_tol=1e-10,
            ),
            "Total/context weekly rates differ.",
        )

        home = (
            positive(row["home_power_proxy_kw"])
            if row["home_power_proxy_kw"].strip()
            else None
        )

        for scenario in scenarios:
            inverses = {}

            for code, context in linked.items():
                if context["use_status"] == "reported_nonuse":
                    require(
                        rates[code] == 0,
                        "Reported nonuse has positive sampled frequency.",
                    )
                    inverses[code] = (0.0, 0.0, 0.0)
                else:
                    require(
                        context["use_status"] == "use_category"
                        and context["power_status"] == "supported_test",
                        "A complete parameter draw lacks "
                        "supported context power.",
                    )
                    inverses[code] = inverse_power_bounds(
                        context["power_models"][scenario], home
                    )

            (low, mean, high), by_context = weekly_time_bounds(
                energy, rates, inverses
            )
            require(
                low <= mean + TOLERANCE_HOURS
                and mean <= high + TOLERANCE_HOURS,
                "Expected time lies outside its power-support bounds.",
            )

            rows.append(dict(
                owner_id=oid,
                cohort=(
                    "common_energy_cases"
                    if oid in common
                    else "open_upper_extension"
                ),
                energy_case=case,
                power_scenario=scenario,
                parameter_draw=int(row["parameter_draw"]),
                total_weekly_rate=total,
                usual_energy_kwh=energy,
                home_power_proxy_kw=home,
                home_session_duration_hours=(
                    energy / home if home is not None else None
                ),
                minimum_weekly_hours=low,
                expected_weekly_hours=mean,
                maximum_weekly_hours=high,
                expected_busy_fraction=mean / HOURS_PER_WEEK,
                best_case_time_budget_exceeded=(
                    low > HOURS_PER_WEEK + TOLERANCE_HOURS
                ),
                expected_time_budget_exceeded=(
                    mean > HOURS_PER_WEEK + TOLERANCE_HOURS
                ),
                expected_no_spare_time=(
                    mean >= HOURS_PER_WEEK - TOLERANCE_HOURS
                ),
                **{
                    c + "_expected_weekly_hours": by_context[c]
                    for c in CONTEXTS
                },
                exceeds_reported_battery_upper=(
                    row["exceeds_reported_battery_upper"]
                ),
            ))

    require(
        len(rows) == len(scenarios) * len(input_rows),
        "Assessment row accounting failed.",
    )

    case_summary = summarise(rows)
    owner_summary = summarise(rows, True)

    require(
        all(
            baseline.sha256(p) == old
            for p, old in watched.items()
        ),
        "Input changed during assessment.",
    )

    output = allowed / (
        "owner_time_budget_"
        + datetime.now(timezone.utc).strftime(
            "%Y%m%dT%H%M%S_%fZ"
        )
    )
    output.mkdir(parents=True, exist_ok=False)

    with (
        output / "owner_time_budget_draws.csv"
    ).open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=DRAW_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    write_csv(
        output / "time_budget_case_summary.csv",
        case_summary,
    )
    write_csv(
        output / "time_budget_owner_summary.csv",
        owner_summary,
    )

    report = dict(
        schema_version="owner_time_budget_assessment_v1",
        status="proxy_time_budget_assessed",
        data_origin="SYNTHETIC",
        behaviour_input_origin="SYNTHETIC",
        output_origin="COMPUTED_PARAMETER_DIAGNOSTIC",
        retained_source_owners=len(owners),
        assessed_owners=len({r["owner_id"] for r in rows}),
        source_joined_parameter_draws=len(input_rows),
        power_case_assessments=len(rows),
        hours_per_week=HOURS_PER_WEEK,
        tolerance_hours=TOLERANCE_HOURS,
        case_summary=case_summary,
        new_random_draws=0,
        new_demand_simulations=0,
        method=dict(
            vehicle_unit=(
                "One modelled vehicle per owner record; charging "
                "contexts cannot overlap for that vehicle."
            ),
            time_budget=(
                "Sum over contexts of completed weekly rate times "
                "usual session energy times inverse power."
            ),
            power_expectation=(
                "Non-home service time uses the weighted expectation "
                "of 1/P, not 1 divided by mean power. Saved home "
                "power is fixed per owner/draw."
            ),
            minimum=(
                "Fastest positive-probability power support in each "
                "context; a lower bound on required weekly time."
            ),
            maximum=(
                "Slowest positive-probability power support in each "
                "context; an upper support bound, not a predicted week."
            ),
            interpretation=(
                "These are long-run rate/time requirements under the "
                "proxy assumptions, not finite calendar-week counts "
                "or observed charging hours."
            ),
        ),
        limits=[
            (
                "A best-case requirement above 168 hours/week cannot "
                "fit the one-vehicle no-overlap model under these "
                "energy/power assumptions."
            ),
            (
                "Expected time above 168 hours/week conflicts with "
                "the target completed rates and assumed per-session "
                "power laws; equality leaves no spare time."
            ),
            (
                "Passing this necessary time-budget check does not "
                "establish start-window schedulability, frequency "
                "reproduction, stationarity or physical compatibility."
            ),
            (
                "Reported kWh and constant-power proxies are used "
                "without extra losses or battery clipping; "
                "meter-versus-battery interpretation remains unresolved."
            ),
            (
                "Vehicle acceptance limits, connectors, taper, dwell "
                "availability and station constraints are not verified."
            ),
            (
                "Percentages describe saved parameter-test combinations "
                "within each cohort, not Sri Lankan owner prevalence "
                "or Monte Carlo mean precision."
            ),
            (
                "No parameter combination is dropped, repaired or "
                "redrawn; battery review flags and archived inputs "
                "are preserved."
            ),
        ],
        owner_eligibility_changed=False,
        parameter_values_changed=False,
        simulator_activation=False,
        district_demand_ready=False,
        empirical_validation=False,
        checks=dict(
            source_export_hashes_match=True,
            comparison_cohorts_verified=True,
            complete_parameter_grid_verified=True,
            context_rate_totals_checked=True,
            inverse_power_support_checked=True,
            assessment_accounting_checked=True,
            input_hashes_unchanged=True,
        ),
        input_sha256={
            p.relative_to(root).as_posix(): old
            for p, old in watched.items()
        },
        output_sha256={
            p.name: baseline.sha256(p)
            for p in sorted(output.iterdir())
        },
    )

    require(
        all(
            baseline.sha256(p) == old
            for p, old in watched.items()
        ),
        "Input changed during export.",
    )
    (output / "owner_time_budget_report.json").write_text(
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
    args = parser.parse_args()

    output, report = assess(args.model)

    print("Owner weekly time-budget assessment complete.")
    print(f"assessed_owners: {report['assessed_owners']}")
    print(
        f"source_joined_parameter_draws: "
        f"{report['source_joined_parameter_draws']}"
    )
    print(
        f"power_case_assessments: "
        f"{report['power_case_assessments']}"
    )

    for row in report["case_summary"]:
        print(
            f"{row['cohort']} | {row['energy_case']} | "
            f"{row['power_scenario']}: "
            f"owners={row['owners']}, "
            f"best_case_exceeded="
            f"{row['best_case_exceeded_percent']:.2f}%, "
            f"expected_exceeded="
            f"{row['expected_exceeded_percent']:.2f}%, "
            f"maximum_expected_hours="
            f"{row['maximum_expected_weekly_hours']:.3f}"
        )

    print(
        "Source, cohort, rate-total and time-budget "
        "accounting checks passed."
    )
    print(f"Output: {output.relative_to(ROOT)}")
    print(
        "Conditional proxy diagnostics only; no redraws, "
        "exclusions or demand simulations."
    )


if __name__ == "__main__":
    try:
        main()
    except (
        ValueError, OSError, KeyError, TypeError, RuntimeError
    ) as exc:
        print(
            f"Owner time-budget assessment failed: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1)