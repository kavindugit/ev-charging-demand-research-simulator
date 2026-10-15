"""Replay shared synthetic candidates with different empty-start warm-ups."""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
from ev_demand.parameters.build_public_power_layer import (
    load_json, sha256, validate_readiness,
)
from ev_demand.simulation.run_session_simulator import (
    CONTEXTS, ENERGY_FIELDS, SESSION_FIELDS, allocate_energy, close,
    schedule_owner, simulate, validate_package, write_csv,
)

GROUPS = CONTEXTS + ("TOTAL",)


def check(condition, message):
    if not condition:
        raise ValueError(message)


def save_json(path, value):
    path.write_text(
        json.dumps(value, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def mean_se(values):
    return (
        statistics.mean(values),
        statistics.stdev(values) / math.sqrt(len(values)),
    )


def replay(candidates, days, warmup):
    source = {r["session_id"]: r for r in candidates}
    check(len(source) == len(candidates), "Duplicate master candidate IDs.")
    by_owner = defaultdict(list)
    for row in candidates:
        if -24 * warmup <= row["start_hour_absolute"] < 24 * days:
            by_owner[row["owner_id"]].append(
                {k: row[k] for k in SESSION_FIELDS}
            )

    kept, dropped = [], []
    for owner in sorted(by_owner):
        a, b = schedule_owner(by_owner[owner])
        kept.extend(a)
        dropped.extend(b)

    eligible = {
        r["session_id"] for rows in by_owner.values() for r in rows
    }
    check(
        len(kept) + len(dropped) == len(eligible)
        and {r["session_id"] for r in kept + dropped} == eligible,
        "Candidate partition changed.",
    )
    for row in kept + dropped:
        check(
            all(
                row[k] == source[row["session_id"]][k]
                for k in SESSION_FIELDS
            ),
            "A candidate draw changed during warm-up replay.",
        )

    hourly = {c: [0.0] * (24 * days) for c in CONTEXTS}
    previous = {}
    for row in sorted(
        kept, key=lambda r: (r["owner_id"], r["start_hour_absolute"])
    ):
        start = row["start_hour_absolute"]
        end = row["end_hour_absolute"]
        check(
            start >= previous.get(row["owner_id"], -math.inf),
            "Owner sessions overlap.",
        )
        check(
            close(end - start, row["active_duration_hours"])
            and close(
                row["active_duration_hours"]
                * row["effective_average_power_kw"],
                row["session_energy_kwh"],
            ),
            "Session duration, power and energy disagree.",
        )
        previous[row["owner_id"]] = end
        row.update(allocate_energy(row, 24 * days, hourly))

    hourly["TOTAL"] = [
        math.fsum(hourly[c][h] for c in CONTEXTS)
        for h in range(24 * days)
    ]
    inside = math.fsum(hourly["TOTAL"])
    check(
        close(
            inside,
            math.fsum(r["energy_in_study_kwh"] for r in kept),
        ),
        "Hourly energy imbalance.",
    )
    check(
        close(
            math.fsum(r["session_energy_kwh"] for r in kept),
            math.fsum(r[k] for r in kept for k in ENERGY_FIELDS),
        ),
        "Boundary energy imbalance.",
    )
    return kept, dropped, hourly


def run_test(
    parameters, days, replicates, base_seed, warmups, project_root=ROOT
):
    check(
        type(days) is int and 1 <= days <= 366,
        "days must be between 1 and 366.",
    )
    check(
        type(replicates) is int and 2 <= replicates <= 100,
        "Use 2 to 100 development replicates.",
    )
    check(
        type(base_seed) is int and base_seed >= 0,
        "base_seed must be nonnegative.",
    )
    check(
        len(warmups) >= 2
        and all(type(w) is int and 0 <= w <= 60 for w in warmups)
        and len(set(warmups)) == len(warmups),
        "Provide distinct warm-ups between 0 and 60 days.",
    )
    warmups = sorted(warmups)
    root = project_root.resolve()
    folder = (root / parameters).resolve()
    check(
        folder.is_relative_to(root / "data/processed/synthetic"),
        "Expected synthetic parameter folder.",
    )
    paths = [
        folder / name for name in (
            "simulator_parameters.json",
            "owner_readiness.csv",
            "public_power_report.json",
        )
    ]
    paths += [
        root / name for name in (
            "src/ev_demand/parameters/build_public_power_layer.py",
            "src/ev_demand/simulation/run_session_simulator.py",
            "src/ev_demand/simulation/run_warmup_sensitivity.py",
        )
    ]
    watched = {p: sha256(p) for p in paths}
    package, upstream = load_json(paths[0]), load_json(paths[2])
    check(
        upstream.get("schema_version") == "public_power_report_v1"
        and upstream.get("data_origin") == "SYNTHETIC",
        "Invalid power-package report.",
    )
    for path in paths[:2]:
        check(
            watched[path] == upstream["output_sha256"][path.name],
            "Parameter export changed.",
        )
    longest = validate_package(package)
    validate_readiness(paths[1], package)

    def unchanged():
        check(
            all(sha256(p) == digest for p, digest in watched.items()),
            "An input or source changed.",
        )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    output = (
        root / "data/simulated/synthetic"
        / f"warmup_sensitivity_{stamp}"
    )
    output.mkdir(parents=True, exist_ok=False)
    report = dict(
        schema_version="paired_warmup_sensitivity_v1",
        data_origin="SYNTHETIC",
        status="in_progress",
        parameter_folder=folder.relative_to(root).as_posix(),
        study_days=days,
        base_seed=base_seed,
        warmup_days=warmups,
        reference_warmup_days=warmups[0],
        engine_default_warmup_days=math.ceil(longest / 24) + 1,
        master_warmup_days=warmups[-1],
        power_scenario=package["public_power_layer"]["scenario"],
        owners_simulated=len(package["owner_profiles"]),
        replicates_requested=replicates,
        replicates_completed=0,
        convergence_established=False,
        stationarity_established=False,
        empirically_validated=False,
        interpretation=[
            "Generate candidates once per replicate over the longest warm-up and fixed study horizon; replay eligible subsets from empty states.",
            "Study candidates, owner rates, energy, power and timing draws match across warm-up cases within this experiment.",
            "The shortest tested warm-up is the comparison reference. The longest remains a finite-warm-up case, not ground truth.",
            "This new master draw stream does not reproduce the earlier repeated-session batches, despite reuse of seed numbers.",
            "Longer history can change accepted sessions in either direction; no monotonic energy change is assumed.",
            "Study energy includes warm-up carry-in and excludes study-session carry-out, following the existing engine.",
            "This test changes initialization only. It does not establish stationarity or validate the behavioural model.",
        ],
    )
    report_path = output / "warmup_sensitivity_report.json"
    save_json(report_path, report)
    print(f"Sensitivity output: {output.relative_to(root)}", flush=True)
    metrics, profiles, daily, children = [], [], [], []

    for index in range(1, replicates + 1):
        unchanged()
        seed = base_seed + index - 1
        a, b, rates, master_hourly, original = simulate(
            package, days, seed, warmups[-1]
        )
        check(
            original["no_owner_overlaps"] and original["energy_conserved"],
            "Master engine checks failed.",
        )
        master = [
            {k: r[k] for k in SESSION_FIELDS} for r in a + b
        ]
        check(
            len(master) == original["candidate_sessions"],
            "Master candidate count differs.",
        )
        child = output / f"replicate_{index:03d}"
        child.mkdir()
        write_csv(child / "master_candidates.csv", master, SESSION_FIELDS)
        write_csv(
            child / "owner_rates.csv", rates,
            (
                "owner_id", "data_origin", "context", "active",
                "weekly_total_sampled", "weekly_context_rate",
            ),
        )

        reference = None
        for w in warmups:
            kept, dropped, hourly = replay(master, days, w)
            study_ids = {
                r["session_id"] for r in kept + dropped
                if r["start_phase"] == "study"
            }
            check(
                study_ids == {
                    r["session_id"] for r in master
                    if r["start_phase"] == "study"
                },
                "Study candidate identities differ across warm-ups.",
            )
            if w == warmups[-1]:
                check(
                    {r["session_id"] for r in kept}
                    == {r["session_id"] for r in a},
                    "Longest replay differs from master scheduling.",
                )
                check(
                    all(
                        close(x, y)
                        for c in CONTEXTS
                        for x, y in zip(hourly[c], master_hourly[c])
                    ),
                    "Longest replay differs from master hourly energy.",
                )

            accepted = {
                c: {
                    r["session_id"] for r in kept
                    if r["start_phase"] == "study"
                    and (c == "TOTAL" or r["context"] == c)
                }
                for c in GROUPS
            }
            if reference is None:
                reference = (hourly, accepted)
            ref_hourly, ref_accepted = reference

            write_csv(
                child / f"accepted_warmup_{w}.csv",
                kept, SESSION_FIELDS + ENERGY_FIELDS,
            )
            write_csv(
                child / f"rejected_warmup_{w}.csv",
                dropped,
                SESSION_FIELDS
                + ("rejection_reason", "conflicting_session_id"),
            )

            hourly_rows = []
            for c in GROUPS:
                count = sum(
                    r["start_phase"] == "study"
                    and (c == "TOTAL" or r["context"] == c)
                    for r in master
                )
                values = hourly[c]
                energy = math.fsum(values) / days
                ref_energy = math.fsum(ref_hourly[c]) / days
                rejected = count - len(accepted[c])
                metrics.append(dict(
                    replicate=index,
                    seed=seed,
                    warmup_days=w,
                    context=c,
                    study_candidates=count,
                    study_accepted=len(accepted[c]),
                    study_rejected=rejected,
                    mean_daily_energy_kwh=energy,
                    reference_mean_daily_energy_kwh=ref_energy,
                    daily_energy_difference_kwh=energy - ref_energy,
                    changed_study_acceptance=len(
                        accepted[c] ^ ref_accepted[c]
                    ),
                    maximum_absolute_hourly_energy_difference_kwh=max(
                        abs(x - y)
                        for x, y in zip(values, ref_hourly[c])
                    ),
                ))
                for h, value in enumerate(values):
                    hourly_rows.append(dict(
                        day_index=h // 24,
                        hour_of_day=h % 24,
                        context=c,
                        data_origin="SYNTHETIC",
                        energy_kwh=value,
                        average_power_kw=value,
                    ))
                for h in range(24):
                    mean = math.fsum(values[h::24]) / days
                    ref_mean = math.fsum(ref_hourly[c][h::24]) / days
                    profiles.append(dict(
                        replicate=index,
                        seed=seed,
                        warmup_days=w,
                        context=c,
                        hour_of_day=h,
                        mean_power_kw=mean,
                        reference_mean_power_kw=ref_mean,
                        difference_kw=mean - ref_mean,
                    ))
                for day in range(days):
                    energy_day = math.fsum(
                        values[day * 24:(day + 1) * 24]
                    )
                    ref_day = math.fsum(
                        ref_hourly[c][day * 24:(day + 1) * 24]
                    )
                    daily.append(dict(
                        replicate=index,
                        seed=seed,
                        warmup_days=w,
                        context=c,
                        day_index=day,
                        energy_kwh=energy_day,
                        reference_energy_kwh=ref_day,
                        difference_kwh=energy_day - ref_day,
                    ))
            write_csv(
                child / f"hourly_warmup_{w}.csv",
                hourly_rows, tuple(hourly_rows[0]),
            )

        unchanged()
        child_report = dict(
            replicate=index,
            seed=seed,
            master_engine_summary=original,
            checks=dict(
                candidate_draws_preserved=True,
                no_owner_overlaps=True,
                energy_conserved=True,
                longest_case_matches_master=True,
            ),
            output_sha256={
                p.name: sha256(p) for p in child.glob("*.csv")
            },
        )
        save_json(child / "warmup_run_report.json", child_report)
        children.append(dict(
            replicate=index,
            report_path=(
                child / "warmup_run_report.json"
            ).relative_to(root).as_posix(),
            report_sha256=sha256(child / "warmup_run_report.json"),
        ))
        report["replicates_completed"] = index
        save_json(report_path, report)
        print(
            f"Paired warm-up replicate {index}/{replicates}: checks=PASS",
            flush=True,
        )

    summary = []
    for w in warmups:
        for c in GROUPS:
            rows = [
                r for r in metrics
                if r["warmup_days"] == w and r["context"] == c
            ]
            mean, se = mean_se([
                r["daily_energy_difference_kwh"] for r in rows
            ])
            count = sum(r["study_candidates"] for r in rows)
            summary.append(dict(
                warmup_days=w,
                context=c,
                mean_daily_energy_kwh=statistics.mean(
                    r["mean_daily_energy_kwh"] for r in rows
                ),
                mean_daily_difference_kwh=mean,
                paired_difference_mcse_kwh=se,
                pooled_study_rejection_percent=(
                    100 * sum(r["study_rejected"] for r in rows) / count
                    if count else None
                ),
                changed_study_acceptance=sum(
                    r["changed_study_acceptance"] for r in rows
                ),
                maximum_absolute_hourly_energy_difference_kwh=max(
                    r["maximum_absolute_hourly_energy_difference_kwh"]
                    for r in rows
                ),
            ))

    for name, rows in (
        ("paired_metrics.csv", metrics),
        ("paired_profile_differences.csv", profiles),
        ("daily_energy_differences.csv", daily),
        ("warmup_summary.csv", summary),
    ):
        write_csv(output / name, rows, tuple(rows[0]))
    (output / "owner_readiness.csv").write_bytes(paths[1].read_bytes())
    unchanged()
    report.update(
        status="complete",
        all_paired_checks_passed=True,
        summary=summary,
        child_reports=children,
        input_sha256={
            p.relative_to(root).as_posix(): digest
            for p, digest in watched.items()
        },
        output_sha256={
            p.name: sha256(p) for p in output.glob("*.csv")
        },
    )
    save_json(report_path, report)
    return output, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parameters", required=True)
    parser.add_argument("--days", type=int, default=28)
    parser.add_argument("--replicates", type=int, default=100)
    parser.add_argument("--base-seed", type=int, default=20261007)
    parser.add_argument(
        "--warmup-days", nargs="+", type=int, default=[4, 7, 14],
    )
    args = parser.parse_args()
    output, report = run_test(
        Path(args.parameters), args.days, args.replicates,
        args.base_seed, args.warmup_days,
    )
    print("Paired warm-up sensitivity complete.")
    print(f"owners_simulated: {report['owners_simulated']}")
    print(f"replicates_completed: {report['replicates_completed']}")
    print(f"warmup_days: {report['warmup_days']}")
    for row in report["summary"]:
        if row["context"] == "TOTAL":
            print(
                f"warmup={row['warmup_days']}d: "
                f"mean_daily_kwh={row['mean_daily_energy_kwh']:.6f}, "
                f"difference_vs_reference={row['mean_daily_difference_kwh']:.6f}, "
                f"paired_mcse={row['paired_difference_mcse_kwh']:.6f}, "
                f"changed_study_acceptance={row['changed_study_acceptance']}"
            )
    print("All candidate-pairing, scheduling and energy checks passed.")
    print(f"Output: {output.relative_to(ROOT)}")
    print(
        "Initialization sensitivity only; "
        "no stationarity or empirical validation established."
    )


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError, OverflowError) as exc:
        print(f"Warm-up sensitivity failed: {exc}", file=sys.stderr)
        raise SystemExit(1)