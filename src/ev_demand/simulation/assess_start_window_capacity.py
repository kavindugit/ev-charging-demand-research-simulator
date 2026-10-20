"""Audit necessary start-window capacity bounds for a saved joint-parameter pilot."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from fractions import Fraction
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from ev_demand.parameters import build_simulator_parameters as baseline
from ev_demand.parameters.build_population_evidence import CONTEXTS, require
from ev_demand.parameters.assess_owner_time_budget import inverse_power_bounds
from ev_demand.simulation.run_session_simulator import close, write_csv

SOURCE_FILES = {
    "sessions.csv",
    "rejected_sessions.csv",
    "selected_owner_parameters.csv",
    "owner_frequency_diagnostics.csv",
    "replicate_summaries.csv",
    "frequency_summary.csv",
    "replicate_profiles.csv",
    "mean_profile_24h.csv",
}

PAD = Fraction(1, 100_000_000)
COUNT_TOLERANCE = 1e-8


def optimistic_capacity(minimum_duration, window, days, expected_starts):
    """Find a conservative capacity bound, stopping when the target is reached."""
    duration = float(minimum_duration)
    start = float(window["start_hour"])
    length = float(window["length_hours"])

    require(
        math.isfinite(duration) and duration > 0
        and math.isfinite(start) and 0 <= start < 24
        and math.isfinite(length) and 0 < length <= 24
        and type(days) is int and days > 0
        and math.isfinite(expected_starts) and expected_starts > 0,
        "Invalid capacity inputs.",
    )

    # Relax constraints slightly so numerical rounding cannot create
    # an artificially restrictive capacity bound.
    raw_duration = Fraction(duration)
    step = max(raw_duration / 2, raw_duration - PAD)
    window_start = (Fraction(start) - PAD) % 24
    window_length = min(Fraction(24), Fraction(length) + 2 * PAD)

    horizon = Fraction(24 * days) + PAD
    ready = Fraction(0)
    goal = math.ceil(expected_starts)
    found = 0
    first, last = None, None

    while found < goal:
        offset = (ready - window_start) % 24
        actual = (
            ready
            if offset <= window_length
            else ready + 24 - offset
        )

        if actual > horizon:
            break

        require(
            actual >= ready and actual + step > actual,
            "Capacity clock did not advance.",
        )

        if first is None:
            first = float(actual)

        last = float(actual)
        found += 1
        ready = actual + step

    exhausted = found < goal
    upper = found if exhausted else None

    failed = (
        upper is not None
        and expected_starts > upper + COUNT_TOLERANCE
    )

    return dict(
        optimistic_starts_found_up_to_target=found,
        capacity_search_exhausted=exhausted,
        optimistic_maximum_starts=upper,
        optimistic_maximum_weekly_rate=(
            upper * 7 / days if upper is not None else None
        ),
        context_target_exceeds_optimistic_capacity=failed,
        minimum_expected_context_start_shortfall=(
            max(0.0, expected_starts - upper) if failed else 0.0
        ),
        first_optimistic_start_hour=first,
        last_optimistic_start_hour=last,
        duration_for_optimistic_bound_hours=float(step),
    )


def audit(pilot_folder, project_root=ROOT):
    root = Path(project_root).resolve()
    watched = {}

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

    pilot = (root / pilot_folder).resolve()

    require(
        pilot.is_relative_to(root / "data/simulated/synthetic")
        and pilot.is_dir(),
        "Use a completed synthetic joint-parameter pilot folder.",
    )

    source = json.loads(
        watch(pilot / "joint_parameter_pilot_report.json").read_text(
            encoding="utf-8-sig"
        )
    )

    require(
        source.get("schema_version")
        == "joint_parameter_scheduling_pilot_v1"
        and source.get("status") == "complete"
        and source.get("data_origin") == "SYNTHETIC"
        and source.get("output_origin") == "SIMULATED"
        and bool(source.get("checks"))
        and all(v is True for v in source["checks"].values()),
        "Unexpected or incomplete source pilot.",
    )

    require(
        set(source["output_sha256"]) == SOURCE_FILES,
        "Unexpected source export manifest.",
    )

    for name, digest in source["output_sha256"].items():
        watch(pilot / name, digest)

    for path, digest in source["input_sha256"].items():
        watch(path, digest)

    watch("src/ev_demand/simulation/assess_start_window_capacity.py")

    model_path = watch(
        Path(source["model_folder"]) / "behaviour_population_model.json"
    )

    require(
        model_path.relative_to(root).as_posix() in source["input_sha256"],
        "The population snapshot was not recorded in the pilot.",
    )

    population = json.loads(
        model_path.read_text(encoding="utf-8-sig")
    )

    owners = {r["owner_id"]: r for r in population["owners"]}
    ids = source["owner_ids"]
    days = source["study_days"]
    replicates = source["replicates"]

    require(
        ids
        and len(ids) == len(set(ids)) == source["owners_simulated"]
        and type(days) is int and 1 <= days <= 56
        and type(replicates) is int and 2 <= replicates <= 20,
        "Invalid pilot settings.",
    )

    selected = baseline.read_csv(
        pilot / "selected_owner_parameters.csv"
    )

    keys = {
        (int(r["replicate"]), r["owner_id"])
        for r in selected
    }

    require(
        len(keys) == len(selected) == len(ids) * replicates
        and keys == {
            (r, oid)
            for r in range(1, replicates + 1)
            for oid in ids
        },
        "Incomplete selected-owner parameter grid.",
    )

    frequency = baseline.read_csv(
        pilot / "owner_frequency_diagnostics.csv"
    )

    targets = {
        (int(r["replicate"]), r["owner_id"], r["context"]): r
        for r in frequency
    }

    require(
        len(targets) == len(frequency) == len(keys) * 5
        and set(targets) == {
            (r, oid, c)
            for r, oid in keys
            for c in (*CONTEXTS, "TOTAL")
        },
        "Incomplete pilot frequency grid.",
    )

    context_rows, owner_rows = [], []

    for parameter in sorted(
        selected,
        key=lambda r: (int(r["replicate"]), r["owner_id"]),
    ):
        rep = int(parameter["replicate"])
        oid = parameter["owner_id"]
        energy = float(parameter["usual_energy_kwh"])
        total = float(parameter["total_weekly_rate"])

        rates = {
            c: float(parameter[c + "_weekly_rate"])
            for c in CONTEXTS
        }

        require(
            math.isfinite(energy) and energy > 0
            and math.isfinite(total) and total > 0
            and all(math.isfinite(v) and v >= 0 for v in rates.values())
            and close(math.fsum(rates.values()), total),
            "Invalid selected owner vector.",
        )

        home = (
            float(parameter["home_power_proxy_kw"])
            if parameter["home_power_proxy_kw"].strip()
            else None
        )

        contexts = {
            r["legacy_context"]: r
            for r in owners[oid]["contexts"]
        }

        require(
            set(contexts) == set(CONTEXTS),
            "Incomplete owner context model.",
        )

        expected_hours = 0.0
        failures = []
        selected_context_rows = []

        for code in CONTEXTS:
            record = targets[(rep, oid, code)]
            target = rates[code] * days / 7
            accepted = int(record["study_accepted_starts"])

            require(
                close(
                    float(record["target_expected_study_starts"]),
                    target,
                )
                and close(
                    float(record["target_weekly_completed_rate"]),
                    rates[code],
                ),
                "Saved target differs from the selected joint rate.",
            )

            if rates[code] == 0:
                require(
                    accepted == 0
                    and int(record["study_candidate_starts"]) == 0,
                    "A zero-rate context has pilot starts.",
                )

                bounds = dict(
                    optimistic_starts_found_up_to_target=0,
                    capacity_search_exhausted=False,
                    optimistic_maximum_starts=None,
                    optimistic_maximum_weekly_rate=None,
                    context_target_exceeds_optimistic_capacity=False,
                    minimum_expected_context_start_shortfall=0.0,
                    first_optimistic_start_hour=None,
                    last_optimistic_start_hour=None,
                    duration_for_optimistic_bound_hours=None,
                )

                duration = None
                expected_context_hours = 0.0
                fastest = None

            else:
                context = contexts[code]

                require(
                    context["use_status"] != "reported_nonuse"
                    and context["power_status"] == "supported_test"
                    and context["timing_rule"] is not None,
                    "Positive use lacks a supported timing/power model.",
                )

                inverse = inverse_power_bounds(
                    context["power_models"][source["power_scenario"]],
                    home,
                )

                duration = energy * inverse[0]
                fastest = 1 / inverse[0]
                expected_context_hours = (
                    rates[code] * energy * inverse[1]
                )

                bounds = optimistic_capacity(
                    duration,
                    context["timing_rule"],
                    days,
                    target,
                )

                if bounds["optimistic_maximum_starts"] is not None:
                    require(
                        accepted <= bounds["optimistic_maximum_starts"],
                        "Observed pilot starts exceed their "
                        "optimistic capacity bound.",
                    )

                if bounds["context_target_exceeds_optimistic_capacity"]:
                    failures.append(code)

            expected_hours += expected_context_hours

            row = dict(
                replicate=rep,
                owner_id=oid,
                parameter_draw=int(parameter["parameter_draw"]),
                context=code,
                weekly_target_rate=rates[code],
                target_expected_study_starts=target,
                control_accepted_study_starts=accepted,
                usual_energy_kwh=energy,
                fastest_supported_power_kw=fastest,
                minimum_session_duration_hours=duration,
                expected_weekly_context_hours=expected_context_hours,
                **bounds,
            )

            context_rows.append(row)
            selected_context_rows.append(row)

        require(
            close(
                expected_hours,
                float(parameter["expected_weekly_charging_hours"]),
            )
            and close(
                float(
                    targets[(rep, oid, "TOTAL")][
                        "target_expected_study_starts"
                    ]
                ),
                total * days / 7,
            ),
            "Saved owner time budget or total target differs.",
        )

        owner_rows.append(dict(
            replicate=rep,
            owner_id=oid,
            parameter_draw=int(parameter["parameter_draw"]),
            energy_case=source["energy_case"],
            power_scenario=source["power_scenario"],
            total_weekly_target_rate=total,
            expected_weekly_charging_hours=expected_hours,
            any_context_capacity_bound_failed=bool(failures),
            failed_contexts_json=json.dumps(failures),
            sum_context_target_shortfall_lower_bounds=math.fsum(
                r["minimum_expected_context_start_shortfall"]
                for r in selected_context_rows
            ),
            status=(
                "context_capacity_bound_failed"
                if failures
                else "necessary_checks_only_no_bound_failure"
            ),
        ))

    summaries = []

    for code in (*CONTEXTS, "ANY_CONTEXT"):
        rows = (
            owner_rows
            if code == "ANY_CONTEXT"
            else [r for r in context_rows if r["context"] == code]
        )

        active = (
            rows
            if code == "ANY_CONTEXT"
            else [r for r in rows if r["weekly_target_rate"] > 0]
        )

        field = (
            "any_context_capacity_bound_failed"
            if code == "ANY_CONTEXT"
            else "context_target_exceeds_optimistic_capacity"
        )

        failed = [r for r in active if r[field]]

        summaries.append(dict(
            context=code,
            owner_parameter_selections=len(rows),
            active_owner_parameter_selections=len(active),
            selections_with_capacity_bound_failure=len(failed),
            failure_percent_of_active=(
                100 * len(failed) / len(active)
                if active else None
            ),
            owners_with_any_failure=len({
                r["owner_id"] for r in failed
            }),
        ))

    owner_summary = []

    for oid in ids:
        rows = [r for r in owner_rows if r["owner_id"] == oid]
        failed = [
            r for r in rows
            if r["any_context_capacity_bound_failed"]
        ]

        owner_summary.append(dict(
            owner_id=oid,
            selections_reviewed=len(rows),
            selections_with_bound_failure=len(failed),
            percent_selected_vectors_with_bound_failure=(
                100 * len(failed) / len(rows)
            ),
            failed_contexts_json=json.dumps(sorted({
                c
                for r in failed
                for c in json.loads(r["failed_contexts_json"])
            })),
        ))

    require(
        len(context_rows) == len(keys) * 4
        and len(owner_rows) == len(keys)
        and all(
            baseline.sha256(p) == digest
            for p, digest in watched.items()
        ),
        "Audit accounting or input integrity failed.",
    )

    output = (
        root / "data/processed/synthetic"
        / (
            "start_window_capacity_"
            + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        )
    )

    output.mkdir(parents=True, exist_ok=False)

    for name, rows in (
        ("context_capacity_audit.csv", context_rows),
        ("owner_parameter_capacity_audit.csv", owner_rows),
        ("capacity_summary.csv", summaries),
        ("owner_capacity_review.csv", owner_summary),
    ):
        write_csv(output / name, rows, tuple(rows[0]))

    require(
        all(
            baseline.sha256(p) == digest
            for p, digest in watched.items()
        ),
        "An input changed during export.",
    )

    report = dict(
        schema_version="start_window_capacity_audit_v1",
        status="complete",
        data_origin="SYNTHETIC",
        source_pilot=pilot.relative_to(root).as_posix(),
        owners_reviewed=len(ids),
        owner_parameter_selections=len(keys),
        unique_owner_parameter_indices=len({
            (r["owner_id"], int(r["parameter_draw"]))
            for r in selected
        }),
        context_assessments=len(context_rows),
        study_days=days,
        source_warmup_days=source["warmup_days"],
        source_replicates=replicates,
        cohort=source["cohort"],
        energy_case=source["energy_case"],
        power_scenario=source["power_scenario"],
        summary=summaries,
        numerical_relaxation_hours=float(PAD),
        expected_count_comparison_tolerance=COUNT_TOLERANCE,
        new_random_draws=0,
        new_demand_simulations=0,
        owner_eligibility_changed=False,
        parameters_changed=False,
        joint_schedulability_established=False,
        completed_frequency_agreement_established=False,
        empirical_validation=False,
        district_demand_ready=False,
        interpretation=[
            (
                "Necessary model-bound test for the saved selected vectors; "
                "no owner or parameter is excluded or redrawn."
            ),
            (
                "Test one context at a time, ignoring all other charging "
                "and starting with an empty calendar at study hour zero."
            ),
            (
                "Use the fastest positive-probability power support bound "
                "every session; use fixed selected home power for T2."
            ),
            (
                "Usual owner energy stays fixed. Start windows repeat daily "
                "and constrain starts; charging may end after a window "
                "or study end."
            ),
            (
                "For equal durations, the earliest permissible next start "
                "minimizes every subsequent start by induction, giving "
                "the maximum count in this relaxed single-context model."
            ),
            (
                "Window endpoints and the study endpoint are relaxed "
                "outward by a tiny tolerance; duration is shortened slightly. "
                "Fraction clocks avoid cumulative rounding. "
                "A failed bound is conservative."
            ),
            (
                "Stop when ceil(expected target) starts are constructed. "
                "A blank maximum means the upper bound was not exhausted, "
                "not zero capacity."
            ),
            (
                "A failed target cannot match its expected study-start "
                "count under these model assumptions, even before "
                "competing contexts are scheduled."
            ),
            (
                "Passing does not establish joint feasibility, typical "
                "performance under sampled power, or long-run stationarity. "
                "No demand profile is generated."
            ),
            (
                "Failures concern hard-window and constant-energy/power "
                "proxy assumptions, not proof that an owner's answers "
                "or real behaviour are invalid."
            ),
            (
                "Summed context shortfall bounds are target-vector "
                "discrepancies; they are not a forecast of unmet energy "
                "or a total-count loss if other contexts exceed target."
            ),
            (
                "Repeated parameter indices remain repeated selections; "
                "failure percentages describe this saved pilot, "
                "not independent respondents or Sri Lankan prevalence."
            ),
        ],
        checks=dict(
            source_hashes_match=True,
            owner_parameter_grid_verified=True,
            reported_target_rates_verified=True,
            power_support_and_time_budgets_verified=True,
            control_counts_respect_exhausted_bounds=True,
            context_and_owner_accounting_checked=True,
            input_hashes_unchanged=True,
        ),
        input_sha256={
            p.relative_to(root).as_posix(): digest
            for p, digest in watched.items()
        },
        output_sha256={
            p.name: baseline.sha256(p)
            for p in sorted(output.iterdir())
        },
    )

    (output / "start_window_capacity_report.json").write_text(
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
    parser.add_argument("--pilot", required=True)
    args = parser.parse_args()

    output, report = audit(args.pilot)

    print("Necessary start-window capacity audit complete.")
    print(f"owners_reviewed: {report['owners_reviewed']}")
    print(
        "owner_parameter_selections: "
        f"{report['owner_parameter_selections']}"
    )
    print(f"context_assessments: {report['context_assessments']}")

    for row in report["summary"]:
        print(
            f"{row['context']}: failed_selections="
            f"{row['selections_with_capacity_bound_failure']}/"
            f"{row['active_owner_parameter_selections']}, "
            f"affected_owners={row['owners_with_any_failure']}"
        )

    print(
        "Source, target, power-support "
        "and capacity-accounting checks passed."
    )
    print(f"Output: {output.relative_to(ROOT)}")
    print(
        "Necessary-bound audit only; no parameter changes, exclusions, "
        "new simulations or empirical validation."
    )


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError, RuntimeError) as exc:
        print(
            f"Start-window capacity audit failed: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1)