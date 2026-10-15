"""Compare nested study durations using one shared candidate stream per replicate."""
from __future__ import annotations

import argparse
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
    CONTEXTS, ENERGY_FIELDS, SESSION_FIELDS, close,
    simulate, validate_package, write_csv,
)
from ev_demand.simulation.run_warmup_sensitivity import (
    check, mean_se, replay, save_json,
)

GROUPS = CONTEXTS + ("TOTAL",)


def duration_cases(
    master, master_kept, master_dropped, master_hourly, horizons, warmup
):
    cases = {}
    for days in horizons:
        kept, dropped, hourly = replay(master, days, warmup)
        for actual, original in (
            (kept, master_kept), (dropped, master_dropped)
        ):
            expected = {
                r["session_id"] for r in original
                if r["start_hour_absolute"] < 24 * days
            }
            check(
                {r["session_id"] for r in actual} == expected,
                "A shorter horizon changed shared-prefix scheduling.",
            )
        check(
            all(
                close(x, y)
                for c in CONTEXTS
                for x, y in zip(
                    hourly[c], master_hourly[c][:24 * days]
                )
            ),
            "A shorter horizon changed shared-prefix hourly energy.",
        )
        cases[days] = kept, dropped, hourly
    return cases


def run_duration_test(
    parameters, horizons, reference_days, warmup,
    replicates, base_seed, project_root=ROOT,
):
    check(
        len(horizons) >= 2
        and all(type(d) is int and 1 <= d <= 366 for d in horizons)
        and len(horizons) == len(set(horizons)),
        "Provide distinct study durations between 1 and 366 days.",
    )
    check(
        type(reference_days) is int and reference_days in horizons,
        "Reference duration must be one of the study durations.",
    )
    check(
        type(warmup) is int and 0 <= warmup <= 60,
        "Warm-up must be between 0 and 60 days.",
    )
    check(
        type(replicates) is int and 2 <= replicates <= 100,
        "Use 2 to 100 development replicates.",
    )
    check(
        type(base_seed) is int and base_seed >= 0,
        "base_seed must be nonnegative.",
    )
    horizons = sorted(horizons)
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
            "src/ev_demand/simulation/run_duration_sensitivity.py",
        )
    ]
    watched = {p: sha256(p) for p in paths}
    package, upstream = load_json(paths[0]), load_json(paths[2])
    check(
        upstream.get("schema_version") == "public_power_report_v1"
        and upstream.get("data_origin") == "SYNTHETIC",
        "Invalid power-package report.",
    )
    for p in paths[:2]:
        check(
            watched[p] == upstream["output_sha256"][p.name],
            "Parameter export changed.",
        )
    validate_package(package)
    validate_readiness(paths[1], package)

    def unchanged():
        check(
            all(sha256(p) == digest for p, digest in watched.items()),
            "An input or source changed.",
        )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    output = (
        root / "data/simulated/synthetic"
        / f"duration_sensitivity_{stamp}"
    )
    output.mkdir(parents=True, exist_ok=False)
    report = dict(
        schema_version="paired_duration_sensitivity_v1",
        data_origin="SYNTHETIC",
        status="in_progress",
        parameter_folder=folder.relative_to(root).as_posix(),
        study_days=horizons,
        reference_days=reference_days,
        warmup_days=warmup,
        base_seed=base_seed,
        replicates_requested=replicates,
        replicates_completed=0,
        owners_simulated=len(package["owner_profiles"]),
        power_scenario=package["public_power_layer"]["scenario"],
        convergence_established=False,
        stationarity_established=False,
        empirically_validated=False,
        interpretation=[
            "Generate the longest study once per replicate, with fixed warm-up; replay nested prefixes using the same candidate draws and sampled owner rates.",
            "Common-prefix scheduling and hourly energy must match; session carry-out is recomputed at each study boundary.",
            "Mean profiles use each case's own number of study days. Sampling units for standard errors are complete replicates.",
            "Prefixes share runs and days; they are dependent comparisons. Paired errors use within-replicate differences from the reference duration.",
            "This fresh master draw stream does not reproduce earlier 28-day batches, despite reuse of seed numbers.",
            "Owner rates are sampled once per replicate, so longer studies do not remove between-replicate rate uncertainty.",
            "No calendar trends, weekday effects, seasonal changes, new owners or parameter fitting are introduced.",
            "Peak outputs are peaks of ensemble mean profiles, not means of replicate peaks. No precision threshold or convergence declaration is applied.",
        ],
    )
    report_path = output / "duration_sensitivity_report.json"
    save_json(report_path, report)
    print(f"Sensitivity output: {output.relative_to(root)}", flush=True)
    metrics, profiles, children = [], [], []

    for index in range(1, replicates + 1):
        unchanged()
        seed = base_seed + index - 1
        a, b, rates, master_hourly, original = simulate(
            package, horizons[-1], seed, warmup
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
        cases = duration_cases(
            master, a, b, master_hourly, horizons, warmup
        )
        reference = cases[reference_days][2]
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

        for days, (kept, dropped, hourly) in cases.items():
            write_csv(
                child / f"accepted_days_{days}.csv",
                kept, SESSION_FIELDS + ENERGY_FIELDS,
            )
            write_csv(
                child / f"rejected_days_{days}.csv",
                dropped,
                SESSION_FIELDS
                + ("rejection_reason", "conflicting_session_id"),
            )
            hourly_rows = []
            for c in GROUPS:
                study = [
                    r for r in kept + dropped
                    if r["start_phase"] == "study"
                    and (c == "TOTAL" or r["context"] == c)
                ]
                rejected = sum(
                    r["start_phase"] == "study"
                    and (c == "TOTAL" or r["context"] == c)
                    for r in dropped
                )
                energy = math.fsum(hourly[c]) / days
                ref_energy = math.fsum(reference[c]) / reference_days
                metrics.append(dict(
                    replicate=index,
                    seed=seed,
                    study_days=days,
                    context=c,
                    study_candidates=len(study),
                    study_rejected=rejected,
                    study_accepted=len(study) - rejected,
                    mean_daily_energy_kwh=energy,
                    reference_mean_daily_energy_kwh=ref_energy,
                    daily_energy_difference_kwh=energy - ref_energy,
                ))
                for h, value in enumerate(hourly[c]):
                    hourly_rows.append(dict(
                        day_index=h // 24,
                        hour_of_day=h % 24,
                        context=c,
                        data_origin="SYNTHETIC",
                        energy_kwh=value,
                        average_power_kw=value,
                    ))
                for h in range(24):
                    mean = math.fsum(hourly[c][h::24]) / days
                    ref_mean = (
                        math.fsum(reference[c][h::24]) / reference_days
                    )
                    profiles.append(dict(
                        replicate=index,
                        seed=seed,
                        study_days=days,
                        context=c,
                        hour_of_day=h,
                        mean_power_kw=mean,
                        reference_mean_power_kw=ref_mean,
                        difference_kw=mean - ref_mean,
                    ))
            write_csv(
                child / f"hourly_days_{days}.csv",
                hourly_rows, tuple(hourly_rows[0]),
            )

        unchanged()
        child_report = dict(
            replicate=index,
            seed=seed,
            master_engine_summary=original,
            checks=dict(
                common_prefix_draws_preserved=True,
                common_prefix_scheduling_matches=True,
                common_prefix_hourly_energy_matches=True,
                energy_conserved=True,
                no_owner_overlaps=True,
            ),
            output_sha256={
                p.name: sha256(p) for p in child.glob("*.csv")
            },
        )
        save_json(child / "duration_run_report.json", child_report)
        children.append(dict(
            replicate=index,
            report_path=(
                child / "duration_run_report.json"
            ).relative_to(root).as_posix(),
            report_sha256=sha256(child / "duration_run_report.json"),
        ))
        report["replicates_completed"] = index
        save_json(report_path, report)
        print(
            f"Paired duration replicate {index}/{replicates}: checks=PASS",
            flush=True,
        )

    summary, mean_profiles = [], []
    for days in horizons:
        for c in GROUPS:
            rows = [
                r for r in metrics
                if r["study_days"] == days and r["context"] == c
            ]
            mean, se = mean_se([
                r["mean_daily_energy_kwh"] for r in rows
            ])
            delta, delta_se = mean_se([
                r["daily_energy_difference_kwh"] for r in rows
            ])
            count = sum(r["study_candidates"] for r in rows)
            case_profile = []

            for h in range(24):
                values = [
                    r for r in profiles
                    if r["study_days"] == days
                    and r["context"] == c
                    and r["hour_of_day"] == h
                ]
                value, error = mean_se([
                    r["mean_power_kw"] for r in values
                ])
                difference, paired_error = mean_se([
                    r["difference_kw"] for r in values
                ])
                entry = dict(
                    study_days=days,
                    context=c,
                    hour_of_day=h,
                    mean_power_kw=value,
                    mcse_kw=error,
                    mean_difference_kw=difference,
                    paired_difference_mcse_kw=paired_error,
                )
                case_profile.append(entry)
                mean_profiles.append(entry)

            check(
                close(
                    math.fsum(
                        r["mean_power_kw"] for r in case_profile
                    ),
                    mean,
                )
                and close(
                    math.fsum(
                        r["mean_difference_kw"] for r in case_profile
                    ),
                    delta,
                ),
                "Ensemble profile does not conserve mean daily energy.",
            )
            peak = max(r["mean_power_kw"] for r in case_profile)
            summary.append(dict(
                study_days=days,
                context=c,
                mean_daily_energy_kwh=mean,
                daily_energy_mcse_kwh=se,
                relative_daily_mcse_percent=(
                    100 * se / mean if mean > 0 else None
                ),
                mean_daily_difference_kwh=delta,
                paired_difference_mcse_kwh=delta_se,
                pooled_study_rejection_percent=(
                    100 * sum(r["study_rejected"] for r in rows) / count
                    if count else None
                ),
                peak_of_ensemble_mean_profile_kw=peak,
                ensemble_mean_peak_hours=json.dumps([
                    r["hour_of_day"] for r in case_profile
                    if close(r["mean_power_kw"], peak)
                ]),
                max_abs_ensemble_hourly_difference_kw=max(
                    abs(r["mean_difference_kw"])
                    for r in case_profile
                ),
            ))

    lookup = {
        (r["study_days"], r["context"], r["hour_of_day"]):
            r["mean_power_kw"]
        for r in mean_profiles
    }
    check(
        all(
            close(
                math.fsum(lookup[(d, c, h)] for c in CONTEXTS),
                lookup[(d, "TOTAL", h)],
            )
            for d in horizons for h in range(24)
        ),
        "Ensemble contexts do not sum to TOTAL.",
    )

    for name, rows in (
        ("paired_metrics.csv", metrics),
        ("paired_profiles.csv", profiles),
        ("duration_summary.csv", summary),
        ("mean_profile_comparison.csv", mean_profiles),
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
    parser.add_argument(
        "--study-days", nargs="+", type=int, default=[14, 28, 56],
    )
    parser.add_argument("--reference-days", type=int, default=28)
    parser.add_argument("--warmup-days", type=int, default=7)
    parser.add_argument("--replicates", type=int, default=100)
    parser.add_argument("--base-seed", type=int, default=20261007)
    args = parser.parse_args()
    output, report = run_duration_test(
        Path(args.parameters), args.study_days, args.reference_days,
        args.warmup_days, args.replicates, args.base_seed,
    )
    print("Paired study-duration sensitivity complete.")
    print(f"owners_simulated: {report['owners_simulated']}")
    print(f"replicates_completed: {report['replicates_completed']}")
    print(f"study_days: {report['study_days']}")
    print(f"warmup_days: {report['warmup_days']}")
    print(f"reference_days: {report['reference_days']}")
    for row in report["summary"]:
        if row["context"] == "TOTAL":
            print(
                f"days={row['study_days']}: "
                f"mean_daily_kwh={row['mean_daily_energy_kwh']:.6f}, "
                f"mcse={row['daily_energy_mcse_kwh']:.6f}, "
                f"difference_vs_reference={row['mean_daily_difference_kwh']:.6f}, "
                f"paired_mcse={row['paired_difference_mcse_kwh']:.6f}, "
                f"ensemble_mean_peak_kw={row['peak_of_ensemble_mean_profile_kw']:.6f}, "
                f"peak_hours={row['ensemble_mean_peak_hours']}"
            )
    print("All shared-prefix, scheduling and energy checks passed.")
    print(f"Output: {output.relative_to(ROOT)}")
    print(
        "Study-duration sensitivity only; "
        "no convergence or empirical validation established."
    )


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError, OverflowError) as exc:
        print(f"Study-duration sensitivity failed: {exc}", file=sys.stderr)
        raise SystemExit(1)