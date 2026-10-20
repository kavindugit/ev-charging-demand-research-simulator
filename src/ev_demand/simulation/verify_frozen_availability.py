"""Independently check a pilot while keeping selected parameters and idle means fixed."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from statistics import mean, stdev, variance
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from ev_demand.parameters import build_simulator_parameters as baseline
from ev_demand.parameters.build_population_evidence import CONTEXTS, require
from ev_demand.simulation.run_availability_generator_pilot import (
    EXTRA, GROUPS, events, make_owner, sequential_sessions, window_start,
)
from ev_demand.simulation.run_session_simulator import (
    ENERGY_FIELDS, SESSION_FIELDS, allocate_energy, close, write_csv,
)

PILOT_FILES = {
    "sessions.csv",
    "control_rejected_sessions.csv",
    "selected_owner_parameters.csv",
    "owner_calibration.csv",
    "owner_generator_diagnostics.csv",
    "replicate_comparison.csv",
    "replicate_profiles.csv",
    "mean_profile_24h.csv",
    "generator_summary.csv",
}
PREFIX_STAGE = "frozen-long-verification-v1"


def process_seed(base_seed, repeat):
    value = f"frozen-availability-verification-v1|{base_seed}|{repeat}"
    return int.from_bytes(
        hashlib.sha256(value.encode()).digest()[:8], "big"
    )


def long_prefix(owner, idle, seed, replicate, count):
    end, counts = 0.0, Counter()
    stream = events(owner, seed, replicate, PREFIX_STAGE)

    for _ in range(count):
        event = next(stream)
        ready = end + idle * event["unit_idle"]

        start = window_start(
            ready,
            owner["contexts"][event["context"]]["timing_rule"],
            event["window_u"],
        )
        end = start + event["duration"]

        require(
            math.isfinite(end) and end > start,
            "Long-prefix clock did not advance.",
        )
        counts[event["context"]] += 1

    return {
        code: 168 * (
            count if code == "TOTAL" else counts[code]
        ) / end
        for code in GROUPS
    }


def bag_statistics(rows, field, bags, repeats):
    grouped = defaultdict(list)

    for row in rows:
        grouped[row["source_replicate"]].append(float(row[field]))

    require(
        set(grouped) == set(range(1, bags + 1))
        and all(len(values) == repeats for values in grouped.values()),
        "Incomplete conditional process grid.",
    )

    means = [
        mean(grouped[rep])
        for rep in range(1, bags + 1)
    ]

    return dict(
        mean=mean(means),
        saved_bag_mcse=stdev(means) / math.sqrt(bags),
        conditional_process_mcse=math.sqrt(
            math.fsum(
                variance(values) / repeats
                for values in grouped.values()
            ) / bags ** 2
        ),
    )


def account(owner, rows, days, replicate, repeat, hourly):
    starts = Counter()
    completions = Counter()
    energies = defaultdict(list)
    previous = -math.inf

    for row in rows:
        require(
            row["start_hour_absolute"] >= previous,
            "Owner overlap during verification.",
        )
        previous = row["end_hour_absolute"]

        row.update(allocate_energy(row, 24 * days, hourly))

        starts[row["context"]] += int(
            0 <= row["start_hour_absolute"] < 24 * days
        )
        completions[row["context"]] += int(
            0 <= row["end_hour_absolute"] < 24 * days
        )
        energies[row["context"]].append(
            row["energy_in_study_kwh"]
        )

    result = []

    for code in GROUPS:
        total = code == "TOTAL"
        rate = owner["total"] if total else owner["rates"][code]

        completed = (
            sum(completions.values())
            if total else completions[code]
        )
        energy = (
            math.fsum(
                value
                for values in energies.values()
                for value in values
            )
            if total else math.fsum(energies[code])
        )

        result.append(
            dict(
                source_replicate=replicate,
                verification_repeat=repeat,
                owner_id=owner["owner_id"],
                context=code,
                target_expected_study_events=rate * days / 7,
                starts=(
                    sum(starts.values()) if total else starts[code]
                ),
                completed_in_study=completed,
                completion_gap=completed - rate * days / 7,
                energy_in_study_kwh=energy,
            )
        )

    return result


def cohort_rows(rows, days, replicate, repeat):
    result = []

    for code in GROUPS:
        group = [row for row in rows if row["context"] == code]
        target = math.fsum(
            row["target_expected_study_events"] for row in group
        )
        completed = sum(
            row["completed_in_study"] for row in group
        )

        result.append(
            dict(
                source_replicate=replicate,
                verification_repeat=repeat,
                context=code,
                target_expected_study_events=target,
                starts=sum(row["starts"] for row in group),
                completed_in_study=completed,
                completion_gap=completed - target,
                mean_daily_energy_kwh=math.fsum(
                    row["energy_in_study_kwh"] for row in group
                ) / days,
            )
        )

    return result


def verify(
    pilot,
    long_events=4096,
    process_repeats=10,
    project_root=ROOT,
):
    require(
        type(long_events) is int
        and 128 <= long_events <= 16384
        and type(process_repeats) is int
        and 2 <= process_repeats <= 20,
        "Invalid verification sizes.",
    )

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
            and (
                path not in watched or watched[path] == digest
            ),
            "Input hash mismatch: " + str(value),
        )

        watched[path] = digest
        return path

    def load(value):
        return json.loads(
            watch(value).read_text(encoding="utf-8-sig")
        )

    folder = (root / pilot).resolve()
    require(
        folder.is_relative_to(root / "data/simulated/synthetic")
        and folder.is_dir(),
        "Use a completed synthetic availability pilot.",
    )

    report = load(folder / "availability_generator_report.json")

    require(
        report.get("schema_version")
        == "availability_generator_comparison_v1"
        and report.get("status") == "complete"
        and report.get("data_origin") == "SYNTHETIC"
        and report.get("output_origin") == "SIMULATED"
        and report.get("checks")
        and all(
            value is True for value in report["checks"].values()
        ),
        "Unexpected pilot report.",
    )
    require(
        set(report["output_sha256"]) == PILOT_FILES,
        "Unexpected pilot export manifest.",
    )

    for name, digest in report["output_sha256"].items():
        watch(folder / name, digest)

    for name, digest in report["input_sha256"].items():
        watch(name, digest)

    watch("src/ev_demand/simulation/verify_frozen_availability.py")

    bank_folder = (root / report["parameters_folder"]).resolve()
    bank = load(bank_folder / "window_screen_sensitivity.json")

    population_folder = (
        root / bank["source_population_folder"]
    ).resolve()
    population = load(
        population_folder / "behaviour_population_model.json"
    )

    require(
        bank["schema_version"]
        == "paired_window_screen_sensitivity_v1"
        and population["schema_version"]
        == "behaviour_population_model_v1",
        "Unexpected source schema.",
    )

    for path in (
        bank_folder / "window_screen_sensitivity.json",
        population_folder / "behaviour_population_model.json",
    ):
        require(
            report["input_sha256"].get(
                path.relative_to(root).as_posix()
            ) == watched[path],
            "Pilot uses a different source snapshot.",
        )

    ids = report["owner_ids"]
    bags = report["replicates"]
    days = report["study_days"]
    warmup = report["warmup_days"]
    seed = report["base_seed"]

    require(
        type(bags) is int
        and bags >= 2
        and ids
        and len(set(ids)) == len(ids)
        and len(ids) == report["owners_simulated"]
        and days == bank["capacity_reference_study_days"]
        and long_events
        > report["verification_events_per_owner_selection"],
        "Invalid source grid or prefix size.",
    )

    owners = {
        row["owner_id"]: row for row in population["owners"]
    }
    expected = {
        (rep, oid)
        for rep in range(1, bags + 1)
        for oid in ids
    }

    def table(name, context=False):
        rows = baseline.read_csv(folder / name)

        indexed = {
            (
                int(row["replicate"]),
                row["owner_id"],
                *([row["context"]] if context else []),
            ): row
            for row in rows
        }

        grid = (
            {
                (rep, oid, code)
                for rep, oid in expected
                for code in GROUPS
            }
            if context else expected
        )

        require(
            len(indexed) == len(rows) and set(indexed) == grid,
            "Incomplete source table: " + name,
        )
        return indexed

    selected = table("selected_owner_parameters.csv")
    calibration = table("owner_calibration.csv")
    original_diagnostics = table(
        "owner_generator_diagnostics.csv", True
    )

    bank_rows = {
        (
            row["owner_id"],
            row["energy_case"],
            int(row["parameter_draw"]),
        ): row
        for row in baseline.read_csv(
            bank_folder / "screened_parameter_draws.csv"
        )
    }

    saved_sessions = defaultdict(list)

    for row in baseline.read_csv(folder / "sessions.csv"):
        if row["generator"] == "availability_aware":
            key = int(row["replicate"]), row["owner_id"]
            require(
                key in expected,
                "Unexpected saved session owner.",
            )
            saved_sessions[key].append(row)

    frozen = {}
    limit = (
        float(bank["threshold_hours"])
        - float(bank["numerical_tolerance_hours"])
    )

    for key in sorted(expected):
        row = selected[key]
        cal = calibration[key]

        source = bank_rows[
            (
                key[1],
                row["energy_case"],
                int(row["parameter_draw"]),
            )
        ]

        require(
            all(row[field] == value for field, value in source.items())
            and row["energy_case"] == report["energy_case"],
            "Saved selected vector differs from the source bank.",
        )

        owner, hours = make_owner(
            owners[key[1]],
            row,
            report["power_scenario"],
            days,
            limit,
        )
        idle = float(cal["idle_mean_hours"])

        require(
            math.isfinite(idle)
            and idle >= 0
            and close(
                hours,
                float(row["expected_weekly_charging_hours"]),
            )
            and close(
                owner["total"],
                float(cal["target_total_weekly_rate"]),
            ),
            "Invalid saved calibration/vector.",
        )

        frozen[key] = owner, idle

    original_rows = []
    prefix_rows = []
    review_rows = []

    for rep in range(1, bags + 1):
        hourly = {
            code: [0.0] * (24 * days) for code in CONTEXTS
        }
        rows = []

        for oid in ids:
            key = rep, oid
            owner, idle = frozen[key]

            replay = sequential_sessions(
                owner, idle, days, warmup, seed, rep
            )
            rows.extend(
                account(owner, replay, days, rep, 0, hourly)
            )

            saved = sorted(
                saved_sessions[key],
                key=lambda row: float(row["start_hour_absolute"]),
            )
            require(
                len(saved) == len(replay),
                "Original availability session count did not replay.",
            )

            for actual, recorded in zip(replay, saved):
                for field in (
                    *SESSION_FIELDS, *EXTRA, *ENERGY_FIELDS
                ):
                    value = actual[field]

                    if (
                        isinstance(value, (int, float))
                        and not isinstance(value, bool)
                    ):
                        matches = close(
                            float(value), float(recorded[field])
                        )
                    else:
                        matches = recorded[field] == (
                            "" if value is None else str(value)
                        )

                    require(
                        matches,
                        "Original session replay differs: " + field,
                    )

            rates = long_prefix(
                owner, idle, seed, rep, long_events
            )
            cal = calibration[key]
            source = selected[key]
            error = 100 * (
                rates["TOTAL"] / owner["total"] - 1
            )

            review_rows.append(
                dict(
                    source_replicate=rep,
                    owner_id=oid,
                    parameter_draw=source["parameter_draw"],
                    original_calibration_status=cal[
                        "calibration_status"
                    ],
                    idle_mean_hours=idle,
                    usual_energy_kwh=owner["energy"],
                    home_power_proxy_kw=source[
                        "home_power_proxy_kw"
                    ],
                    target_weekly_rate=owner["total"],
                    long_prefix_weekly_rate=rates["TOTAL"],
                    long_prefix_error_percent=error,
                    within_5_percent_diagnostic_band=abs(error) <= 5,
                )
            )

            for code in GROUPS:
                target = (
                    owner["total"]
                    if code == "TOTAL"
                    else owner["rates"][code]
                )
                prefix_rows.append(
                    dict(
                        source_replicate=rep,
                        owner_id=oid,
                        context=code,
                        target_weekly_rate=target,
                        long_prefix_weekly_rate=rates[code],
                    )
                )

        for row in rows:
            saved = original_diagnostics[
                (rep, row["owner_id"], row["context"])
            ]

            require(
                row["starts"] == int(saved["availability_starts"])
                and row["completed_in_study"]
                == int(saved["availability_completed_in_study"])
                and close(
                    row["target_expected_study_events"],
                    float(saved["target_expected_study_events"]),
                ),
                "Original owner counts did not replay.",
            )

        original_rows.extend(
            cohort_rows(rows, days, rep, 0)
        )

        print(
            f"Frozen source bag {rep}/{bags}: "
            "original replay and long-prefix check complete",
            flush=True,
        )

    process_rows = []
    owner_rows = []

    for repeat in range(1, process_repeats + 1):
        new_seed = process_seed(seed, repeat)
        require(
            new_seed != seed,
            "Verification study seed collided with the original seed.",
        )

        for rep in range(1, bags + 1):
            hourly = {
                code: [0.0] * (24 * days) for code in CONTEXTS
            }
            rows = []

            for oid in ids:
                owner, idle = frozen[(rep, oid)]

                sessions = sequential_sessions(
                    owner, idle, days, warmup, new_seed, rep
                )
                rows.extend(
                    account(
                        owner, sessions, days, rep, repeat, hourly
                    )
                )

            require(
                close(
                    math.fsum(
                        row["energy_in_study_kwh"]
                        for row in rows
                        if row["context"] == "TOTAL"
                    ),
                    math.fsum(
                        value
                        for series in hourly.values()
                        for value in series
                    ),
                ),
                "Verification hourly energy failed.",
            )

            owner_rows.extend(rows)
            process_rows.extend(
                cohort_rows(rows, days, rep, repeat)
            )

        print(
            f"Conditional process repeat {repeat}/{process_repeats}: "
            f"all {bags} saved bags checked",
            flush=True,
        )

    summary = []

    for code in GROUPS:
        rows = [
            row for row in process_rows if row["context"] == code
        ]
        old = [
            row for row in original_rows if row["context"] == code
        ]
        prefixes = [
            row for row in prefix_rows if row["context"] == code
        ]

        target = math.fsum(
            row["target_expected_study_events"] for row in rows
        )
        old_target = math.fsum(
            row["target_expected_study_events"] for row in old
        )
        prefix_target = math.fsum(
            row["target_weekly_rate"] for row in prefixes
        )

        gaps = bag_statistics(
            rows, "completion_gap", bags, process_repeats
        )
        energy = bag_statistics(
            rows, "mean_daily_energy_kwh", bags, process_repeats
        )

        summary.append(
            dict(
                context=code,
                original_completed_to_target=(
                    sum(row["completed_in_study"] for row in old)
                    / old_target
                    if old_target else None
                ),
                verification_completed_to_target=(
                    sum(row["completed_in_study"] for row in rows)
                    / target
                    if target else None
                ),
                mean_completion_gap=gaps["mean"],
                saved_bag_completion_gap_mcse=gaps[
                    "saved_bag_mcse"
                ],
                conditional_process_completion_gap_mcse=gaps[
                    "conditional_process_mcse"
                ],
                long_prefix_weekly_to_target=(
                    math.fsum(
                        row["long_prefix_weekly_rate"]
                        for row in prefixes
                    ) / prefix_target
                    if prefix_target else None
                ),
                original_mean_daily_energy_kwh=mean(
                    row["mean_daily_energy_kwh"] for row in old
                ),
                verification_mean_daily_energy_kwh=energy["mean"],
                saved_bag_energy_mcse=energy["saved_bag_mcse"],
                conditional_process_energy_mcse=energy[
                    "conditional_process_mcse"
                ],
            )
        )

        original_summary = next(
            row
            for row in report["summary"]
            if row["context"] == code
        )
        require(
            close(
                summary[-1]["original_mean_daily_energy_kwh"],
                original_summary["availability_mean_daily_energy_kwh"],
            ),
            "Original mean energy did not replay.",
        )

    statuses = []

    for status in sorted(
        {row["original_calibration_status"] for row in review_rows}
    ):
        group = [
            row
            for row in review_rows
            if row["original_calibration_status"] == status
        ]

        statuses.append(
            dict(
                original_calibration_status=status,
                selections=len(group),
                unique_owners=len(
                    {row["owner_id"] for row in group}
                ),
                mean_long_prefix_error_percent=mean(
                    row["long_prefix_error_percent"]
                    for row in group
                ),
                below_minus_5_percent=sum(
                    row["long_prefix_error_percent"] < -5
                    for row in group
                ),
                above_plus_5_percent=sum(
                    row["long_prefix_error_percent"] > 5
                    for row in group
                ),
            )
        )

    require(
        len(review_rows) == bags * len(ids)
        and len(owner_rows)
        == bags * process_repeats * len(ids) * 5,
        "Verification owner grid is incomplete.",
    )
    require(
        all(
            baseline.sha256(path) == digest
            for path, digest in watched.items()
        ),
        "Input changed during verification.",
    )

    output = root / "data/simulated/synthetic" / (
        "availability_verification_"
        + datetime.now(timezone.utc).strftime(
            "%Y%m%dT%H%M%S_%fZ"
        )
    )
    output.mkdir(parents=True, exist_ok=False)

    for filename, rows in (
        ("long_prefix_rates.csv", prefix_rows),
        ("parameter_selection_review.csv", review_rows),
        ("original_replay_summary.csv", original_rows),
        ("owner_process_checks.csv", owner_rows),
        ("conditional_process_summary.csv", process_rows),
        ("verification_context_summary.csv", summary),
        ("verification_status_summary.csv", statuses),
    ):
        write_csv(output / filename, rows, tuple(rows[0]))

    require(
        all(
            baseline.sha256(path) == digest
            for path, digest in watched.items()
        ),
        "Input changed during export.",
    )

    result = dict(
        schema_version="frozen_availability_verification_v1",
        status="complete",
        data_origin="SYNTHETIC",
        behaviour_input_origin="SYNTHETIC",
        output_origin="SIMULATED",
        source_pilot=folder.relative_to(root).as_posix(),
        owners_simulated=len(ids),
        source_parameter_bags=bags,
        selected_parameter_pairs=len(frozen),
        study_days=days,
        warmup_days=warmup,
        base_seed=seed,
        source_calibration_events=report[
            "calibration_events_per_owner_selection"
        ],
        long_prefix_events_per_selection=long_events,
        conditional_process_repeats_per_bag=process_repeats,
        conditional_study_batches=bags * process_repeats,
        new_parameter_draws=0,
        new_calibrations=0,
        summary=summary,
        calibration_group_summary=statuses,
        completed_frequency_agreement_established=False,
        initialization_stationarity_established=False,
        empirical_validation=False,
        policy=[
            (
                "All saved owner vectors and calibrated idle means are "
                "fixed; no selection, exclusion, redraw or retuning uses "
                "verification outcomes."
            ),
            (
                "Original availability study sessions, boundary energy "
                "and owner counts are replayed before independent checks."
            ),
            (
                "Long-prefix rates are finite-prefix completion-count / "
                "elapsed-time proxies from an empty clock, using a "
                "separate stage. Sum owner rate estimates with equal "
                "owner representation, rather than pooling unequal "
                "owner exposure times."
            ),
            (
                "Longer verification reduces verification sampling noise; "
                "it does not remove uncertainty from the original "
                "finite-prefix calibration or prove a stationary mean."
            ),
            (
                "The independent 28-day checks retain the source warm-up. "
                "Repeats reuse the saved parameter bags and are conditional "
                "process repeats, not new independently sampled population "
                "or parameter replicates."
            ),
            (
                "Saved-bag MCSE uses one conditional-repeat mean per "
                "original parameter bag. Conditional process MCSE holds "
                "all saved bags fixed and uses within-bag process "
                "variances. Neither measures uncertainty in real owner "
                "behaviour or fitted source parameters."
            ),
            (
                "Context ratios, including T4, and owner review flags "
                "remain diagnostics. The 5% band is not a confidence "
                "interval or a production acceptance rule. No process "
                "change is selected automatically."
            ),
            (
                "No new session-trace CSV is exported. Process seeds, "
                "saved inputs, code hashes and owner/context counts "
                "permit deterministic replay."
            ),
        ],
        checks=dict(
            source_hashes_verified=True,
            saved_vectors_and_idle_means_fixed=True,
            original_availability_replayed=True,
            no_owner_overlaps=True,
            energy_conserved=True,
            complete_owner_process_grid=True,
            saved_bags_not_flattened_for_mcse=True,
            source_files_unchanged=True,
        ),
        input_sha256={
            path.relative_to(root).as_posix(): digest
            for path, digest in watched.items()
        },
        output_sha256={
            path.name: baseline.sha256(path)
            for path in sorted(output.iterdir())
        },
    )

    (output / "frozen_verification_report.json").write_text(
        json.dumps(
            result,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        ) + "\n",
        encoding="utf-8",
    )

    return output, result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot", required=True)
    parser.add_argument("--long-events", type=int, default=4096)
    parser.add_argument("--process-repeats", type=int, default=10)
    args = parser.parse_args()

    output, report = verify(
        args.pilot, args.long_events, args.process_repeats
    )

    print("Frozen availability-generator verification complete.")

    for key in (
        "owners_simulated",
        "source_parameter_bags",
        "selected_parameter_pairs",
        "long_prefix_events_per_selection",
        "conditional_process_repeats_per_bag",
        "conditional_study_batches",
    ):
        print(f"{key}: {report[key]}")

    for row in report["calibration_group_summary"]:
        print(
            f"{row['original_calibration_status']}: "
            f"selections={row['selections']}, "
            f"long_prefix_mean_error="
            f"{row['mean_long_prefix_error_percent']:.3f}%, "
            f"below_-5%={row['below_minus_5_percent']}, "
            f"above_+5%={row['above_plus_5_percent']}"
        )

    def show(value):
        return "NA" if value is None else f"{value:.4f}"

    for row in report["summary"]:
        print(
            f"{row['context']}: completions/target="
            f"{show(row['original_completed_to_target'])} -> "
            f"{show(row['verification_completed_to_target'])}, "
            f"long_prefix/target="
            f"{show(row['long_prefix_weekly_to_target'])}, "
            f"mean_gap={row['mean_completion_gap']:.3f}, "
            f"bag_mcse={row['saved_bag_completion_gap_mcse']:.3f}, "
            f"conditional_process_mcse="
            f"{row['conditional_process_completion_gap_mcse']:.3f}"
        )

    print(
        "Original replay, frozen-parameter, owner-grid "
        "and energy checks passed."
    )
    print(f"Output: {output.relative_to(ROOT)}")
    print(
        "Conditional generator verification only; "
        "no retuning, new parameter draws or empirical validation."
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
        print(
            f"Frozen availability verification failed: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1)