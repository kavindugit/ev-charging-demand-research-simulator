"""One reproducible synthetic session-engine run; no fleet scaling or fitting."""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from random import Random
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from ev_demand.parameters.build_public_power_layer import (
    compile_power_config,
    finite_number,
    load_json,
    positive_bounds,
    sample_session_power,
    sha256,
    validate_readiness,
)

CONTEXTS = ("T1", "T2", "T3", "T4")

DRAW_FIELDS = (
    "effective_average_power_kw",
    "power_model_kind",
    "rating_id",
    "rated_output_kw",
    "sampled_average_to_rated_fraction",
    "uncapped_effective_average_power_kw",
    "vehicle_limit_kw",
    "vehicle_limit_applied",
    "vehicle_limit_status",
    "connector_compatibility_checked",
    "power_evidence_status",
    "scenario",
)

SESSION_FIELDS = (
    "session_id",
    "owner_id",
    "data_origin",
    "context",
    "charger_code",
    "start_phase",
    "start_day_index",
    "start_hour_of_day",
    "start_hour_absolute",
    "end_hour_absolute",
    "active_duration_hours",
    "session_energy_kwh",
) + DRAW_FIELDS

ENERGY_FIELDS = (
    "energy_before_study_kwh",
    "energy_in_study_kwh",
    "energy_after_study_kwh",
)


def close(a, b):
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-7)


def validate_package(package):
    if (
        package.get("schema_version")
        != "bounded_test_parameters_with_public_power_v1"
        or package.get("data_origin") != "SYNTHETIC"
        or package.get("ready_for_synthetic_engine_test") is not True
    ):
        raise ValueError(
            "Expected the synthetic package WITH the public power layer."
        )

    for flag in (
        "empirically_calibrated",
        "district_demand_ready",
        "national_forecast_ready",
    ):
        if package.get(flag) is not False:
            raise ValueError(f"Unexpected readiness flag: {flag}.")

    layer = package["public_power_layer"]
    pools = compile_power_config(
        layer["config_snapshot"],
        layer["scenario"],
    )

    owners = package["owner_profiles"]
    summary = package["summary"]

    if (
        not owners
        or len(owners) != summary["owners_ready_for_bounded_test"]
        or summary["owners_retained_in_audit"]
        != len(owners) + summary["owners_deferred"]
    ):
        raise ValueError("Owner counts do not match the package.")

    seen = set()
    longest = 0.0

    for owner in owners:
        owner_id = owner["owner_id"]

        if (
            not owner_id
            or owner_id in seen
            or owner["data_origin"] != "SYNTHETIC"
        ):
            raise ValueError("Invalid owner identity or origin.")

        seen.add(owner_id)

        energy_low = finite_number(
            owner["session_energy_kwh_min"],
            "energy minimum",
        )
        energy_high = finite_number(
            owner["session_energy_kwh_max"],
            "energy maximum",
            positive=True,
        )

        if not 0 <= energy_low <= energy_high:
            raise ValueError("Invalid session energy bounds.")

        low = finite_number(
            owner["weekly_total_feasible_min"],
            "weekly minimum",
        )
        high = finite_number(
            owner["weekly_total_feasible_max"],
            "weekly maximum",
            positive=True,
        )

        active = owner["active_contexts"]
        codes = [row["context"] for row in active]
        inactive = owner["inactive_contexts"]

        if (
            not active
            or len(codes) != len(set(codes))
            or len(inactive) != len(set(inactive))
            or set(codes) & set(inactive)
            or set(codes) | set(inactive) != set(CONTEXTS)
        ):
            raise ValueError("Invalid owner context structure.")

        lows, highs = [], []

        for context in active:
            a = finite_number(
                context["weekly_rate_min"],
                "context minimum",
            )
            b = finite_number(
                context["weekly_rate_max"],
                "context maximum",
                positive=True,
            )

            if not 0 <= a <= b:
                raise ValueError("Invalid context frequency bounds.")

            lows.append(a)
            highs.append(b)

            start = finite_number(
                context["window_start_hour"],
                "window start",
            )
            end = finite_number(
                context["window_end_hour"],
                "window end",
            )
            length = finite_number(
                context["window_length_hours"],
                "window length",
            )

            if not (
                0 <= start < 24
                and 0 <= end < 24
                and 0 < length < 24
                and close((end - start) % 24, length)
            ):
                raise ValueError("Invalid charging-start window.")

            model = context["power_model"]

            if context["context"] == "T2":
                if model["kind"] != "reported_home_uniform":
                    raise ValueError(
                        "Home context has an unexpected power model."
                    )

                power_low, _ = positive_bounds(
                    model["effective_average_kw_min"],
                    model["effective_average_kw_max"],
                    "home power",
                )

            else:
                expected = pools[
                    (context["context"], context["charger_code"])
                ]

                if (
                    model["kind"] != "nonhome_rating_mixture"
                    or model["scenario"] != layer["scenario"]
                    or model["options"] != expected
                ):
                    raise ValueError(
                        "Non-home model differs from its configuration snapshot."
                    )

                power_low = min(
                    row["effective_average_kw_min"]
                    for row in expected
                    if row["selection_probability"] > 0
                )

            longest = max(longest, energy_high / power_low)

        if (
            not 0 <= low <= high
            or low < math.fsum(lows) - 1e-9
            or high > math.fsum(highs) + 1e-9
        ):
            raise ValueError(
                "Total and context frequency bounds disagree."
            )

    return longest


def allocate_rates(owner, rng):
    contexts = owner["active_contexts"]

    total = rng.uniform(
        owner["weekly_total_feasible_min"],
        owner["weekly_total_feasible_max"],
    )

    lower_sum = math.fsum(
        row["weekly_rate_min"]
        for row in contexts
    )

    widths = [
        row["weekly_rate_max"] - row["weekly_rate_min"]
        for row in contexts
    ]
    width_sum = math.fsum(widths)

    rates = [
        (
            row["weekly_rate_min"]
            + (total - lower_sum) * width / width_sum
            if width_sum > 0
            else row["weekly_rate_min"]
        )
        for row, width in zip(contexts, widths)
    ]

    if not close(math.fsum(rates), total):
        raise ValueError(
            "Context rates do not conserve the owner's total rate."
        )

    if any(
        rate < row["weekly_rate_min"] - 1e-9
        or rate > row["weekly_rate_max"] + 1e-9
        for rate, row in zip(rates, contexts)
    ):
        raise ValueError(
            "Allocated context rate is outside its bounds."
        )

    return total, rates


def poisson_count(mean, rng):
    # Count arrivals in a unit interval using exponential waiting times.
    if mean <= 0:
        return 0

    elapsed, count = 0.0, 0

    while True:
        elapsed += rng.expovariate(mean)

        if elapsed >= 1:
            return count

        count += 1


def schedule_owner(candidates):
    accepted, rejected = [], []
    last_end, last_id = -math.inf, ""

    ordered = sorted(
        candidates,
        key=lambda item: (
            item["start_hour_absolute"],
            item["session_id"],
        ),
    )

    for row in ordered:
        if row["session_energy_kwh"] <= 0:
            rejected.append({
                **row,
                "rejection_reason": "zero_energy_draw",
                "conflicting_session_id": "",
            })

        elif row["start_hour_absolute"] < last_end:
            rejected.append({
                **row,
                "rejection_reason": "owner_already_charging",
                "conflicting_session_id": last_id,
            })

        else:
            accepted.append(row)
            last_end = row["end_hour_absolute"]
            last_id = row["session_id"]

    return accepted, rejected


def allocate_energy(session, horizon, hourly):
    start = session["start_hour_absolute"]
    end = session["end_hour_absolute"]
    power = session["effective_average_power_kw"]

    before = power * max(
        0.0,
        min(end, 0.0) - start,
    )
    after = power * max(
        0.0,
        end - max(start, float(horizon)),
    )

    a = max(start, 0.0)
    b = min(end, float(horizon))
    inside = power * max(0.0, b - a)

    if b > a:
        for hour in range(math.floor(a), math.ceil(b)):
            overlap = max(
                0.0,
                min(b, hour + 1.0) - max(a, float(hour)),
            )
            hourly[session["context"]][hour] += power * overlap

    if not close(
        before + inside + after,
        session["session_energy_kwh"],
    ):
        raise ValueError(
            "Session energy was not conserved at the study boundary."
        )

    return dict(zip(
        ENERGY_FIELDS,
        (before, inside, after),
    ))


def simulate(package, days, seed, warmup_days=None):
    longest = validate_package(package)

    if type(days) is not int or not 1 <= days <= 366:
        raise ValueError(
            "For this first engine test, days must be between 1 and 366."
        )

    warmup = (
        math.ceil(longest / 24) + 1
        if warmup_days is None
        else warmup_days
    )

    if type(warmup) is not int or warmup < 0:
        raise ValueError(
            "Warm-up days must be a nonnegative integer."
        )

    rng = Random(seed)
    horizon = days * 24
    hourly = {
        code: [0.0] * horizon
        for code in CONTEXTS
    }

    accepted, rejected, rate_rows = [], [], []
    serial = 0

    owners = sorted(
        package["owner_profiles"],
        key=lambda row: row["owner_id"],
    )

    for owner in owners:
        total, rates = allocate_rates(owner, rng)
        contexts = owner["active_contexts"]

        rate_map = {
            row["context"]: rate
            for row, rate in zip(contexts, rates)
        }

        for code in CONTEXTS:
            rate_rows.append({
                "owner_id": owner["owner_id"],
                "data_origin": "SYNTHETIC",
                "context": code,
                "active": code in rate_map,
                "weekly_total_sampled": total,
                "weekly_context_rate": rate_map.get(code, 0.0),
            })

        candidates = []

        for day in range(-warmup, days):
            count = poisson_count(total / 7, rng)

            for _ in range(count):
                context = rng.choices(
                    contexts,
                    weights=rates,
                    k=1,
                )[0]

                clock = (
                    context["window_start_hour"]
                    + rng.random() * context["window_length_hours"]
                ) % 24

                start = day * 24 + clock

                energy = rng.uniform(
                    owner["session_energy_kwh_min"],
                    owner["session_energy_kwh_max"],
                )

                draw = sample_session_power(context, rng)
                duration = energy / draw["effective_average_power_kw"]

                serial += 1

                candidates.append({
                    "session_id": f"SIM_{serial:08d}",
                    "owner_id": owner["owner_id"],
                    "data_origin": "SYNTHETIC",
                    "context": context["context"],
                    "charger_code": context["charger_code"],
                    "start_phase": "warmup" if day < 0 else "study",
                    "start_day_index": day,
                    "start_hour_of_day": clock,
                    "start_hour_absolute": start,
                    "end_hour_absolute": start + duration,
                    "active_duration_hours": duration,
                    "session_energy_kwh": energy,
                    **draw,
                })

        kept, dropped = schedule_owner(candidates)

        if any(
            kept[i]["start_hour_absolute"]
            < kept[i - 1]["end_hour_absolute"]
            for i in range(1, len(kept))
        ):
            raise ValueError(
                "Overlapping accepted sessions for one owner."
            )

        for row in kept:
            row.update(allocate_energy(row, horizon, hourly))

        accepted.extend(kept)
        rejected.extend(dropped)

    accepted.sort(
        key=lambda row: (
            row["start_hour_absolute"],
            row["owner_id"],
        )
    )
    rejected.sort(
        key=lambda row: (
            row["start_hour_absolute"],
            row["owner_id"],
        )
    )

    hourly_total = math.fsum(
        value
        for series in hourly.values()
        for value in series
    )
    session_inside = math.fsum(
        row["energy_in_study_kwh"]
        for row in accepted
    )
    full_energy = math.fsum(
        row["session_energy_kwh"]
        for row in accepted
    )
    before = math.fsum(
        row["energy_before_study_kwh"]
        for row in accepted
    )
    after = math.fsum(
        row["energy_after_study_kwh"]
        for row in accepted
    )

    if (
        not close(hourly_total, session_inside)
        or not close(
            full_energy,
            before + hourly_total + after,
        )
    ):
        raise ValueError("Hourly energy conservation failed.")

    summary = {
        "owners_simulated": len(package["owner_profiles"]),
        "study_days": days,
        "warmup_days": warmup,
        "seed": seed,
        "candidate_sessions": serial,
        "accepted_sessions_including_warmup": len(accepted),
        "accepted_sessions_starting_in_study": sum(
            row["start_phase"] == "study"
            for row in accepted
        ),
        "rejected_sessions_including_warmup": len(rejected),
        "rejected_sessions_starting_in_study": sum(
            row["start_phase"] == "study"
            for row in rejected
        ),
        "rejection_reasons": dict(Counter(
            row["rejection_reason"]
            for row in rejected
        )),
        "full_accepted_session_energy_kwh": full_energy,
        "energy_before_study_kwh": before,
        "session_energy_in_study_kwh": session_inside,
        "hourly_energy_in_study_kwh": hourly_total,
        "energy_after_study_kwh": after,
        "energy_balance_error_kwh": hourly_total - session_inside,
        "mean_daily_energy_kwh": hourly_total / days,
        "no_owner_overlaps": True,
        "energy_conserved": True,
    }

    return accepted, rejected, rate_rows, hourly, summary


def write_csv(path, rows, fields):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def run_files(parameters, days, seed, project_root=ROOT):
    project_root = project_root.resolve()
    allowed = (
        project_root / "data/processed/synthetic"
    ).resolve()

    folder = (project_root / parameters).resolve()

    if not folder.is_relative_to(allowed):
        raise ValueError(
            "Parameters must be under data/processed/synthetic."
        )

    paths = {
        name: folder / name
        for name in (
            "simulator_parameters.json",
            "owner_readiness.csv",
            "public_power_report.json",
        )
    }

    hashes = {
        name: sha256(path)
        for name, path in paths.items()
    }

    package = load_json(paths["simulator_parameters.json"])
    report = load_json(paths["public_power_report.json"])

    if (
        report.get("schema_version") != "public_power_report_v1"
        or report.get("data_origin") != "SYNTHETIC"
    ):
        raise ValueError("Unexpected public power report.")

    for name in (
        "simulator_parameters.json",
        "owner_readiness.csv",
    ):
        if hashes[name] != report["output_sha256"][name]:
            raise ValueError(
                f"Power-package output changed: {name}."
            )

    validate_readiness(
        paths["owner_readiness.csv"],
        package,
    )

    sampler_path = (
        project_root
        / "src/ev_demand/parameters/build_public_power_layer.py"
    )
    sampler_hash = sha256(sampler_path)
    readiness_bytes = paths["owner_readiness.csv"].read_bytes()

    kept, dropped, rates, hourly, summary = simulate(
        package,
        days,
        seed,
    )

    if (
        any(
            sha256(paths[name]) != value
            for name, value in hashes.items()
        )
        or sha256(sampler_path) != sampler_hash
    ):
        raise ValueError(
            "An input or power sampler changed during simulation; rerun."
        )

    stamp = datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%S_%fZ"
    )
    output = (
        project_root
        / "data/simulated/synthetic"
        / f"sessions_{stamp}"
    )
    output.mkdir(parents=True, exist_ok=False)

    write_csv(
        output / "sessions.csv",
        kept,
        SESSION_FIELDS + ENERGY_FIELDS,
    )
    write_csv(
        output / "rejected_sessions.csv",
        dropped,
        SESSION_FIELDS + (
            "rejection_reason",
            "conflicting_session_id",
        ),
    )
    write_csv(
        output / "owner_rates.csv",
        rates,
        (
            "owner_id",
            "data_origin",
            "context",
            "active",
            "weekly_total_sampled",
            "weekly_context_rate",
        ),
    )

    series = {
        **hourly,
        "TOTAL": [
            math.fsum(
                hourly[code][h]
                for code in CONTEXTS
            )
            for h in range(days * 24)
        ],
    }

    hourly_rows = [
        {
            "day_index": h // 24,
            "hour_of_day": h % 24,
            "context": code,
            "data_origin": "SYNTHETIC",
            "energy_kwh": values[h],
            "average_power_kw": values[h],
        }
        for h in range(days * 24)
        for code, values in series.items()
    ]

    profile = [
        {
            "hour_of_day": clock,
            "context": code,
            "data_origin": "SYNTHETIC",
            "mean_power_kw": math.fsum(values[clock::24]) / days,
        }
        for clock in range(24)
        for code, values in series.items()
    ]

    write_csv(
        output / "hourly_demand.csv",
        hourly_rows,
        (
            "day_index",
            "hour_of_day",
            "context",
            "data_origin",
            "energy_kwh",
            "average_power_kw",
        ),
    )
    write_csv(
        output / "profile_24h.csv",
        profile,
        (
            "hour_of_day",
            "context",
            "data_origin",
            "mean_power_kw",
        ),
    )

    (output / "owner_readiness.csv").write_bytes(readiness_bytes)

    final_report = {
        "schema_version": "session_engine_test_v1",
        "data_origin": "SYNTHETIC",
        "summary": summary,
        "input_sha256": hashes,
        "power_sampler_sha256": sampler_hash,
        "simulator_sha256": sha256(Path(__file__)),
        "parameter_folder": folder.relative_to(
            project_root
        ).as_posix(),
        "power_scenario": package["public_power_layer"]["scenario"],
        "upstream_owner_summary": package["summary"],
        "output_sha256": {
            path.name: sha256(path)
            for path in output.glob("*.csv")
        },
        "policy": {
            "one_owner_one_vehicle": True,
            "frequency": (
                "One feasible weekly rate per owner; daily Poisson "
                "candidate counts with mean rate/7."
            ),
            "context_rates": (
                "Allocate sampled total across context bounds "
                "in proportion to available widths."
            ),
            "start_windows": (
                "Uniform reported clock window; wrap midnight; "
                "no extra TOU delay or weekday effect."
            ),
            "energy": (
                "Independent usual-energy draw per candidate; "
                "no separate distance-derived energy added."
            ),
            "power": (
                "Use existing power helper once per session; "
                "constant effective average power."
            ),
            "overlap": (
                "Sort by start time per owner; reject overlapping "
                "candidates without shifting or replacing them."
            ),
            "rates_after_rejection": (
                "Realized accepted frequency can be below proposed "
                "Poisson frequency; inspect rejections."
            ),
            "warmup": (
                "Longest possible session duration rounded to days "
                "plus one day; empty initial state, "
                "not proven stationarity."
            ),
            "boundary": (
                "Count warm-up carry-in and study-start carry-out "
                "explicitly; hourly CSV covers study only."
            ),
            "electricity": (
                "Delivered-energy load proxy; grid conversion losses "
                "and auxiliary loads are not modelled."
            ),
        },
        "limitations": [
            (
                "Single seed engineering run, not repeated Monte Carlo "
                "uncertainty or a national forecast."
            ),
            (
                "Completeness-selected synthetic owners; "
                "no empirical calibration or population weighting."
            ),
            (
                "No trip/SOC state, usable battery-capacity constraints, "
                "vehicle acceptance curves or connector matching."
            ),
            (
                "Rejected candidate energy is hypothetical; "
                "it is not measured unserved charging demand."
            ),
            (
                "No station assignment, port/site capacity, queues, "
                "travel time or public/private access enforcement."
            ),
        ],
        "empirically_validated": False,
        "national_forecast_ready": False,
    }

    (output / "simulation_report.json").write_text(
        json.dumps(
            final_report,
            indent=2,
            allow_nan=False,
        ) + "\n",
        encoding="utf-8",
    )

    return output, summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parameters", required=True)
    parser.add_argument("--days", type=int, default=28)
    parser.add_argument("--seed", type=int, default=20261007)
    args = parser.parse_args()

    output, summary = run_files(
        Path(args.parameters),
        args.days,
        args.seed,
    )

    print("First synthetic session simulation complete.")

    for key in (
        "owners_simulated",
        "study_days",
        "warmup_days",
        "seed",
        "candidate_sessions",
        "accepted_sessions_starting_in_study",
        "rejected_sessions_starting_in_study",
    ):
        print(f"{key}: {summary[key]}")

    print(
        "hourly_energy_in_study_kwh: "
        f"{summary['hourly_energy_in_study_kwh']:.6f}"
    )
    print(
        "energy_balance_error_kwh: "
        f"{summary['energy_balance_error_kwh']:.10f}"
    )
    print(f"no_owner_overlaps: {summary['no_owner_overlaps']}")
    print(f"energy_conserved: {summary['energy_conserved']}")
    print(f"Output: {output.relative_to(ROOT)}")
    print(
        "Single synthetic engine test; "
        "no national scaling or empirical validation."
    )


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError, OverflowError) as exc:
        print(
            f"Session simulation failed: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1)