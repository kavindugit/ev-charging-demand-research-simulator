"""Compare reduced/rated power pilots with separate control and availability pairing."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from statistics import mean, stdev
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from ev_demand.parameters import build_simulator_parameters as baseline
from ev_demand.parameters.build_population_evidence import CONTEXTS, require
from ev_demand.simulation.run_session_simulator import (
    DRAW_FIELDS,
    ENERGY_FIELDS,
    SESSION_FIELDS,
    allocate_energy,
    close,
    write_csv,
)

GROUPS = (*CONTEXTS, "TOTAL")
GENERATORS = ("poisson_control", "availability_aware")

FILES = {
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

POWER_DEPENDENT = {
    "end_hour_absolute",
    "active_duration_hours",
    "effective_average_power_kw",
    "sampled_average_to_rated_fraction",
    "uncapped_effective_average_power_kw",
    "scenario",
    "vehicle_limit_applied",
}


def paired_stats(values):
    require(
        len(values) >= 2
        and all(math.isfinite(value) for value in values),
        "Invalid paired replicate values.",
    )
    return mean(values), stdev(values) / math.sqrt(len(values))


def check_power_pair(first, second, candidate):
    if candidate:
        shared = set(SESSION_FIELDS) - POWER_DEPENDENT
    else:
        shared = {
            "session_id",
            "owner_id",
            "data_origin",
            "context",
            "charger_code",
            "session_energy_kwh",
            "power_model_kind",
            "rating_id",
            "rated_output_kw",
            "vehicle_limit_kw",
            "vehicle_limit_status",
            "connector_compatibility_checked",
            "power_evidence_status",
        }

    if first["context"] == "T2":
        shared |= set(DRAW_FIELDS) | {"active_duration_hours"}

    require(
        all(first[field] == second[field] for field in shared),
        "Paired event attributes differ: " + first["session_id"],
    )

    if first["context"] != "T2":
        require(
            float(second["effective_average_power_kw"])
            >= float(first["effective_average_power_kw"]) - 1e-9
            and close(
                float(second["sampled_average_to_rated_fraction"]),
                1.0,
            )
            and close(
                float(second["uncapped_effective_average_power_kw"]),
                float(second["rated_output_kw"]),
            ),
            "Unexpected rated-ceiling power draw.",
        )


def compare(reduced, rated, project_root=ROOT):
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

    def load(reference, scenario):
        folder = (root / reference).resolve()

        require(
            folder.is_relative_to(root / "data/simulated/synthetic")
            and folder.is_dir(),
            "Use synthetic pilot folders.",
        )

        report = json.loads(
            watch(
                folder / "availability_generator_report.json"
            ).read_text(encoding="utf-8-sig")
        )

        require(
            report.get("schema_version")
            == "availability_generator_comparison_v1"
            and report.get("status") == "complete"
            and report.get("data_origin") == "SYNTHETIC"
            and report.get("output_origin") == "SIMULATED"
            and report.get("power_scenario") == scenario
            and report.get("checks")
            and all(
                value is True
                for value in report["checks"].values()
            ),
            "Unexpected pilot report.",
        )

        require(
            set(report["output_sha256"]) == FILES,
            "Unexpected pilot export manifest.",
        )

        for name, digest in report["output_sha256"].items():
            watch(folder / name, digest)

        for name, digest in report["input_sha256"].items():
            watch(name, digest)

        n = report["replicates"]
        days = report["study_days"]
        warmup = report["warmup_days"]
        ids = report["owner_ids"]

        require(
            type(n) is int
            and n >= 2
            and type(days) is int
            and days > 0
            and ids
            and len(set(ids)) == len(ids) == report["owners_simulated"],
            "Invalid pilot grid.",
        )

        expected = {
            (rep, oid)
            for rep in range(1, n + 1)
            for oid in ids
        }

        def table(name, extra=()):
            rows = baseline.read_csv(folder / name)
            indexed = {
                (
                    int(row["replicate"]),
                    *[row[field] for field in extra],
                ): row
                for row in rows
            }
            return rows, indexed

        rows, selected = table(
            "selected_owner_parameters.csv", ("owner_id",)
        )
        require(
            len(rows) == len(selected)
            and set(selected) == expected,
            "Incomplete selected-owner grid.",
        )

        rows, calibration = table(
            "owner_calibration.csv", ("owner_id",)
        )
        require(
            len(rows) == len(calibration)
            and set(calibration) == expected,
            "Incomplete calibration grid.",
        )

        require(
            dict(
                Counter(row["calibration_status"] for row in rows)
            ) == report["calibration_status_counts"]
            and sum(
                abs(float(row["verification_relative_error_percent"]))
                > report["verification_development_band_percent"]
                for row in rows
            ) == report["verification_selections_outside_band"],
            "Calibration counts disagree.",
        )

        hourly = {
            (rep, generator): {
                code: [0.0] * (24 * days)
                for code in CONTEXTS
            }
            for rep in range(1, n + 1)
            for generator in GENERATORS
        }

        kept = {generator: {} for generator in GENERATORS}
        rejected = {}
        counters = Counter()
        ordered = defaultdict(list)

        def session(row, accepted):
            rep = int(row["replicate"])
            oid = row["owner_id"]
            code = row["context"]
            generator = row["generator"]

            require(
                (rep, oid) in expected
                and code in CONTEXTS
                and generator in GENERATORS
                and row["data_origin"] == "SYNTHETIC"
                and row["output_origin"] == "SIMULATED"
                and row["behaviour_input_origin"] == "SYNTHETIC",
                "Invalid session provenance/grid.",
            )

            sample = dict(row)
            for field in (
                "start_hour_absolute",
                "end_hour_absolute",
                "effective_average_power_kw",
                "session_energy_kwh",
            ):
                sample[field] = float(row[field])

            start = sample["start_hour_absolute"]
            end = sample["end_hour_absolute"]
            power = sample["effective_average_power_kw"]
            energy = sample["session_energy_kwh"]

            require(
                all(
                    math.isfinite(value)
                    for value in (start, end, power, energy)
                )
                and power > 0
                and energy > 0
                and -24 * warmup <= start < 24 * days
                and end > start
                and close(power * (end - start), energy)
                and close(
                    float(row["active_duration_hours"]),
                    end - start,
                )
                and close(
                    energy,
                    float(selected[(rep, oid)]["usual_energy_kwh"]),
                )
                and row["start_phase"]
                == ("study" if start >= 0 else "warmup"),
                "Session energy/timing is inconsistent.",
            )

            if code == "T2":
                require(
                    close(
                        power,
                        float(
                            selected[(rep, oid)]["home_power_proxy_kw"]
                        ),
                    ),
                    "Selected home power changed.",
                )

            key = row["session_id"]

            if accepted:
                require(
                    key not in kept[generator],
                    "Duplicate accepted session ID.",
                )
                kept[generator][key] = row
                ordered[(rep, generator, oid)].append((start, end))

                allocation = allocate_energy(
                    sample, 24 * days, hourly[(rep, generator)]
                )
                require(
                    all(
                        close(allocation[field], float(row[field]))
                        for field in ENERGY_FIELDS
                    ),
                    "Saved boundary energy disagrees.",
                )

                counters[
                    (rep, generator, oid, code, "starts")
                ] += int(start >= 0)

                counters[
                    (rep, generator, oid, code, "completed")
                ] += int(0 <= end < 24 * days)

            else:
                require(
                    generator == "poisson_control"
                    and key not in rejected,
                    "Invalid rejected control session.",
                )
                rejected[key] = row

                counters[
                    (rep, generator, oid, code, "rejected")
                ] += int(start >= 0)

        for row in baseline.read_csv(folder / "sessions.csv"):
            session(row, True)

        for row in baseline.read_csv(
            folder / "control_rejected_sessions.csv"
        ):
            session(row, False)

        require(
            not (
                set(rejected) & set(kept["poisson_control"])
            ),
            "A control candidate is both accepted and rejected.",
        )

        for values in ordered.values():
            values.sort()
            require(
                all(
                    second[0] >= first[1]
                    for first, second in zip(values, values[1:])
                ),
                "Owner overlap in source pilot.",
            )

        profiles, daily = {}, {}

        rows, saved_profiles = table(
            "replicate_profiles.csv",
            ("context", "hour_of_day"),
        )
        profile_grid = {
            (rep, code, str(hour))
            for rep in range(1, n + 1)
            for code in GROUPS
            for hour in range(24)
        }

        require(
            len(rows) == len(saved_profiles)
            and set(saved_profiles) == profile_grid,
            "Incomplete source profiles.",
        )

        for rep in range(1, n + 1):
            for generator in GENERATORS:
                field = (
                    "control_mean_power_kw"
                    if generator == "poisson_control"
                    else "availability_mean_power_kw"
                )

                for code in GROUPS:
                    for hour in range(24):
                        value = math.fsum(
                            value
                            for context in CONTEXTS
                            if code == "TOTAL" or context == code
                            for value in hourly[
                                (rep, generator)
                            ][context][hour::24]
                        ) / days

                        profiles[
                            (rep, generator, code, hour)
                        ] = value

                        require(
                            close(
                                value,
                                float(
                                    saved_profiles[
                                        (rep, code, str(hour))
                                    ][field]
                                ),
                            ),
                            "Source profile does not rebuild from sessions.",
                        )

                    daily[(rep, generator, code)] = math.fsum(
                        profiles[(rep, generator, code, hour)]
                        for hour in range(24)
                    )

        rows, saved_daily = table(
            "replicate_comparison.csv", ("context",)
        )
        require(
            len(rows) == len(saved_daily) == n * 5
            and set(saved_daily)
            == {
                (rep, code)
                for rep in range(1, n + 1)
                for code in GROUPS
            },
            "Incomplete source daily energy grid.",
        )

        for (rep, code), row in saved_daily.items():
            require(
                close(
                    daily[(rep, "poisson_control", code)],
                    float(row["control_daily_energy_kwh"]),
                )
                and close(
                    daily[(rep, "availability_aware", code)],
                    float(row["availability_daily_energy_kwh"]),
                ),
                "Source daily energy disagrees with sessions.",
            )

        rows, diagnostics = table(
            "owner_generator_diagnostics.csv",
            ("owner_id", "context"),
        )
        require(
            len(rows) == len(diagnostics)
            and set(diagnostics)
            == {
                (rep, oid, code)
                for rep, oid in expected
                for code in GROUPS
            },
            "Incomplete owner diagnostic grid.",
        )

        counts = defaultdict(lambda: Counter(target=0.0))

        for (rep, oid, code), row in diagnostics.items():
            source = selected[(rep, oid)]
            rate = float(
                source[
                    "total_weekly_rate"
                    if code == "TOTAL"
                    else code + "_weekly_rate"
                ]
            )
            target = rate * days / 7

            require(
                close(
                    target,
                    float(row["target_expected_study_events"]),
                ),
                "Owner target changed.",
            )

            for generator in GENERATORS:
                def number(kind):
                    return sum(
                        counters[
                            (rep, generator, oid, context, kind)
                        ]
                        for context in CONTEXTS
                        if code == "TOTAL" or context == code
                    )

                starts = number("starts")
                completed = number("completed")
                dropped = number("rejected")

                prefix = (
                    "control"
                    if generator == "poisson_control"
                    else "availability"
                )

                require(
                    starts == int(
                        row[
                            "control_accepted_starts"
                            if generator == "poisson_control"
                            else "availability_starts"
                        ]
                    )
                    and completed
                    == int(row[prefix + "_completed_in_study"]),
                    "Owner counts do not rebuild.",
                )

                if generator == "poisson_control":
                    require(
                        dropped == int(row["control_rejected_starts"])
                        and starts + dropped
                        == int(row["control_candidate_starts"]),
                        "Control candidate accounting failed.",
                    )

                counts[(rep, generator, code)].update(
                    target=target,
                    starts=starts,
                    completed=completed,
                    rejected=dropped,
                )

        for code in GROUPS:
            saved = next(
                row
                for row in report["summary"]
                if row["context"] == code
            )
            for generator in GENERATORS:
                field = (
                    "control_mean_daily_energy_kwh"
                    if generator == "poisson_control"
                    else "availability_mean_daily_energy_kwh"
                )
                require(
                    close(
                        mean(
                            daily[(rep, generator, code)]
                            for rep in range(1, n + 1)
                        ),
                        saved[field],
                    ),
                    "Source report mean energy disagrees.",
                )

        return dict(
            folder=folder,
            report=report,
            selected=selected,
            calibration=calibration,
            kept=kept,
            rejected=rejected,
            profiles=profiles,
            daily=daily,
            counts=counts,
        )

    first_case = load(reduced, "reduced_test")
    second_case = load(rated, "rated_ceiling_test")

    watch(
        "src/ev_demand/simulation/compare_availability_power_pilots.py"
    )

    for field in (
        "parameters_folder",
        "owner_ids",
        "owners_simulated",
        "cohort",
        "energy_case",
        "study_days",
        "warmup_days",
        "replicates",
        "base_seed",
        "calibration_events_per_owner_selection",
        "verification_events_per_owner_selection",
        "calibration_proxy_relative_tolerance",
        "verification_development_band_percent",
        "policy",
        "input_sha256",
    ):
        require(
            first_case["report"][field]
            == second_case["report"][field],
            "Power pilots differ in " + field,
        )

    require(
        first_case["folder"] != second_case["folder"]
        and first_case["selected"].keys()
        == second_case["selected"].keys(),
        "Invalid paired pilot selection grid.",
    )

    calibration_rows = []

    for key, first in first_case["selected"].items():
        second = second_case["selected"][key]

        require(
            first.keys() == second.keys()
            and all(
                first[field] == second[field]
                for field in first
                if field != "expected_weekly_charging_hours"
            ),
            "Original owner parameter draws differ.",
        )

        require(
            float(second["expected_weekly_charging_hours"])
            <= float(first["expected_weekly_charging_hours"]) + 1e-8,
            "Rated expected occupation exceeds reduced occupation.",
        )

        first_cal = first_case["calibration"][key]
        second_cal = second_case["calibration"][key]

        calibration_rows.append(
            dict(
                replicate=key[0],
                owner_id=key[1],
                parameter_draw=first["parameter_draw"],
                reduced_expected_weekly_hours=first[
                    "expected_weekly_charging_hours"
                ],
                rated_expected_weekly_hours=second[
                    "expected_weekly_charging_hours"
                ],
                reduced_idle_mean_hours=first_cal["idle_mean_hours"],
                rated_idle_mean_hours=second_cal["idle_mean_hours"],
                reduced_calibration_status=first_cal[
                    "calibration_status"
                ],
                rated_calibration_status=second_cal[
                    "calibration_status"
                ],
                reduced_verification_error_percent=first_cal[
                    "verification_relative_error_percent"
                ],
                rated_verification_error_percent=second_cal[
                    "verification_relative_error_percent"
                ],
            )
        )

    old_candidates = {
        **first_case["kept"]["poisson_control"],
        **first_case["rejected"],
    }
    new_candidates = {
        **second_case["kept"]["poisson_control"],
        **second_case["rejected"],
    }

    require(
        old_candidates.keys() == new_candidates.keys(),
        "Poisson candidate IDs differ.",
    )

    for key in old_candidates:
        check_power_pair(
            old_candidates[key], new_candidates[key], True
        )

    common = (
        first_case["kept"]["availability_aware"].keys()
        & second_case["kept"]["availability_aware"].keys()
    )

    for key in common:
        check_power_pair(
            first_case["kept"]["availability_aware"][key],
            second_case["kept"]["availability_aware"][key],
            False,
        )

    n = first_case["report"]["replicates"]
    metrics, hourly_pairs = [], []

    for rep in range(1, n + 1):
        for generator in GENERATORS:
            for code in GROUPS:
                first_counts = first_case["counts"][
                    (rep, generator, code)
                ]
                second_counts = second_case["counts"][
                    (rep, generator, code)
                ]

                require(
                    close(
                        first_counts["target"],
                        second_counts["target"],
                    ),
                    "Paired context targets differ.",
                )

                first_energy = first_case["daily"][
                    (rep, generator, code)
                ]
                second_energy = second_case["daily"][
                    (rep, generator, code)
                ]

                metrics.append(
                    dict(
                        replicate=rep,
                        generator=generator,
                        context=code,
                        target_expected_study_events=first_counts[
                            "target"
                        ],
                        reduced_starts=first_counts["starts"],
                        rated_starts=second_counts["starts"],
                        reduced_completions=first_counts["completed"],
                        rated_completions=second_counts["completed"],
                        completion_difference=(
                            second_counts["completed"]
                            - first_counts["completed"]
                        ),
                        reduced_daily_energy_kwh=first_energy,
                        rated_daily_energy_kwh=second_energy,
                        daily_energy_difference_kwh=(
                            second_energy - first_energy
                        ),
                    )
                )

                for hour in range(24):
                    first_power = first_case["profiles"][
                        (rep, generator, code, hour)
                    ]
                    second_power = second_case["profiles"][
                        (rep, generator, code, hour)
                    ]

                    hourly_pairs.append(
                        dict(
                            replicate=rep,
                            generator=generator,
                            context=code,
                            hour_of_day=hour,
                            reduced_mean_power_kw=first_power,
                            rated_mean_power_kw=second_power,
                            difference_kw=second_power - first_power,
                        )
                    )

        print(
            f"Verified power pair {rep}/{n}: "
            "owner draws, control candidates and energy=PASS",
            flush=True,
        )

    summaries, means, peaks = [], [], {}

    for generator in GENERATORS:
        for code in GROUPS:
            rows = [
                row
                for row in metrics
                if row["generator"] == generator
                and row["context"] == code
            ]

            delta, mcse = paired_stats(
                [row["daily_energy_difference_kwh"] for row in rows]
            )
            count_delta, count_mcse = paired_stats(
                [row["completion_difference"] for row in rows]
            )
            target = math.fsum(
                row["target_expected_study_events"] for row in rows
            )

            summaries.append(
                dict(
                    generator=generator,
                    context=code,
                    replicates=n,
                    pooled_target_events=target,
                    reduced_completions_to_target=(
                        sum(row["reduced_completions"] for row in rows)
                        / target
                        if target else None
                    ),
                    rated_completions_to_target=(
                        sum(row["rated_completions"] for row in rows)
                        / target
                        if target else None
                    ),
                    reduced_mean_daily_energy_kwh=mean(
                        row["reduced_daily_energy_kwh"] for row in rows
                    ),
                    rated_mean_daily_energy_kwh=mean(
                        row["rated_daily_energy_kwh"] for row in rows
                    ),
                    mean_daily_energy_difference_kwh=delta,
                    paired_daily_energy_mcse_kwh=mcse,
                    mean_completion_difference=count_delta,
                    paired_completion_difference_mcse=count_mcse,
                )
            )

            for hour in range(24):
                pairs = [
                    row
                    for row in hourly_pairs
                    if row["generator"] == generator
                    and row["context"] == code
                    and row["hour_of_day"] == hour
                ]

                difference, error = paired_stats(
                    [row["difference_kw"] for row in pairs]
                )

                means.append(
                    dict(
                        generator=generator,
                        context=code,
                        hour_of_day=hour,
                        reduced_mean_power_kw=mean(
                            row["reduced_mean_power_kw"] for row in pairs
                        ),
                        rated_mean_power_kw=mean(
                            row["rated_mean_power_kw"] for row in pairs
                        ),
                        mean_difference_kw=difference,
                        paired_difference_mcse_kw=error,
                        output_origin="SIMULATED",
                        behaviour_input_origin="SYNTHETIC",
                    )
                )

            for column, value in (
                (
                    "reduced_mean_power_kw",
                    "reduced_mean_daily_energy_kwh",
                ),
                (
                    "rated_mean_power_kw",
                    "rated_mean_daily_energy_kwh",
                ),
                (
                    "mean_difference_kw",
                    "mean_daily_energy_difference_kwh",
                ),
            ):
                require(
                    close(
                        math.fsum(
                            row[column]
                            for row in means
                            if row["generator"] == generator
                            and row["context"] == code
                        ),
                        summaries[-1][value],
                    ),
                    "Paired mean-profile energy failed.",
                )

        total = [
            row
            for row in means
            if row["generator"] == generator
            and row["context"] == "TOTAL"
        ]
        peaks[generator] = {}

        for case in ("reduced", "rated"):
            peak = max(
                row[case + "_mean_power_kw"] for row in total
            )
            peaks[generator][case] = dict(
                peak_of_mean_profile_kw=peak,
                peak_hours=[
                    row["hour_of_day"]
                    for row in total
                    if close(row[case + "_mean_power_kw"], peak)
                ],
            )

    require(
        all(
            baseline.sha256(path) == digest
            for path, digest in watched.items()
        ),
        "Input changed during comparison.",
    )

    output = root / "data/processed/synthetic" / (
        "availability_power_comparison_"
        + datetime.now(timezone.utc).strftime(
            "%Y%m%dT%H%M%S_%fZ"
        )
    )
    output.mkdir(parents=True, exist_ok=False)

    for name, rows in (
        ("paired_replicate_metrics.csv", metrics),
        ("paired_hourly_profiles.csv", hourly_pairs),
        ("context_power_summary.csv", summaries),
        ("mean_profile_comparison.csv", means),
        ("owner_calibration_comparison.csv", calibration_rows),
    ):
        write_csv(output / name, rows, tuple(rows[0]))

    require(
        all(
            baseline.sha256(path) == digest
            for path, digest in watched.items()
        ),
        "Input changed during export.",
    )

    result = dict(
        schema_version="paired_availability_power_comparison_v1",
        status="complete",
        data_origin="SYNTHETIC",
        behaviour_input_origin="SYNTHETIC",
        output_origin="SIMULATED",
        reduced_pilot=first_case["folder"].relative_to(root).as_posix(),
        rated_pilot=second_case["folder"].relative_to(root).as_posix(),
        replicates=n,
        owners_simulated=first_case["report"]["owners_simulated"],
        energy_case=first_case["report"]["energy_case"],
        cohort=first_case["report"]["cohort"],
        study_days=first_case["report"]["study_days"],
        warmup_days=first_case["report"]["warmup_days"],
        paired_owner_parameter_selections=len(calibration_rows),
        matched_control_candidates_including_warmup=len(old_candidates),
        common_availability_serial_events_checked=len(common),
        availability_candidate_pairing=False,
        context_summary=summaries,
        ensemble_mean_peaks=peaks,
        calibration_status_counts={
            "reduced": first_case["report"]["calibration_status_counts"],
            "rated": second_case["report"]["calibration_status_counts"],
        },
        verification_selections_outside_band={
            "reduced": first_case["report"][
                "verification_selections_outside_band"
            ],
            "rated": second_case["report"][
                "verification_selections_outside_band"
            ],
        },
        new_random_draws=0,
        new_demand_simulations=0,
        empirical_validation=False,
        policy=[
            (
                "Positive differences mean rated minus reduced. "
                "Compare both generators separately."
            ),
            (
                "All original selected owner parameters match. "
                "Expected weekly occupation may change with public "
                "power and is not an unchanged latent draw."
            ),
            (
                "Poisson control candidates are paired by ID, start "
                "time, energy, rating and power-independent attributes, "
                "including rejected and warm-up candidates."
            ),
            (
                "Availability runs are paired on owner selections and "
                "common serial event attributes, not identical calendar "
                "candidates. Each power case calibrates its own idle "
                "gap; this comparison includes that policy response."
            ),
            (
                "A negative energy difference is allowed. Fixed session "
                "energy, changing timing, carry-in/out and finite-study "
                "counts do not require rated power to increase daily kWh."
            ),
            (
                "MCSE uses paired whole-cohort replicate differences, "
                "not owners or hours treated as independent samples. "
                "It measures conditional simulation sampling error, "
                "not input-fit uncertainty or empirical accuracy."
            ),
            (
                "Mean-profile peaks and tied hours are reported "
                "separately for each case. Peak of the ensemble mean "
                "is not mean daily peak demand."
            ),
            (
                "All calibration review selections remain included. "
                "No generator, owner exclusion or power case is "
                "selected automatically."
            ),
        ],
        checks=dict(
            source_hashes_verified=True,
            original_owner_draws_paired=True,
            expected_hours_difference_allowed=True,
            poisson_candidate_attributes_paired=True,
            availability_common_serial_attributes_checked=True,
            no_owner_overlaps=True,
            session_boundary_and_hourly_energy_checked=True,
            full_source_grids_checked=True,
            paired_mean_profiles_conserve_energy=True,
            input_files_unchanged=True,
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

    (output / "availability_power_comparison_report.json").write_text(
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
    parser.add_argument("--reduced", required=True)
    parser.add_argument("--rated", required=True)
    args = parser.parse_args()

    output, report = compare(args.reduced, args.rated)

    print("Paired availability public-power comparison complete.")

    for key in (
        "replicates",
        "owners_simulated",
        "paired_owner_parameter_selections",
        "matched_control_candidates_including_warmup",
        "common_availability_serial_events_checked",
    ):
        print(f"{key}: {report[key]}")

    for row in report["context_summary"]:
        print(
            f"{row['generator']} | {row['context']}: "
            f"mean_daily_kwh="
            f"{row['reduced_mean_daily_energy_kwh']:.3f} -> "
            f"{row['rated_mean_daily_energy_kwh']:.3f}, "
            f"difference={row['mean_daily_energy_difference_kwh']:.6f}, "
            f"paired_mcse={row['paired_daily_energy_mcse_kwh']:.6f}"
        )

    for generator, cases in report["ensemble_mean_peaks"].items():
        print(f"{generator} mean-profile peaks: {cases}")

    print(
        "Owner pairing, control-candidate pairing, "
        "source grids and profile-energy checks passed."
    )
    print(f"Output: {output.relative_to(ROOT)}")
    print(
        "Conditional power/policy sensitivity only; "
        "no new simulations or empirical validation."
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
            f"Availability-power comparison failed: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1)