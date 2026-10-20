"""Compare a Poisson control with an internally calibrated availability-aware pilot."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from random import Random
from statistics import mean, stdev
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from ev_demand.parameters import build_simulator_parameters as baseline
from ev_demand.parameters.build_population_evidence import CONTEXTS, require
from ev_demand.parameters.assess_owner_time_budget import (
    inverse_power_bounds,
    weekly_time_bounds,
)
from ev_demand.parameters.build_public_power_layer import sample_session_power
from ev_demand.simulation.assess_start_window_capacity import optimistic_capacity
from ev_demand.simulation.run_joint_parameter_pilot import (
    simulate_selection,
    stream_seed as control_seed,
)
from ev_demand.simulation.run_session_simulator import (
    SESSION_FIELDS,
    ENERGY_FIELDS,
    allocate_energy,
    close,
    write_csv,
)

CASES = (
    "bounded_uniform_test",
    "conditional_gamma_test",
    "conditional_lognormal_test",
)
GROUPS = (*CONTEXTS, "TOTAL")

FILES = {
    "window_screen_block_audit.csv",
    "window_screen_owner_audit.csv",
    "window_screen_group_summary.csv",
    "window_capacity_assessments.csv",
    "window_parameter_distribution_changes.csv",
    "screened_parameter_draws.csv",
}
MODEL_FILES = {
    "population_owner_case_audit.csv",
    "population_context_audit.csv",
    "population_case_summary.csv",
    "joint_owner_parameter_test_draws.csv",
}

EXTRA = (
    "ready_hour_absolute",
    "idle_gap_hours",
    "window_wait_hours",
    "idle_mean_hours",
)

CALIBRATION_TOLERANCE = 0.002
VERIFICATION_BAND_PERCENT = 5.0


def seed_for(seed, replicate, owner_id, stage, component):
    value = (
        f"availability-pilot-v1|{seed}|{replicate}|"
        f"{owner_id}|{stage}|{component}"
    )
    return int.from_bytes(
        hashlib.sha256(value.encode()).digest()[:8], "big"
    )


def window_start(ready, window, u):
    start = float(window["start_hour"])
    length = float(window["length_hours"])

    require(
        math.isfinite(ready)
        and 0 <= start < 24
        and 0 < length <= 24
        and 0 <= u < 1,
        "Invalid availability/window draw.",
    )

    opening = start + 24 * math.floor((ready - start) / 24)
    if ready >= opening + length:
        opening += 24

    lower = max(ready, opening)
    upper = opening + length

    actual = min(
        lower + u * (upper - lower),
        math.nextafter(upper, lower),
    )

    require(
        upper > lower and actual >= lower and actual < upper,
        "Start left its permitted window.",
    )
    return actual


def events(owner, seed, replicate, stage):
    streams = {
        component: Random(
            seed_for(
                seed, replicate, owner["owner_id"], stage, component
            )
        )
        for component in ("context", "power", "clock", "idle")
    }

    codes = [c for c in CONTEXTS if owner["rates"][c] > 0]
    weights = [owner["rates"][c] for c in codes]

    while True:
        code = streams["context"].choices(
            codes, weights=weights, k=1
        )[0]

        power = sample_session_power(
            {"power_model": owner["contexts"][code]["power_model"]},
            streams["power"],
        )
        duration = (
            owner["energy"] / power["effective_average_power_kw"]
        )

        require(
            math.isfinite(duration) and duration > 0,
            "Invalid sequential session duration.",
        )

        yield dict(
            context=code,
            power=power,
            duration=duration,
            window_u=streams["clock"].random(),
            unit_idle=streams["idle"].expovariate(1.0),
        )


def prefix(owner, seed, replicate, stage, count):
    stream = events(owner, seed, replicate, stage)
    return [next(stream) for _ in range(count)]


def prefix_rate(owner, path, idle_mean):
    require(
        path and math.isfinite(idle_mean) and idle_mean >= 0,
        "Invalid timing calibration input.",
    )

    end = 0.0
    for event in path:
        ready = end + idle_mean * event["unit_idle"]
        start = window_start(
            ready,
            owner["contexts"][event["context"]]["timing_rule"],
            event["window_u"],
        )
        end = start + event["duration"]

        require(
            math.isfinite(end) and end > start,
            "Calibration clock did not advance.",
        )

    return 168 * len(path) / end


def calibrate(owner, path):
    target = owner["total"]
    zero = prefix_rate(owner, path, 0.0)
    evaluations = 1
    best_idle, best_rate = 0.0, zero

    if zero >= target * (1 - CALIBRATION_TOLERANCE):
        low, high = 0.0, 168 / target

        for _ in range(20):
            rate = prefix_rate(owner, path, high)
            evaluations += 1

            if abs(rate - target) < abs(best_rate - target):
                best_idle, best_rate = high, rate

            if rate <= target:
                break
            high *= 2
        else:
            raise ValueError(
                "Could not bracket the internal idle parameter."
            )

        for _ in range(16):
            middle = (low + high) / 2
            rate = prefix_rate(owner, path, middle)
            evaluations += 1

            if abs(rate - target) < abs(best_rate - target):
                best_idle, best_rate = middle, rate

            if rate > target:
                low = middle
            else:
                high = middle

        status = (
            "proxy_target_matched"
            if abs(best_rate / target - 1)
            <= CALIBRATION_TOLERANCE
            else "calibration_proxy_gap_review"
        )
    else:
        status = "zero_idle_proxy_below_target_review"

    return dict(
        idle_mean_hours=best_idle,
        target_total_weekly_rate=target,
        zero_idle_calibration_rate_proxy=zero,
        calibrated_rate_proxy=best_rate,
        calibration_relative_error_percent=(
            100 * (best_rate / target - 1)
        ),
        calibration_status=status,
        calibration_evaluations=evaluations,
    )


def sequential_sessions(
    owner, idle_mean, days, warmup, seed, replicate
):
    end = -24.0 * warmup
    rows = []

    for serial, event in enumerate(
        events(owner, seed, replicate, "study"), 1
    ):
        gap = idle_mean * event["unit_idle"]
        ready = end + gap
        code = event["context"]

        start = window_start(
            ready,
            owner["contexts"][code]["timing_rule"],
            event["window_u"],
        )

        if start >= 24 * days:
            break

        previous_end = end
        end = start + event["duration"]

        require(
            math.isfinite(end)
            and end > start
            and start >= previous_end,
            "Sequential no-overlap check failed.",
        )

        day = math.floor(start / 24)

        rows.append(
            dict(
                replicate=replicate,
                output_origin="SIMULATED",
                behaviour_input_origin="SYNTHETIC",
                session_id=(
                    f"AV_{replicate:03d}_"
                    f"{owner['owner_id']}_{serial:06d}"
                ),
                owner_id=owner["owner_id"],
                data_origin="SYNTHETIC",
                context=code,
                charger_code=owner["contexts"][code]["charger_code"],
                start_phase="warmup" if day < 0 else "study",
                start_day_index=day,
                start_hour_of_day=start % 24,
                start_hour_absolute=start,
                end_hour_absolute=end,
                active_duration_hours=event["duration"],
                session_energy_kwh=owner["energy"],
                **event["power"],
                ready_hour_absolute=ready,
                idle_gap_hours=gap,
                window_wait_hours=start - ready,
                idle_mean_hours=idle_mean,
            )
        )

    return rows


def make_owner(raw, row, scenario, days, limit):
    energy = float(row["usual_energy_kwh"])
    total = float(row["total_weekly_rate"])
    rates = {
        c: float(row[c + "_weekly_rate"]) for c in CONTEXTS
    }

    require(
        math.isfinite(energy)
        and energy > 0
        and math.isfinite(total)
        and total > 0
        and all(math.isfinite(v) and v >= 0 for v in rates.values())
        and close(math.fsum(rates.values()), total),
        "Invalid selected energy/rates.",
    )

    home = (
        float(row["home_power_proxy_kw"])
        if row["home_power_proxy_kw"].strip()
        else None
    )
    contexts, inverses = {}, {}

    require(
        len(raw["contexts"]) == 4
        and {
            c["legacy_context"] for c in raw["contexts"]
        } == set(CONTEXTS),
        "Incomplete selected context grid.",
    )

    for context in raw["contexts"]:
        code = context["legacy_context"]

        if context["use_status"] == "reported_nonuse":
            require(
                rates[code] == 0,
                "Reported nonuse has positive frequency.",
            )
            inverses[code] = (0.0, 0.0, 0.0)
            continue

        require(
            context["power_status"] == "supported_test"
            and context["timing_rule"] is not None,
            "Selected owner has unsupported components.",
        )

        power = dict(context["power_models"][scenario])
        inverses[code] = inverse_power_bounds(power, home)

        if code == "T2":
            power["effective_average_kw_min"] = home
            power["effective_average_kw_max"] = home

        contexts[code] = dict(
            timing_rule=context["timing_rule"],
            power_model=power,
            charger_code=context["source_context"]["charger_code"],
        )

        if rates[code] > 0:
            capacity = optimistic_capacity(
                energy * inverses[code][0],
                context["timing_rule"],
                days,
                rates[code] * days / 7,
            )
            require(
                not capacity[
                    "context_target_exceeds_optimistic_capacity"
                ],
                "Selected vector fails its recorded window screen.",
            )

    (_, expected_hours, _), _ = weekly_time_bounds(
        energy, rates, inverses
    )
    require(
        expected_hours < limit,
        "Selected vector fails its recorded time screen.",
    )

    owner = dict(
        owner_id=raw["owner_id"],
        energy=energy,
        total=total,
        rates=rates,
        contexts=contexts,
    )
    return owner, expected_hours


def run_pilot(
    parameters,
    days=28,
    warmup=7,
    replicates=10,
    base_seed=20261007,
    energy_case=CASES[0],
    power_scenario="reduced_test",
    cohort="common_energy_cases",
    calibration_events=512,
    project_root=ROOT,
):
    require(
        type(days) is int
        and 1 <= days <= 56
        and type(warmup) is int
        and 0 <= warmup <= 60
        and type(replicates) is int
        and 2 <= replicates <= 20
        and type(base_seed) is int
        and base_seed >= 0,
        "Invalid pilot days, warm-up, replicates or seed.",
    )
    require(
        type(calibration_events) is int
        and 128 <= calibration_events <= 4096,
        "Use 128-4096 events for each calibration and verification prefix.",
    )
    require(
        energy_case in CASES
        and cohort in {"common_energy_cases", "open_upper_extension"}
        and (
            cohort != "open_upper_extension"
            or energy_case != CASES[0]
        ),
        "Invalid case/cohort.",
    )

    root = Path(project_root).resolve()
    watched = {}
    allowed = (root / "data/processed/synthetic").resolve()

    def watch(value, digest=None):
        path = (root / value).resolve()
        require(
            path.is_relative_to(root) and path.is_file(),
            "Missing project input: " + str(value),
        )

        value_hash = baseline.sha256(path)
        require(
            (digest is None or value_hash == digest)
            and (
                path not in watched
                or watched[path] == value_hash
            ),
            "Input hash mismatch: " + str(value),
        )

        watched[path] = value_hash
        return path

    def package(value, filename, schema, status, files):
        folder = (root / value).resolve()
        require(
            folder.is_relative_to(allowed) and folder.is_dir(),
            "Use a completed synthetic package.",
        )

        data = json.loads(
            watch(folder / filename).read_text(
                encoding="utf-8-sig"
            )
        )
        require(
            data.get("schema_version") == schema
            and data.get("status") == status
            and data.get("data_origin") == "SYNTHETIC"
            and bool(data.get("checks"))
            and all(v is True for v in data["checks"].values()),
            "Unexpected input: " + filename,
        )
        require(
            set(data["output_sha256"]) == files,
            "Unexpected export manifest: " + filename,
        )

        for name, digest in data["output_sha256"].items():
            watch(folder / name, digest)

        return folder, data

    bank_folder, bank_report = package(
        parameters,
        "window_screen_sensitivity.json",
        "paired_window_screen_sensitivity_v1",
        "screening_sensitivity_prepared",
        FILES,
    )

    for path, digest in bank_report["input_sha256"].items():
        watch(path, digest)

    model_folder, population = package(
        bank_report["source_population_folder"],
        "behaviour_population_model.json",
        "behaviour_population_model_v1",
        "components_assembled_for_review",
        MODEL_FILES,
    )

    for filename in (
        "build_simulator_parameters.py",
        "build_population_evidence.py",
        "assess_owner_time_budget.py",
        "build_public_power_layer.py",
    ):
        watch("src/ev_demand/parameters/" + filename)

    for filename in (
        "run_session_simulator.py",
        "run_joint_parameter_pilot.py",
        "assess_start_window_capacity.py",
        "run_availability_generator_pilot.py",
    ):
        watch("src/ev_demand/simulation/" + filename)

    require(
        days == bank_report["capacity_reference_study_days"]
        and power_scenario in population["power_scenarios"],
        "Use the bank's capacity-reference duration and a supported power case.",
    )

    cohort_key = (
        "all_energy_cases_common"
        if cohort == "common_energy_cases"
        else "conditional_open_upper_extension"
    )
    ids = sorted(population["comparison_cohort_ids"][cohort_key])

    require(
        ids and len(ids) == len(set(ids)),
        "Empty or duplicate owner cohort.",
    )

    owners = {
        row["owner_id"]: row for row in population["owners"]
    }
    require(
        len(owners)
        == len(population["owners"])
        == population["owners_retained"],
        "Invalid source owner grid.",
    )

    records = [
        row
        for row in bank_report["owner_screens"]
        if row["cohort"] == cohort
    ]
    indices = {
        row["owner_id"]: json.loads(
            row["admitted_parameter_draws_json"]
        )
        for row in records
    }

    require(
        len(records) == len(indices) == len(ids)
        and set(indices) == set(ids)
        and all(
            values and values == sorted(set(values))
            for values in indices.values()
        ),
        "Every selected owner needs admitted indices; "
        "the full cohort cannot silently shrink.",
    )

    rows = baseline.read_csv(
        bank_folder / "screened_parameter_draws.csv"
    )
    indexed = {
        (
            row["owner_id"],
            row["energy_case"],
            int(row["parameter_draw"]),
        ): row
        for row in rows
    }
    require(
        len(indexed)
        == len(rows)
        == bank_report["retained_parameter_rows"],
        "Duplicate or incomplete bank rows.",
    )

    original = {
        (
            row["owner_id"],
            row["energy_case"],
            int(row["parameter_draw"]),
        ): row
        for row in baseline.read_csv(
            model_folder / "joint_owner_parameter_test_draws.csv"
        )
    }

    for key, row in indexed.items():
        require(
            key in original
            and all(
                row[field] == value
                for field, value in original[key].items()
            ),
            "A source parameter value changed.",
        )

    for oid in ids:
        require(
            owners[oid]["case_component_status"][energy_case][
                "components_available"
            ],
            "Missing owner components.",
        )

        for index in indices[oid]:
            row = indexed[(oid, energy_case, index)]
            require(
                row["screen_cohort"] == cohort
                and math.isclose(
                    float(row["within_owner_draw_probability"]),
                    1 / len(indices[oid]),
                    rel_tol=1e-10,
                    abs_tol=1e-12,
                ),
                "Incorrect within-owner weights.",
            )

    limit = (
        float(bank_report["threshold_hours"])
        - float(bank_report["numerical_tolerance_hours"])
    )
    require(0 < limit <= 168, "Unexpected time-screen limit.")

    sessions_all = []
    rejected_all = []
    selected_all = []
    calibration_rows = []
    diagnostics = []
    profiles = []
    comparisons = []

    for rep in range(1, replicates + 1):
        require(
            all(
                baseline.sha256(path) == digest
                for path, digest in watched.items()
            ),
            "An input changed during the pilot.",
        )

        selection = []
        for oid in ids:
            index = Random(
                control_seed(base_seed, rep, oid, "parameters")
            ).choice(indices[oid])

            row = indexed[(oid, energy_case, index)]
            owner, hours = make_owner(
                owners[oid], row, power_scenario, days, limit
            )
            selection.append(owner)
            selected_all.append(
                dict(
                    replicate=rep,
                    **row,
                    expected_weekly_charging_hours=hours,
                )
            )

        snapshot = json.dumps(
            selection, sort_keys=True, allow_nan=False
        )

        (
            control,
            dropped,
            control_diagnostics,
            control_profiles,
            _,
        ) = simulate_selection(
            selection, days, warmup, base_seed, rep
        )

        availability = []

        for position, owner in enumerate(selection, 1):
            path = prefix(
                owner,
                base_seed,
                rep,
                "calibration",
                calibration_events,
            )
            result = calibrate(owner, path)

            verification = prefix(
                owner,
                base_seed,
                rep,
                "verification",
                calibration_events,
            )
            rate = prefix_rate(
                owner, verification, result["idle_mean_hours"]
            )
            error = 100 * (rate / owner["total"] - 1)

            result.update(
                replicate=rep,
                owner_id=owner["owner_id"],
                calibration_events=calibration_events,
                verification_rate_proxy=rate,
                verification_relative_error_percent=error,
                verification_within_development_band=(
                    abs(error) <= VERIFICATION_BAND_PERCENT
                ),
                calibration_path_sha256=hashlib.sha256(
                    json.dumps(
                        path, sort_keys=True, allow_nan=False
                    ).encode()
                ).hexdigest(),
                verification_path_sha256=hashlib.sha256(
                    json.dumps(
                        verification,
                        sort_keys=True,
                        allow_nan=False,
                    ).encode()
                ).hexdigest(),
            )
            calibration_rows.append(result)

            availability.extend(
                sequential_sessions(
                    owner,
                    result["idle_mean_hours"],
                    days,
                    warmup,
                    base_seed,
                    rep,
                )
            )

            if position % 10 == 0 or position == len(selection):
                print(
                    f"Availability replicate {rep}/{replicates}: "
                    f"owners={position}/{len(selection)}",
                    flush=True,
                )

        require(
            json.dumps(
                selection, sort_keys=True, allow_nan=False
            ) == snapshot,
            "Paired owner parameters were modified.",
        )

        hourly = {
            context: [0.0] * (24 * days)
            for context in CONTEXTS
        }
        for row in availability:
            row.update(
                allocate_energy(row, 24 * days, hourly)
            )

        require(
            close(
                math.fsum(
                    row["session_energy_kwh"]
                    for row in availability
                ),
                math.fsum(
                    row[field]
                    for row in availability
                    for field in ENERGY_FIELDS
                ),
            )
            and close(
                math.fsum(
                    row["energy_in_study_kwh"]
                    for row in availability
                ),
                math.fsum(
                    value
                    for series in hourly.values()
                    for value in series
                ),
            ),
            "Availability energy balance failed.",
        )

        control_profile = {
            (row["context"], row["hour_of_day"]):
            row["mean_power_kw"]
            for row in control_profiles
        }
        new_profile = {
            (context, hour):
            math.fsum(hourly[context][hour::24]) / days
            for context in CONTEXTS
            for hour in range(24)
        }
        new_profile.update(
            {
                ("TOTAL", hour): math.fsum(
                    new_profile[(context, hour)]
                    for context in CONTEXTS
                )
                for hour in range(24)
            }
        )

        for owner in selection:
            oid = owner["owner_id"]

            for code in GROUPS:
                def belonging(row):
                    return (
                        row["owner_id"] == oid
                        and (
                            code == "TOTAL"
                            or row["context"] == code
                        )
                    )

                old = [
                    row for row in control if belonging(row)
                ]
                new = [
                    row for row in availability if belonging(row)
                ]

                target = (
                    owner["total"]
                    if code == "TOTAL"
                    else owner["rates"][code]
                ) * days / 7

                old_diag = next(
                    row
                    for row in control_diagnostics
                    if row["owner_id"] == oid
                    and row["context"] == code
                )

                diagnostics.append(
                    dict(
                        replicate=rep,
                        owner_id=oid,
                        context=code,
                        target_expected_study_events=target,
                        control_candidate_starts=old_diag[
                            "study_candidate_starts"
                        ],
                        control_accepted_starts=sum(
                            row["start_phase"] == "study"
                            for row in old
                        ),
                        control_rejected_starts=old_diag[
                            "study_rejected_starts"
                        ],
                        availability_starts=sum(
                            row["start_phase"] == "study"
                            for row in new
                        ),
                        control_completed_in_study=sum(
                            0 <= row["end_hour_absolute"]
                            < 24 * days
                            for row in old
                        ),
                        availability_completed_in_study=sum(
                            0 <= row["end_hour_absolute"]
                            < 24 * days
                            for row in new
                        ),
                    )
                )

        for code in GROUPS:
            for hour in range(24):
                a = control_profile[(code, hour)]
                b = new_profile[(code, hour)]
                profiles.append(
                    dict(
                        replicate=rep,
                        context=code,
                        hour_of_day=hour,
                        control_mean_power_kw=a,
                        availability_mean_power_kw=b,
                        difference_kw=b - a,
                        output_origin="SIMULATED",
                        behaviour_input_origin="SYNTHETIC",
                    )
                )

            a = math.fsum(
                control_profile[(code, hour)]
                for hour in range(24)
            )
            b = math.fsum(
                new_profile[(code, hour)]
                for hour in range(24)
            )

            comparisons.append(
                dict(
                    replicate=rep,
                    context=code,
                    control_daily_energy_kwh=a,
                    availability_daily_energy_kwh=b,
                    difference_daily_energy_kwh=b - a,
                )
            )

        sessions_all.extend(
            dict(generator="poisson_control", **row)
            for row in control
        )
        sessions_all.extend(
            dict(generator="availability_aware", **row)
            for row in availability
        )
        rejected_all.extend(
            dict(generator="poisson_control", **row)
            for row in dropped
        )

        print(
            f"Generator pair {rep}/{replicates}: "
            "parameter pairing and energy=PASS",
            flush=True,
        )

    summary, mean_profiles = [], []

    count_fields = (
        "control_candidate_starts",
        "control_accepted_starts",
        "control_rejected_starts",
        "availability_starts",
        "control_completed_in_study",
        "availability_completed_in_study",
    )

    for code in GROUPS:
        rows = [
            row
            for row in diagnostics
            if row["context"] == code
        ]
        target = math.fsum(
            row["target_expected_study_events"]
            for row in rows
        )
        counts = {
            field: sum(row[field] for row in rows)
            for field in count_fields
        }
        daily = [
            row
            for row in comparisons
            if row["context"] == code
        ]

        summary.append(
            dict(
                context=code,
                target_expected_study_events=target,
                **counts,
                control_starts_to_target=(
                    counts["control_accepted_starts"] / target
                    if target else None
                ),
                availability_starts_to_target=(
                    counts["availability_starts"] / target
                    if target else None
                ),
                control_completions_to_target=(
                    counts["control_completed_in_study"] / target
                    if target else None
                ),
                availability_completions_to_target=(
                    counts["availability_completed_in_study"] / target
                    if target else None
                ),
                control_mean_daily_energy_kwh=mean(
                    row["control_daily_energy_kwh"]
                    for row in daily
                ),
                availability_mean_daily_energy_kwh=mean(
                    row["availability_daily_energy_kwh"]
                    for row in daily
                ),
                mean_daily_energy_difference_kwh=mean(
                    row["difference_daily_energy_kwh"]
                    for row in daily
                ),
                paired_daily_energy_mcse_kwh=(
                    stdev(
                        row["difference_daily_energy_kwh"]
                        for row in daily
                    ) / math.sqrt(replicates)
                ),
            )
        )

        for hour in range(24):
            values = [
                row
                for row in profiles
                if row["context"] == code
                and row["hour_of_day"] == hour
            ]
            require(
                len(values) == replicates,
                "Incomplete replicate profile grid.",
            )

            mean_profiles.append(
                dict(
                    context=code,
                    hour_of_day=hour,
                    n_replicates=replicates,
                    control_mean_power_kw=mean(
                        row["control_mean_power_kw"]
                        for row in values
                    ),
                    availability_mean_power_kw=mean(
                        row["availability_mean_power_kw"]
                        for row in values
                    ),
                    mean_difference_kw=mean(
                        row["difference_kw"] for row in values
                    ),
                    paired_difference_mcse_kw=(
                        stdev(
                            row["difference_kw"]
                            for row in values
                        ) / math.sqrt(replicates)
                    ),
                    output_origin="SIMULATED",
                    behaviour_input_origin="SYNTHETIC",
                )
            )

        for column, total_column in (
            (
                "control_mean_power_kw",
                "control_mean_daily_energy_kwh",
            ),
            (
                "availability_mean_power_kw",
                "availability_mean_daily_energy_kwh",
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
                        for row in mean_profiles
                        if row["context"] == code
                    ),
                    summary[-1][total_column],
                ),
                "Ensemble mean-profile energy failed.",
            )

    require(
        len(selected_all)
        == len(calibration_rows)
        == replicates * len(ids)
        and len(diagnostics) == replicates * len(ids) * 5,
        "Owner/replicate accounting failed.",
    )
    require(
        all(
            baseline.sha256(path) == digest
            for path, digest in watched.items()
        ),
        "Input changed before export.",
    )

    output = root / "data/simulated/synthetic" / (
        "availability_generator_pilot_"
        + datetime.now(timezone.utc).strftime(
            "%Y%m%dT%H%M%S_%fZ"
        )
    )
    output.mkdir(parents=True, exist_ok=False)

    fields = (
        "generator",
        "replicate",
        "output_origin",
        "behaviour_input_origin",
        *SESSION_FIELDS,
    )

    write_csv(
        output / "sessions.csv",
        sessions_all,
        (*fields, *EXTRA, *ENERGY_FIELDS),
    )
    write_csv(
        output / "control_rejected_sessions.csv",
        rejected_all,
        (
            *fields,
            "rejection_reason",
            "conflicting_session_id",
        ),
    )

    for filename, rows in (
        ("selected_owner_parameters.csv", selected_all),
        ("owner_calibration.csv", calibration_rows),
        ("owner_generator_diagnostics.csv", diagnostics),
        ("replicate_comparison.csv", comparisons),
        ("replicate_profiles.csv", profiles),
        ("mean_profile_24h.csv", mean_profiles),
        ("generator_summary.csv", summary),
    ):
        write_csv(output / filename, rows, tuple(rows[0]))

    require(
        all(
            baseline.sha256(path) == digest
            for path, digest in watched.items()
        ),
        "Input changed during export.",
    )

    report = dict(
        schema_version="availability_generator_comparison_v1",
        status="complete",
        data_origin="SYNTHETIC",
        behaviour_input_origin="SYNTHETIC",
        output_origin="SIMULATED",
        parameters_folder=bank_folder.relative_to(root).as_posix(),
        owners_simulated=len(ids),
        owner_ids=ids,
        cohort=cohort,
        energy_case=energy_case,
        power_scenario=power_scenario,
        study_days=days,
        warmup_days=warmup,
        replicates=replicates,
        base_seed=base_seed,
        calibration_events_per_owner_selection=calibration_events,
        verification_events_per_owner_selection=calibration_events,
        calibration_proxy_relative_tolerance=CALIBRATION_TOLERANCE,
        verification_development_band_percent=VERIFICATION_BAND_PERCENT,
        calibration_status_counts=dict(
            Counter(
                row["calibration_status"]
                for row in calibration_rows
            )
        ),
        verification_selections_outside_band=sum(
            not row["verification_within_development_band"]
            for row in calibration_rows
        ),
        selected_owner_parameter_pairs=len(selected_all),
        summary=summary,
        candidate_pairing=False,
        completed_frequency_agreement_established=False,
        warmup_stationarity_established=False,
        empirical_validation=False,
        national_forecast_ready=False,
        policy=[
            (
                "One vehicle per source owner. Select an admitted index "
                "uniformly within each owner; use identical energy, joint "
                "rates, home power and power laws for both generators."
            ),
            (
                "The Poisson control is rerun on this bank. Older pilot "
                "energy results use different selected parameters and "
                "are not this paired control."
            ),
            (
                "Availability rule: independently choose the next context "
                "using joint-rate shares; after the previous charge ends "
                "add an exponential idle gap, then sample uniformly within "
                "the remaining part of the first permissible window."
            ),
            (
                "This changes the timing process and its marginal "
                "within-window start distribution. A chosen next context "
                "can require waiting even if another context has an "
                "earlier window."
            ),
            (
                "Internal calibration adjusts only the idle-gap mean to "
                "a finite-prefix throughput proxy, N completions divided "
                "by elapsed hours from an empty clock. It is not empirical "
                "fitting or proof of a stationary mean."
            ),
            (
                "The same calibration event prefix is reused during "
                "bracketing/bisection. Verification and study use separate "
                "owner/replicate/component hash streams; their outcomes "
                "never tune the idle mean or select parameter indices."
            ),
            (
                "A zero-idle prefix below target is a limitation of this "
                "policy/path proxy, not proof of physical impossibility. "
                "Retain the owner at zero idle with a review flag; do not "
                "redraw or exclude it."
            ),
            (
                "The 0.2% calibration proxy tolerance and 5% verification "
                "band are development diagnostics, not statistical "
                "confidence limits or production acceptance criteria. "
                "Review owner rows as well as pooled totals."
            ),
            (
                "Study starts and literal completions in [0, study_hours) "
                "are reported separately. Warm-up starts can complete "
                "during study; sessions starting in study may finish "
                "afterwards."
            ),
            (
                "Availability sessions are constructed without overlap; "
                "zero rejection is not evidence that completed frequency "
                "matches target. This is paired on owner parameters, "
                "not on candidate events."
            ),
            (
                "Keep the constant E/P energy proxy, fixed owner usual "
                "energy and home power, and per-session non-home power. "
                "No battery clipping, added driving energy, conversion "
                "losses, site queues or dwell constraints are introduced."
            ),
            (
                "Paired energy MCSE describes the small conditional pilot "
                "over the saved bank and process streams. Input-fit "
                "uncertainty, initialization stationarity, empirical "
                "validation and production precision remain unresolved."
            ),
        ],
        checks=dict(
            source_hashes_verified=True,
            selected_owner_parameters_paired=True,
            selected_time_and_window_screens_rechecked=True,
            independent_calibration_verification_study_streams=True,
            no_owner_overlaps=True,
            session_and_hourly_energy_conserved=True,
            replicate_and_mean_profile_energy_checked=True,
            complete_owner_grid=True,
            input_hashes_unchanged=True,
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

    (output / "availability_generator_report.json").write_text(
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
    parser.add_argument("--parameters", required=True)
    parser.add_argument("--days", type=int, default=28)
    parser.add_argument("--warmup-days", type=int, default=7)
    parser.add_argument("--replicates", type=int, default=10)
    parser.add_argument("--base-seed", type=int, default=20261007)
    parser.add_argument(
        "--energy-case", choices=CASES, default=CASES[0]
    )
    parser.add_argument(
        "--power-scenario",
        choices=("reduced_test", "rated_ceiling_test"),
        default="reduced_test",
    )
    parser.add_argument(
        "--cohort",
        choices=("common_energy_cases", "open_upper_extension"),
        default="common_energy_cases",
    )
    parser.add_argument(
        "--calibration-events", type=int, default=512
    )
    args = parser.parse_args()

    output, report = run_pilot(
        args.parameters,
        args.days,
        args.warmup_days,
        args.replicates,
        args.base_seed,
        args.energy_case,
        args.power_scenario,
        args.cohort,
        args.calibration_events,
    )

    print("Paired availability-aware generator pilot complete.")
    print(f"owners_simulated: {report['owners_simulated']}")
    print(f"replicates: {report['replicates']}")
    print(
        "calibration_status_counts: "
        f"{report['calibration_status_counts']}"
    )
    print(
        "verification_selections_outside_5_percent_band: "
        f"{report['verification_selections_outside_band']}"
    )

    def show(value):
        return f"{value:.4f}" if value is not None else "NA"

    for row in report["summary"]:
        print(
            f"{row['context']}: starts/target="
            f"{show(row['control_starts_to_target'])} -> "
            f"{show(row['availability_starts_to_target'])}, "
            "completions/target="
            f"{show(row['control_completions_to_target'])} -> "
            f"{show(row['availability_completions_to_target'])}, "
            f"mean_daily_kwh="
            f"{row['control_mean_daily_energy_kwh']:.3f} -> "
            f"{row['availability_mean_daily_energy_kwh']:.3f}"
        )

    print("Parameter-pairing, no-overlap and energy checks passed.")
    print(f"Output: {output.relative_to(ROOT)}")
    print(
        "Generator sensitivity only; frequency agreement, "
        "stationarity and empirical validation remain unestablished."
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
            f"Availability-generator pilot failed: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1)