"""Audit one synthetic session run before changing its model or scaling it."""
from __future__ import annotations

import argparse
from collections import defaultdict
import csv
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from ev_demand.parameters.build_public_power_layer import (
    load_json,
    sha256,
)
from ev_demand.simulation.run_session_simulator import (
    CONTEXTS,
    allocate_energy,
    close,
    schedule_owner,
    validate_package,
    write_csv,
)


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def number(row, key):
    value = float(row[key])

    if not math.isfinite(value):
        raise ValueError(f"Nonfinite value in {key}.")

    return value


def require(condition, message):
    if not condition:
        raise ValueError(message)


def parse_sessions(rows, accepted, owners, warmup, horizon):
    keys = (
        "start_hour_absolute",
        "end_hour_absolute",
        "start_hour_of_day",
        "active_duration_hours",
        "session_energy_kwh",
        "effective_average_power_kw",
    )

    for row in rows:
        require(
            row["data_origin"] == "SYNTHETIC"
            and row["owner_id"] in owners,
            "Session identity/origin does not match its parameter package.",
        )

        for key in keys:
            row[key] = number(row, key)

        start = row["start_hour_absolute"]
        end = row["end_hour_absolute"]
        clock = row["start_hour_of_day"]
        power = row["effective_average_power_kw"]
        day = int(row["start_day_index"])

        require(
            -warmup * 24 <= start < horizon
            and 0 <= clock < 24,
            "Session start is outside its allowed calendar.",
        )
        require(
            day == math.floor(start / 24)
            and close(start, day * 24 + clock),
            "Session clock/day fields disagree.",
        )
        require(
            row["start_phase"]
            == ("warmup" if start < 0 else "study"),
            "Session phase does not match its start.",
        )
        require(
            power > 0
            and end >= start
            and row["active_duration_hours"] >= 0,
            "Session duration or power is invalid.",
        )
        require(
            close(end - start, row["active_duration_hours"])
            and close(
                row["session_energy_kwh"],
                power * row["active_duration_hours"],
            ),
            "Session duration, energy and power disagree.",
        )

        owner = owners[row["owner_id"]]
        contexts = {
            item["context"]: item
            for item in owner["active_contexts"]
        }

        require(
            row["context"] in contexts,
            "Session uses an inactive context.",
        )

        context = contexts[row["context"]]

        require(
            row["charger_code"] == context["charger_code"],
            "Session charger category changed.",
        )
        require(
            (clock - context["window_start_hour"]) % 24
            < context["window_length_hours"] + 1e-9,
            "Session is outside its reported start window.",
        )
        require(
            owner["session_energy_kwh_min"] - 1e-9
            <= row["session_energy_kwh"]
            <= owner["session_energy_kwh_max"] + 1e-9,
            "Session energy is outside its owner range.",
        )

        model = context["power_model"]

        if model["kind"] == "reported_home_uniform":
            bounds = model

        else:
            options = {
                item["rating_id"]: item
                for item in model["options"]
            }

            require(
                row["rating_id"] in options,
                "Session uses an unknown charger rating.",
            )

            bounds = options[row["rating_id"]]

            require(
                close(
                    number(row, "rated_output_kw"),
                    bounds["rated_output_kw"],
                ),
                "Session rating label disagrees.",
            )

        require(
            bounds["effective_average_kw_min"] - 1e-9
            <= power
            <= bounds["effective_average_kw_max"] + 1e-9,
            "Session power is outside the configured test range.",
        )

        if accepted:
            require(
                row["session_energy_kwh"] > 0,
                "Accepted session has no active energy.",
            )

            for key in (
                "energy_before_study_kwh",
                "energy_in_study_kwh",
                "energy_after_study_kwh",
            ):
                row[key] = number(row, key)

    return rows


def group_diagnostics(label, accepted, rejected, proposed_rate):
    kept = [
        row for row in accepted
        if row["start_phase"] == "study"
    ]
    dropped = [
        row for row in rejected
        if row["start_phase"] == "study"
    ]

    count = len(kept) + len(dropped)

    durations = [
        row["active_duration_hours"]
        for row in kept
    ]
    powers = [
        row["effective_average_power_kw"]
        for row in kept
    ]

    return {
        "group": label,
        "study_candidate_starts": count,
        "study_accepted_starts": len(kept),
        "study_rejected_starts": len(dropped),
        "study_rejection_fraction": (
            len(dropped) / count if count else None
        ),
        "sampled_weekly_candidate_rate": proposed_rate,
        "study_energy_kwh_including_carry_in": math.fsum(
            row["energy_in_study_kwh"]
            for row in accepted
        ),
        "median_accepted_study_duration_hours": (
            statistics.median(durations)
            if durations else None
        ),
        "max_accepted_study_duration_hours": (
            max(durations) if durations else None
        ),
        "mean_accepted_study_effective_power_kw": (
            statistics.mean(powers) if powers else None
        ),
        "rejected_study_candidate_energy_kwh": math.fsum(
            row["session_energy_kwh"]
            for row in dropped
        ),
    }


def audit_run(run, project_root=ROOT):
    project_root = project_root.resolve()
    run = (project_root / run).resolve()

    require(
        run.is_relative_to(
            (project_root / "data/simulated/synthetic").resolve()
        ),
        "Run must be under data/simulated/synthetic.",
    )

    names = (
        "sessions.csv",
        "rejected_sessions.csv",
        "owner_rates.csv",
        "hourly_demand.csv",
        "profile_24h.csv",
        "owner_readiness.csv",
        "simulation_report.json",
    )

    paths = {
        name: run / name
        for name in names
    }
    hashes = {
        name: sha256(path)
        for name, path in paths.items()
    }

    report = load_json(paths["simulation_report.json"])

    require(
        report.get("schema_version") == "session_engine_test_v1"
        and report.get("data_origin") == "SYNTHETIC",
        "Unexpected simulation report.",
    )

    for name in names[:-1]:
        require(
            hashes[name] == report["output_sha256"][name],
            f"Output changed since the run: {name}.",
        )

    folder = (
        project_root / report["parameter_folder"]
    ).resolve()

    require(
        folder.is_relative_to(
            (project_root / "data/processed/synthetic").resolve()
        ),
        "Invalid parameter-folder reference.",
    )

    inputs = {
        name: folder / name
        for name in (
            "simulator_parameters.json",
            "owner_readiness.csv",
            "public_power_report.json",
        )
    }

    for name, path in inputs.items():
        require(
            sha256(path) == report["input_sha256"][name],
            f"Source package changed: {name}.",
        )

    sampler = (
        project_root
        / "src/ev_demand/parameters/build_public_power_layer.py"
    )
    simulator = (
        project_root
        / "src/ev_demand/simulation/run_session_simulator.py"
    )

    require(
        sha256(sampler) == report["power_sampler_sha256"],
        "Power sampler changed since the run.",
    )
    require(
        sha256(simulator) == report["simulator_sha256"],
        "Simulator changed since the run.",
    )

    package = load_json(inputs["simulator_parameters.json"])
    validate_package(package)

    owners = {
        owner["owner_id"]: owner
        for owner in package["owner_profiles"]
    }

    summary = report["summary"]
    days = summary["study_days"]
    warmup = summary["warmup_days"]

    require(
        type(days) is int
        and days > 0
        and type(warmup) is int
        and warmup >= 0,
        "Invalid study or warm-up length.",
    )

    horizon = days * 24

    accepted = parse_sessions(
        read_csv(paths["sessions.csv"]),
        True,
        owners,
        warmup,
        horizon,
    )
    rejected = parse_sessions(
        read_csv(paths["rejected_sessions.csv"]),
        False,
        owners,
        warmup,
        horizon,
    )

    ids = [
        row["session_id"]
        for row in accepted + rejected
    ]

    require(
        len(ids) == len(set(ids)),
        "Duplicate session IDs across accepted/rejected files.",
    )
    require(
        len(ids) == summary["candidate_sessions"]
        and len(owners) == summary["owners_simulated"],
        "Candidate or owner counts disagree with the report.",
    )
    require(
        len(accepted) == summary["accepted_sessions_including_warmup"]
        and len(rejected)
        == summary["rejected_sessions_including_warmup"],
        "Session counts disagree.",
    )

    for rows, key in (
        (accepted, "accepted_sessions_starting_in_study"),
        (rejected, "rejected_sessions_starting_in_study"),
    ):
        require(
            sum(row["start_phase"] == "study" for row in rows)
            == summary[key],
            "Study-start counts disagree with the report.",
        )

    accepted_by_owner = defaultdict(list)
    rejected_by_owner = defaultdict(list)

    for row in accepted:
        accepted_by_owner[row["owner_id"]].append(row)

    for row in rejected:
        rejected_by_owner[row["owner_id"]].append(row)

    for owner_id in owners:
        original = accepted_by_owner[owner_id]
        dropped = rejected_by_owner[owner_id]

        kept_replay, dropped_replay = schedule_owner(
            original + dropped
        )

        require(
            {row["session_id"] for row in kept_replay}
            == {row["session_id"] for row in original},
            "Accepted schedule does not match the stated rejection policy.",
        )

        reasons = {
            row["session_id"]: (
                row["rejection_reason"],
                row["conflicting_session_id"],
            )
            for row in dropped_replay
        }

        require(
            all(
                reasons.get(row["session_id"])
                == (
                    row["rejection_reason"],
                    row["conflicting_session_id"],
                )
                for row in dropped
            ),
            "Rejected-session reason or conflict reference is inconsistent.",
        )

    hourly = {
        code: [0.0] * horizon
        for code in CONTEXTS
    }

    for row in accepted:
        partition = allocate_energy(row, horizon, hourly)

        require(
            all(
                close(row[key], value)
                for key, value in partition.items()
            ),
            "Exported session boundary energy disagrees with its timeline.",
        )

    series = {
        **hourly,
        "TOTAL": [
            math.fsum(hourly[code][h] for code in CONTEXTS)
            for h in range(horizon)
        ],
    }

    seen = set()

    for row in read_csv(paths["hourly_demand.csv"]):
        day = int(row["day_index"])
        clock = int(row["hour_of_day"])
        code = row["context"]
        key = (day, clock, code)

        require(
            0 <= day < days
            and 0 <= clock < 24
            and code in series
            and key not in seen,
            "Hourly file has duplicate or invalid rows.",
        )

        seen.add(key)
        expected = series[code][day * 24 + clock]

        require(
            row["data_origin"] == "SYNTHETIC"
            and close(number(row, "energy_kwh"), expected)
            and close(number(row, "average_power_kw"), expected),
            "Hourly CSV does not match session allocation.",
        )

    require(
        len(seen) == horizon * 5,
        "Hourly file is missing intervals or contexts.",
    )

    profile = {}
    seen = set()

    for row in read_csv(paths["profile_24h.csv"]):
        clock = int(row["hour_of_day"])
        code = row["context"]
        key = (clock, code)

        require(
            0 <= clock < 24
            and code in series
            and key not in seen,
            "Invalid or duplicate profile row.",
        )

        seen.add(key)
        value = number(row, "mean_power_kw")

        require(
            row["data_origin"] == "SYNTHETIC"
            and close(
                value,
                math.fsum(series[code][clock::24]) / days,
            ),
            "24-hour profile does not match the hourly series.",
        )

        profile[key] = value

    require(
        len(profile) == 120,
        "24-hour profile is incomplete.",
    )

    rates = {}
    totals = {}

    for row in read_csv(paths["owner_rates.csv"]):
        owner_id = row["owner_id"]
        code = row["context"]
        key = (owner_id, code)

        require(
            owner_id in owners
            and code in CONTEXTS
            and key not in rates
            and row["data_origin"] == "SYNTHETIC",
            "Invalid or duplicate owner-rate row.",
        )

        rate = number(row, "weekly_context_rate")
        total = number(row, "weekly_total_sampled")

        require(
            rate >= 0
            and total >= 0
            and (
                owner_id not in totals
                or close(total, totals[owner_id])
            ),
            "Owner-rate rows have inconsistent totals.",
        )

        rates[key] = rate
        totals[owner_id] = total

    require(
        len(rates) == len(owners) * 4,
        "Owner-rate file is incomplete.",
    )

    for owner_id in owners:
        require(
            close(
                math.fsum(
                    rates[(owner_id, code)]
                    for code in CONTEXTS
                ),
                totals[owner_id],
            ),
            "Context rates do not sum to the owner's sampled total.",
        )

        owner = owners[owner_id]

        require(
            owner["weekly_total_feasible_min"] - 1e-9
            <= totals[owner_id]
            <= owner["weekly_total_feasible_max"] + 1e-9,
            "Sampled owner rate is outside its bounds.",
        )

        active = {
            row["context"]: row
            for row in owner["active_contexts"]
        }

        for code in CONTEXTS:
            rate = rates[(owner_id, code)]

            require(
                (
                    active[code]["weekly_rate_min"] - 1e-9
                    <= rate
                    <= active[code]["weekly_rate_max"] + 1e-9
                )
                if code in active
                else close(rate, 0),
                "Context rate is outside its source bounds.",
            )

    energy = math.fsum(series["TOTAL"])

    require(
        close(energy, summary["hourly_energy_in_study_kwh"])
        and close(energy, summary["session_energy_in_study_kwh"]),
        "Study-energy totals disagree.",
    )

    require(
        close(
            math.fsum(
                row["session_energy_kwh"]
                for row in accepted
            ),
            math.fsum(
                row["energy_before_study_kwh"]
                + row["energy_in_study_kwh"]
                + row["energy_after_study_kwh"]
                for row in accepted
            ),
        ),
        "Full session energy does not balance.",
    )

    for field in (
        "energy_before_study_kwh",
        "energy_after_study_kwh",
    ):
        require(
            close(
                math.fsum(row[field] for row in accepted),
                summary[field],
            ),
            "Boundary-energy report disagrees.",
        )

    require(
        close(
            math.fsum(
                row["session_energy_kwh"]
                for row in accepted
            ),
            summary["full_accepted_session_energy_kwh"],
        ),
        "Full accepted-energy report disagrees.",
    )

    require(
        paths["owner_readiness.csv"].read_bytes()
        == inputs["owner_readiness.csv"].read_bytes(),
        "Owner-readiness audit was not preserved exactly.",
    )

    by_context = [
        group_diagnostics(
            code,
            [row for row in accepted if row["context"] == code],
            [row for row in rejected if row["context"] == code],
            math.fsum(
                rates[(owner_id, code)]
                for owner_id in owners
            ),
        )
        for code in CONTEXTS
    ]

    by_owner = [
        group_diagnostics(
            owner_id,
            accepted_by_owner[owner_id],
            rejected_by_owner[owner_id],
            totals[owner_id],
        )
        for owner_id in sorted(owners)
    ]

    for row in by_context + by_owner:
        row["realized_weekly_candidate_starts"] = (
            row["study_candidate_starts"] * 7 / days
        )
        row["realized_weekly_accepted_starts"] = (
            row["study_accepted_starts"] * 7 / days
        )

    total = group_diagnostics(
        "TOTAL",
        accepted,
        rejected,
        math.fsum(totals.values()),
    )

    mean_peak = max(
        profile[(clock, "TOTAL")]
        for clock in range(24)
    )

    peak_hours = [
        clock
        for clock in range(24)
        if close(profile[(clock, "TOTAL")], mean_peak)
    ]

    audit = {
        "schema_version": "session_run_audit_v1",
        "data_origin": "SYNTHETIC",
        "run_folder": run.relative_to(project_root).as_posix(),
        "study_days": days,
        "owners_simulated": len(owners),
        "study_counts": total,
        "owners_with_study_rejections": sum(
            row["study_rejected_starts"] > 0
            for row in by_owner
        ),
        "mean_daily_energy_kwh": energy / days,
        "mean_profile_peak_kw": mean_peak,
        "mean_profile_peak_hours": peak_hours,
        "largest_hourly_power_kw_in_run": max(series["TOTAL"]),
        "checks": {
            "output_hashes_match": True,
            "scheduling_replay_matches": True,
            "session_energy_balances": True,
            "hourly_series_matches_sessions": True,
            "profile_matches_hourly_series": True,
            "owner_readiness_preserved": True,
        },
        "context_diagnostics": by_context,
        "input_sha256": hashes,
        "interpretation": [
            (
                "Rejection fraction uses candidate starts in the study "
                "only; warm-up starts are excluded."
            ),
            (
                "Study energy includes charging carried in from warm-up "
                "and excludes charging after the horizon."
            ),
            (
                "Observed weekly start counts need not equal sampled "
                "Poisson rates in a finite run."
            ),
            (
                "Rejected candidate energy is hypothetical, "
                "not measured unserved demand."
            ),
            (
                "Rejection changes the realised frequency and energy; "
                "assess it before scaling or comparing scenarios."
            ),
            (
                "No universal acceptable rejection threshold "
                "is established by this engineering audit."
            ),
            (
                "This audit does not calibrate parameters, change "
                "the model or validate national demand."
            ),
        ],
    }

    require(
        all(
            sha256(paths[name]) == value
            for name, value in hashes.items()
        ),
        "Run files changed during auditing.",
    )

    for name, path in inputs.items():
        require(
            sha256(path) == report["input_sha256"][name],
            "Parameters changed during auditing.",
        )

    stamp = datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%S_%fZ"
    )

    output = (
        project_root
        / "data/processed/synthetic"
        / f"session_audit_{stamp}"
    )
    output.mkdir(parents=True, exist_ok=False)

    fields = tuple(by_context[0])

    write_csv(
        output / "context_diagnostics.csv",
        by_context,
        fields,
    )
    write_csv(
        output / "owner_diagnostics.csv",
        by_owner,
        fields,
    )

    audit["output_sha256"] = {
        path.name: sha256(path)
        for path in output.glob("*.csv")
    }

    (output / "session_audit_report.json").write_text(
        json.dumps(
            audit,
            indent=2,
            allow_nan=False,
        ) + "\n",
        encoding="utf-8",
    )

    return output, audit, by_owner


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    args = parser.parse_args()

    output, audit, owners = audit_run(Path(args.run))
    counts = audit["study_counts"]
    fraction = counts["study_rejection_fraction"]

    print("Session-run audit complete.")
    print(f"study_candidate_starts: {counts['study_candidate_starts']}")
    print(f"study_accepted_starts: {counts['study_accepted_starts']}")
    print(f"study_rejected_starts: {counts['study_rejected_starts']}")
    print(
        "study_rejection_percent: "
        + (
            f"{fraction * 100:.2f}"
            if fraction is not None
            else "NA"
        )
    )
    print(
        "owners_with_study_rejections: "
        f"{audit['owners_with_study_rejections']}"
    )

    for row in audit["context_diagnostics"]:
        fraction = row["study_rejection_fraction"]
        percent = (
            f"{fraction * 100:.2f}%"
            if fraction is not None
            else "NA"
        )

        print(
            f"{row['group']}: "
            f"candidates={row['study_candidate_starts']}, "
            f"accepted={row['study_accepted_starts']}, "
            f"rejected={row['study_rejected_starts']}, "
            f"rejection={percent}"
        )

    print(
        "mean_daily_energy_kwh: "
        f"{audit['mean_daily_energy_kwh']:.6f}"
    )
    print(
        "mean_profile_peak_kw: "
        f"{audit['mean_profile_peak_kw']:.6f}"
    )
    print(
        "mean_profile_peak_hours: "
        f"{audit['mean_profile_peak_hours']}"
    )

    print("Top owners by rejected study starts:")

    top = sorted(
        owners,
        key=lambda row: (
            -row["study_rejected_starts"],
            row["group"],
        ),
    )[:5]

    for row in top:
        print(
            f"{row['group']}: "
            f"candidates={row['study_candidate_starts']}, "
            f"rejected={row['study_rejected_starts']}"
        )

    print("All export and accounting checks passed.")
    print(f"Output: {output.relative_to(ROOT)}")
    print(
        "Diagnostics only; source run unchanged; "
        "no empirical validation."
    )


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError, OverflowError) as exc:
        print(
            f"Session-run audit failed: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1)
        