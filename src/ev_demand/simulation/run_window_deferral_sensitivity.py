"""Compare a saved control pilot with same-day, start-window-preserving deferral."""
from __future__ import annotations

import argparse
from collections import defaultdict
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
    SESSION_FIELDS,
    ENERGY_FIELDS,
    allocate_energy,
    close,
    schedule_owner,
    write_csv,
)

GROUPS = (*CONTEXTS, "TOTAL")

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

EXTRA = (
    "proposal_start_hour_absolute",
    "latest_start_exclusive_hour_absolute",
    "deferral_hours",
)

CHANGED = {
    "start_hour_absolute",
    "start_hour_of_day",
    "end_hour_absolute",
}

AUDIT_FIELDS = (
    "replicate",
    "owner_id",
    "session_id",
    "context",
    "start_phase",
    "proposal_start_hour_absolute",
    "latest_start_exclusive_hour_absolute",
    "control_accepted",
    "deferral_accepted",
    "accepted_start_hour_absolute",
    "deferral_hours",
)


def start_limit(row, window):
    start = float(window["start_hour"])
    length = float(window["length_hours"])

    require(
        math.isfinite(start)
        and math.isfinite(length)
        and 0 <= start < 24
        and 0 < length <= 24,
        "Invalid source start window.",
    )

    offset = (row["start_hour_of_day"] - start) % 24

    require(
        offset < length + 1e-9,
        "A source candidate starts outside its reported window.",
    )

    occurrence_end = row["start_hour_absolute"] - offset + length

    return min(
        occurrence_end,
        24 * (row["start_day_index"] + 1),
    )


def defer_owner(candidates, windows):
    accepted, rejected = [], []
    last_end, last_id = -math.inf, ""

    for original in sorted(
        candidates,
        key=lambda r: (r["start_hour_absolute"], r["session_id"]),
    ):
        row = dict(original)
        proposed = row["start_hour_absolute"]
        latest = start_limit(row, windows[row["context"]])
        actual = max(proposed, last_end)

        row.update(
            proposal_start_hour_absolute=proposed,
            latest_start_exclusive_hour_absolute=latest,
            deferral_hours=None,
        )

        if actual >= latest:
            row.update(
                rejection_reason="owner_busy_until_start_window_closes",
                conflicting_session_id=last_id,
            )
            rejected.append(row)
            continue

        row.update(
            start_hour_absolute=actual,
            start_hour_of_day=actual % 24,
            end_hour_absolute=actual + row["active_duration_hours"],
            deferral_hours=actual - proposed,
        )

        require(
            math.floor(actual / 24) == row["start_day_index"]
            and (
                row["start_hour_of_day"]
                - windows[row["context"]]["start_hour"]
            ) % 24
            < windows[row["context"]]["length_hours"] + 1e-9,
            "A delayed start left its original calendar day or start window.",
        )

        accepted.append(row)
        last_end = row["end_hour_absolute"]
        last_id = row["session_id"]

    require(
        len(accepted) + len(rejected) == len(candidates)
        and all(
            b["start_hour_absolute"] >= a["end_hour_absolute"]
            for a, b in zip(accepted, accepted[1:])
        ),
        "Deferral candidate accounting or no-overlap check failed.",
    )

    return accepted, rejected


def profile_sessions(sessions, days):
    hourly = {c: [0.0] * (24 * days) for c in CONTEXTS}

    for row in sessions:
        row.update(allocate_energy(row, 24 * days, hourly))

    inside = math.fsum(r["energy_in_study_kwh"] for r in sessions)

    require(
        close(
            inside,
            math.fsum(v for series in hourly.values() for v in series),
        )
        and close(
            math.fsum(r["session_energy_kwh"] for r in sessions),
            math.fsum(r[k] for r in sessions for k in ENERGY_FIELDS),
        ),
        "Session energy balance failed.",
    )

    profile = {
        (c, h): math.fsum(hourly[c][h::24]) / days
        for c in CONTEXTS
        for h in range(24)
    }

    profile.update({
        ("TOTAL", h): math.fsum(profile[(c, h)] for c in CONTEXTS)
        for h in range(24)
    })

    require(
        close(
            math.fsum(profile[("TOTAL", h)] for h in range(24)),
            inside / days,
        ),
        "Profile and daily energy disagree.",
    )

    return profile


def run_sensitivity(pilot_folder, project_root=ROOT):
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

    watch("src/ev_demand/simulation/run_window_deferral_sensitivity.py")

    population_path = watch(
        Path(source["model_folder"]) / "behaviour_population_model.json"
    )

    require(
        population_path.relative_to(root).as_posix()
        in source["input_sha256"],
        "The timing model was not recorded in the control pilot.",
    )

    population = json.loads(
        population_path.read_text(encoding="utf-8-sig")
    )

    windows = {
        r["owner_id"]: {
            c["legacy_context"]: c["timing_rule"]
            for c in r["contexts"]
            if c["timing_rule"] is not None
        }
        for r in population["owners"]
    }

    days, warmup, replicates = [
        source[k]
        for k in ("study_days", "warmup_days", "replicates")
    ]

    ids = source["owner_ids"]

    require(
        type(days) is int
        and days > 0
        and type(warmup) is int
        and warmup >= 0
        and type(replicates) is int
        and 2 <= replicates <= 20
        and ids
        and len(ids) == len(set(ids)) == source["owners_simulated"],
        "Invalid source settings.",
    )

    parameters = baseline.read_csv(
        pilot / "selected_owner_parameters.csv"
    )

    parameter_map = {
        (int(r["replicate"]), r["owner_id"]): r
        for r in parameters
    }

    require(
        len(parameter_map) == len(parameters) == replicates * len(ids)
        and set(parameter_map) == {
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
        len(targets)
        == len(frequency)
        == replicates * len(ids) * len(GROUPS)
        and set(targets) == {
            (r, oid, c)
            for r in range(1, replicates + 1)
            for oid in ids
            for c in GROUPS
        },
        "Incomplete source frequency grid.",
    )

    raw_profiles = baseline.read_csv(
        pilot / "replicate_profiles.csv"
    )

    original_profiles = {
        (
            int(r["replicate"]),
            r["context"],
            int(r["hour_of_day"]),
        ): float(r["mean_power_kw"])
        for r in raw_profiles
    }

    require(
        len(original_profiles) == len(raw_profiles) == replicates * 120
        and set(original_profiles) == {
            (r, c, h)
            for r in range(1, replicates + 1)
            for c in GROUPS
            for h in range(24)
        },
        "Incomplete source profile grid.",
    )

    source_kept, source_dropped = {}, {}
    candidates = defaultdict(list)

    for name, index in (
        ("sessions.csv", source_kept),
        ("rejected_sessions.csv", source_dropped),
    ):
        for raw in baseline.read_csv(pilot / name):
            rep = int(raw["replicate"])
            row = {k: raw[k] for k in SESSION_FIELDS}

            for k in (
                *CHANGED,
                "active_duration_hours",
                "session_energy_kwh",
                "effective_average_power_kw",
            ):
                row[k] = float(row[k])

            row["start_day_index"] = int(row["start_day_index"])
            key = rep, row["session_id"]

            require(
                key not in source_kept
                and key not in source_dropped
                and 1 <= rep <= replicates
                and row["owner_id"] in ids
                and row["context"] in CONTEXTS
                and raw["output_origin"] == "SIMULATED"
                and raw["behaviour_input_origin"] == "SYNTHETIC"
                and row["data_origin"] == "SYNTHETIC",
                "Invalid candidate identity or origin.",
            )

            require(
                all(
                    math.isfinite(row[k])
                    for k in (
                        *CHANGED,
                        "active_duration_hours",
                        "session_energy_kwh",
                        "effective_average_power_kw",
                    )
                )
                and min(
                    row[k]
                    for k in (
                        "active_duration_hours",
                        "session_energy_kwh",
                        "effective_average_power_kw",
                    )
                ) > 0
                and -warmup <= row["start_day_index"] < days
                and 0 <= row["start_hour_of_day"] < 24
                and close(
                    row["start_hour_absolute"],
                    row["start_day_index"] * 24
                    + row["start_hour_of_day"],
                )
                and close(
                    row["end_hour_absolute"]
                    - row["start_hour_absolute"],
                    row["active_duration_hours"],
                )
                and close(
                    row["active_duration_hours"]
                    * row["effective_average_power_kw"],
                    row["session_energy_kwh"],
                )
                and row["start_phase"] == (
                    "warmup" if row["start_day_index"] < 0 else "study"
                ),
                "Invalid source duration, power, energy or start boundary.",
            )

            require(
                row["session_energy_kwh"] == float(
                    parameter_map[(rep, row["owner_id"])][
                        "usual_energy_kwh"
                    ]
                ),
                "A source energy differs from its selected owner parameter.",
            )

            start_limit(
                row,
                windows[row["owner_id"]][row["context"]],
            )

            index[key] = raw
            candidates[(rep, row["owner_id"])].append(row)

    kept_all, dropped_all = [], []
    audit, owner_rows, profiles, comparisons = [], [], [], []

    for rep in range(1, replicates + 1):
        kept_pair = [[], []]
        dropped_after = []

        for oid in ids:
            proposals = candidates[(rep, oid)]

            before, rejected_before = schedule_owner(
                [dict(r) for r in proposals]
            )

            after, rejected_after = defer_owner(
                proposals,
                windows[oid],
            )

            require(
                {r["session_id"] for r in before} == {
                    sid
                    for (r, sid), v in source_kept.items()
                    if r == rep and v["owner_id"] == oid
                },
                "Control accepted sessions did not replay.",
            )

            for row in rejected_before:
                raw = source_dropped[(rep, row["session_id"])]

                require(
                    row["rejection_reason"] == raw["rejection_reason"]
                    and row["conflicting_session_id"]
                    == raw["conflicting_session_id"],
                    "Control rejection did not replay.",
                )

            a, b = [
                {r["session_id"]: r for r in rows}
                for rows in (before, after)
            ]

            after_map = {
                r["session_id"]: r
                for r in after + rejected_after
            }

            for row in proposals:
                sid = row["session_id"]
                alternative = after_map[sid]

                require(
                    all(
                        row[k] == alternative[k]
                        for k in SESSION_FIELDS
                        if k not in CHANGED
                    ),
                    "A fixed candidate attribute changed.",
                )

                audit.append(dict(
                    replicate=rep,
                    owner_id=oid,
                    session_id=sid,
                    context=row["context"],
                    start_phase=row["start_phase"],
                    proposal_start_hour_absolute=row[
                        "start_hour_absolute"
                    ],
                    latest_start_exclusive_hour_absolute=alternative[
                        "latest_start_exclusive_hour_absolute"
                    ],
                    control_accepted=sid in a,
                    deferral_accepted=sid in b,
                    accepted_start_hour_absolute=(
                        b[sid]["start_hour_absolute"]
                        if sid in b else None
                    ),
                    deferral_hours=(
                        b[sid]["deferral_hours"]
                        if sid in b else None
                    ),
                ))

            for code in GROUPS:
                def belongs(row):
                    return (
                        row["start_phase"] == "study"
                        and (
                            code == "TOTAL"
                            or row["context"] == code
                        )
                    )

                ac, bc = [
                    {
                        sid
                        for sid, row in mapping.items()
                        if belongs(row)
                    }
                    for mapping in (a, b)
                ]

                n = sum(belongs(row) for row in proposals)
                target = targets[(rep, oid, code)]

                require(
                    int(target["study_candidate_starts"]) == n
                    and int(target["study_accepted_starts"]) == len(ac)
                    and int(target["study_rejected_starts"])
                    == n - len(ac),
                    "Source frequency counts did not replay.",
                )

                gained = len(bc - ac)
                lost = len(ac - bc)

                require(
                    len(bc) - len(ac) == gained - lost,
                    "Gained/lost acceptance accounting failed.",
                )

                owner_rows.append(dict(
                    replicate=rep,
                    owner_id=oid,
                    context=code,
                    target_expected_study_starts=float(
                        target["target_expected_study_starts"]
                    ),
                    study_candidate_starts=n,
                    control_accepted_starts=len(ac),
                    deferral_accepted_starts=len(bc),
                    control_rejected_starts=n - len(ac),
                    deferral_rejected_starts=n - len(bc),
                    gained_accepted_starts=gained,
                    lost_accepted_starts=lost,
                    delayed_accepted_starts=sum(
                        b[sid]["deferral_hours"] > 0
                        for sid in bc
                    ),
                ))

            for i, rows in enumerate((before, after)):
                kept_pair[i].extend(rows)

            dropped_after.extend(rejected_after)

        before_profile, after_profile = [
            profile_sessions(rows, days)
            for rows in kept_pair
        ]

        for row in kept_pair[0]:
            raw = source_kept[(rep, row["session_id"])]

            require(
                all(
                    close(row[k], float(raw[k]))
                    for k in ENERGY_FIELDS
                ),
                "Control boundary energy did not replay.",
            )

        for c in GROUPS:
            for h in range(24):
                old = before_profile[(c, h)]
                new = after_profile[(c, h)]

                require(
                    close(old, original_profiles[(rep, c, h)]),
                    "Control hourly profile did not replay.",
                )

                profiles.append(dict(
                    replicate=rep,
                    context=c,
                    hour_of_day=h,
                    control_mean_power_kw=old,
                    deferral_mean_power_kw=new,
                    difference_kw=new - old,
                    output_origin="SIMULATED",
                    behaviour_input_origin="SYNTHETIC",
                ))

            energy_before = math.fsum(
                before_profile[(c, h)] for h in range(24)
            )
            energy_after = math.fsum(
                after_profile[(c, h)] for h in range(24)
            )

            comparisons.append(dict(
                replicate=rep,
                context=c,
                control_daily_energy_kwh=energy_before,
                deferral_daily_energy_kwh=energy_after,
                difference_daily_energy_kwh=(
                    energy_after - energy_before
                ),
            ))

        kept_all.extend(
            dict(
                replicate=rep,
                output_origin="SIMULATED",
                behaviour_input_origin="SYNTHETIC",
                **r,
            )
            for r in kept_pair[1]
        )

        dropped_all.extend(
            dict(
                replicate=rep,
                output_origin="SIMULATED",
                behaviour_input_origin="SYNTHETIC",
                **r,
            )
            for r in dropped_after
        )

        print(
            f"Window-deferral pair {rep}/{replicates}: "
            "replay, pairing and energy=PASS",
            flush=True,
        )

    mean_profiles, summary = [], []

    count_fields = (
        "study_candidate_starts",
        "control_accepted_starts",
        "deferral_accepted_starts",
        "control_rejected_starts",
        "deferral_rejected_starts",
        "gained_accepted_starts",
        "lost_accepted_starts",
        "delayed_accepted_starts",
    )

    for c in GROUPS:
        rows = [r for r in owner_rows if r["context"] == c]

        target = math.fsum(
            r["target_expected_study_starts"] for r in rows
        )

        counts = {
            k: sum(r[k] for r in rows)
            for k in count_fields
        }

        n = counts["study_candidate_starts"]

        daily_rows = [
            r for r in comparisons if r["context"] == c
        ]

        differences = [
            r["difference_daily_energy_kwh"]
            for r in daily_rows
        ]

        summary.append(dict(
            context=c,
            target_expected_study_starts=target,
            **counts,
            control_accepted_to_target_ratio=(
                counts["control_accepted_starts"] / target
                if target else None
            ),
            deferral_accepted_to_target_ratio=(
                counts["deferral_accepted_starts"] / target
                if target else None
            ),
            control_rejection_percent=(
                100 * counts["control_rejected_starts"] / n
                if n else None
            ),
            deferral_rejection_percent=(
                100 * counts["deferral_rejected_starts"] / n
                if n else None
            ),
            control_mean_daily_energy_kwh=mean(
                r["control_daily_energy_kwh"]
                for r in daily_rows
            ),
            deferral_mean_daily_energy_kwh=mean(
                r["deferral_daily_energy_kwh"]
                for r in daily_rows
            ),
            mean_daily_energy_difference_kwh=mean(differences),
            paired_daily_energy_mcse_kwh=(
                stdev(differences) / math.sqrt(replicates)
            ),
        ))

        for h in range(24):
            hourly = [
                r for r in profiles
                if r["context"] == c and r["hour_of_day"] == h
            ]

            require(
                len(hourly) == replicates,
                "Incomplete paired profile grid.",
            )

            mean_profiles.append(dict(
                context=c,
                hour_of_day=h,
                n_replicates=replicates,
                control_mean_power_kw=mean(
                    r["control_mean_power_kw"] for r in hourly
                ),
                deferral_mean_power_kw=mean(
                    r["deferral_mean_power_kw"] for r in hourly
                ),
                mean_difference_kw=mean(
                    r["difference_kw"] for r in hourly
                ),
                paired_difference_mcse_kw=(
                    stdev(r["difference_kw"] for r in hourly)
                    / math.sqrt(replicates)
                ),
                output_origin="SIMULATED",
                behaviour_input_origin="SYNTHETIC",
            ))

        for column, daily_column in (
            ("control_mean_power_kw", "control_mean_daily_energy_kwh"),
            ("deferral_mean_power_kw", "deferral_mean_daily_energy_kwh"),
            ("mean_difference_kw", "mean_daily_energy_difference_kwh"),
        ):
            require(
                close(
                    math.fsum(
                        r[column]
                        for r in mean_profiles
                        if r["context"] == c
                    ),
                    summary[-1][daily_column],
                ),
                "Ensemble profile does not conserve daily energy.",
            )

    require(
        len(audit) == len(source_kept) + len(source_dropped)
        and len(kept_all) + len(dropped_all) == len(audit),
        "Full candidate pairing failed.",
    )

    require(
        all(
            baseline.sha256(p) == digest
            for p, digest in watched.items()
        ),
        "An input changed during comparison.",
    )

    output = (
        root / "data/simulated/synthetic"
        / (
            "window_deferral_sensitivity_"
            + datetime.now(timezone.utc).strftime(
                "%Y%m%dT%H%M%S_%fZ"
            )
        )
    )

    output.mkdir(parents=True, exist_ok=False)

    prefix = (
        "replicate",
        "output_origin",
        "behaviour_input_origin",
        *SESSION_FIELDS,
        *EXTRA,
    )

    write_csv(
        output / "sessions.csv",
        kept_all,
        (*prefix, *ENERGY_FIELDS),
    )

    write_csv(
        output / "rejected_sessions.csv",
        dropped_all,
        (*prefix, "rejection_reason", "conflicting_session_id"),
    )

    write_csv(
        output / "candidate_comparison.csv",
        audit,
        AUDIT_FIELDS,
    )

    for name, rows in (
        ("owner_frequency_comparison.csv", owner_rows),
        ("replicate_energy_comparison.csv", comparisons),
        ("replicate_profiles.csv", profiles),
        ("mean_profile_24h.csv", mean_profiles),
        ("sensitivity_summary.csv", summary),
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
        schema_version="same_day_window_deferral_sensitivity_v1",
        status="complete",
        data_origin="SYNTHETIC",
        behaviour_input_origin="SYNTHETIC",
        output_origin="SIMULATED",
        source_pilot=pilot.relative_to(root).as_posix(),
        owners_simulated=len(ids),
        owner_ids=ids,
        cohort=source["cohort"],
        energy_case=source["energy_case"],
        power_scenario=source["power_scenario"],
        study_days=days,
        warmup_days=warmup,
        replicates=replicates,
        base_seed=source["base_seed"],
        candidates_paired_including_warmup=len(audit),
        summary=summary,
        new_random_draws=0,
        completed_frequency_agreement_established=False,
        empirical_validation=False,
        national_forecast_ready=False,
        policy=[
            (
                "Replay and verify the saved control; retain every "
                "candidate owner, context, energy, power and proposed time."
            ),
            (
                "Process candidates in proposed-start order. If busy, "
                "try the previous accepted session's end time."
            ),
            (
                "The new start must remain in the same contiguous reported "
                "start-window occurrence AND original calendar day; "
                "upper endpoints are exclusive."
            ),
            (
                "A circular window crossing midnight is clipped at the "
                "original calendar day's boundary for this sensitivity."
            ),
            (
                "Start windows constrain charging starts, not session "
                "completion or plug-in dwell availability."
            ),
            (
                "Energy, power and active duration stay fixed. No alternative "
                "context, redraw, replacement or early start is introduced."
            ),
            (
                "Delayed sessions can block later candidates; record both "
                "gained and lost acceptances without optimizing the result."
            ),
            (
                "Frequency metrics count study starts; warm-up sessions "
                "can contribute carry-in energy. Rejected proposal energy "
                "is not observed unmet demand."
            ),
            (
                "Deferral assumes willingness to delay within the reported "
                "window; it is a separate behavioural sensitivity, "
                "not observed flexibility."
            ),
            (
                "Paired MCSE describes this small conditional scheduling "
                "comparison; input-fit uncertainty, stationarity and "
                "production precision remain unestablished."
            ),
        ],
        checks=dict(
            source_hashes_match=True,
            control_scheduling_and_energy_replayed=True,
            candidate_attributes_paired=True,
            starts_within_original_window_and_day=True,
            no_owner_overlaps=True,
            energy_conserved=True,
            gained_lost_accounting_checked=True,
            paired_profiles_checked=True,
            source_inputs_unchanged=True,
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

    (output / "window_deferral_sensitivity_report.json").write_text(
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

    output, report = run_sensitivity(args.pilot)

    print("Paired start-window deferral sensitivity complete.")
    print(f"owners_simulated: {report['owners_simulated']}")
    print(f"replicates: {report['replicates']}")
    print(
        "matched_candidates_including_warmup: "
        f"{report['candidates_paired_including_warmup']}"
    )

    def show(value, digits=4):
        return f"{value:.{digits}f}" if value is not None else "NA"

    for row in report["summary"]:
        print(
            f"{row['context']}: accepted/target="
            f"{show(row['control_accepted_to_target_ratio'])} -> "
            f"{show(row['deferral_accepted_to_target_ratio'])}, "
            f"rejection={show(row['control_rejection_percent'], 2)}% -> "
            f"{show(row['deferral_rejection_percent'], 2)}%, "
            f"mean_daily_kwh={row['control_mean_daily_energy_kwh']:.3f} -> "
            f"{row['deferral_mean_daily_energy_kwh']:.3f}"
        )

    total = next(
        r for r in report["summary"] if r["context"] == "TOTAL"
    )

    for field in (
        "gained_accepted_starts",
        "lost_accepted_starts",
        "delayed_accepted_starts",
    ):
        print(f"{field}: {total[field]}")

    print(
        "paired_daily_energy_difference_mcse_kwh: "
        f"{total['paired_daily_energy_mcse_kwh']:.6f}"
    )
    print(
        "Control replay, candidate pairing, "
        "start-window and energy checks passed."
    )
    print(f"Output: {output.relative_to(ROOT)}")
    print(
        "Scheduling sensitivity only; completed-frequency agreement "
        "and empirical validation remain unestablished."
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
            f"Start-window deferral sensitivity failed: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1)