"""Repeated synthetic engine tests; each replicate retains a full run and audit."""
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
from ev_demand.parameters.build_public_power_layer import load_json, sha256
from ev_demand.simulation.run_session_simulator import CONTEXTS, close, run_files, write_csv
from ev_demand.simulation.audit_session_run import audit_run, read_csv

GROUPS = CONTEXTS + ("TOTAL",)


def quantile(values, probability):
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    low = math.floor(position)
    high = math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def describe(values):
    values = [value for value in values if value is not None]
    if not values:
        return dict(n=0, mean=None, sample_std=None, p05=None, p50=None, p95=None)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("Nonfinite value in repeated-run statistics.")
    return dict(
        n=len(values), mean=statistics.mean(values),
        sample_std=statistics.stdev(values) if len(values) > 1 else None,
        p05=quantile(values, 0.05), p50=quantile(values, 0.50),
        p95=quantile(values, 0.95),
    )


def snapshot(paths):
    return {name: sha256(path) for name, path in paths.items()}


def save_json(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def run_batch(parameters, days, replicates, base_seed, project_root=ROOT):
    if type(days) is not int or not 1 <= days <= 366:
        raise ValueError("days must be between 1 and 366.")
    if type(replicates) is not int or not 2 <= replicates <= 100:
        raise ValueError("Use 2 to 100 replicates for this development batch.")
    if type(base_seed) is not int or base_seed < 0:
        raise ValueError("base_seed must be a nonnegative integer.")
    root = project_root.resolve()
    folder = (root / parameters).resolve()
    if not folder.is_relative_to((root / "data/processed/synthetic").resolve()):
        raise ValueError("Parameters must be under data/processed/synthetic.")
    input_paths = {name: folder / name for name in (
        "simulator_parameters.json", "owner_readiness.csv", "public_power_report.json",
    )}
    source_paths = {name: root / name for name in (
        "src/ev_demand/parameters/build_public_power_layer.py",
        "src/ev_demand/simulation/run_session_simulator.py",
        "src/ev_demand/simulation/audit_session_run.py",
        "src/ev_demand/simulation/run_repeated_session_test.py",
    )}
    input_hashes, source_hashes = snapshot(input_paths), snapshot(source_paths)
    package = load_json(input_paths["simulator_parameters.json"])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    output = root / "data/simulated/synthetic" / f"repeated_sessions_{stamp}"
    output.mkdir(parents=True, exist_ok=False)
    report = {
        "schema_version": "repeated_session_engine_test_v1", "data_origin": "SYNTHETIC",
        "status": "in_progress", "replicates_requested": replicates,
        "parameter_folder": folder.relative_to(root).as_posix(), "study_days": days,
        "base_seed": base_seed, "seed_policy": "base_seed + zero-based replicate index",
        "power_scenario": package["public_power_layer"]["scenario"],
        "owners_simulated": len(package["owner_profiles"]),
        "input_sha256": input_hashes, "source_sha256": source_hashes,
        "empirically_validated": False, "national_forecast_ready": False,
        "convergence_established": False,
        "interpretation": [
            "Each run resamples owner rates, counts, contexts, starts, energy and power under the current engine.",
            "The ready owner pool and power scenario are fixed across replicates.",
            "P05/P50/P95 describe simulated replicate variation; they are not empirical confidence intervals.",
            f"Profile quantiles are pointwise across replicate {days}-day mean profiles, not daily extremes.",
            "Context quantiles cannot be added to obtain TOTAL quantiles; TOTAL is computed within each replicate.",
            "Overlap rejections change accepted frequency and energy; repeated runs do not correct this model choice.",
            "No fleet scaling, calibration, station queues, SOC/trip evolution or grid losses are introduced.",
            "A development batch does not establish Monte Carlo convergence or validate real-world demand.",
        ],
    }
    report_path = output / "repeated_test_report.json"
    save_json(report_path, report)
    print(f"Batch output: {output.relative_to(root)}", flush=True)
    runs, diagnostics, profiles = [], [], []

    def ensure_unchanged():
        if snapshot(input_paths) != input_hashes or snapshot(source_paths) != source_hashes:
            raise ValueError("An input or source file changed during the batch; stop and rerun.")

    for index in range(replicates):
        ensure_unchanged()
        seed = base_seed + index
        run, summary = run_files(folder, days, seed, root)
        audit_folder, audit, _ = audit_run(run, root)
        ensure_unchanged()
        if not all(audit["checks"].values()):
            raise ValueError("A replicate audit did not pass.")
        counts = audit["study_counts"]
        runs.append({
            "replicate": index + 1, "seed": seed,
            "run_folder": run.relative_to(root).as_posix(),
            "audit_folder": audit_folder.relative_to(root).as_posix(),
            "simulation_report_sha256": sha256(run / "simulation_report.json"),
            "audit_report_sha256": sha256(audit_folder / "session_audit_report.json"),
            "study_candidate_starts": counts["study_candidate_starts"],
            "study_accepted_starts": counts["study_accepted_starts"],
            "study_rejected_starts": counts["study_rejected_starts"],
            "study_rejection_fraction": counts["study_rejection_fraction"],
            "study_energy_kwh": summary["hourly_energy_in_study_kwh"],
            "mean_daily_energy_kwh": audit["mean_daily_energy_kwh"],
            "mean_profile_peak_kw": audit["mean_profile_peak_kw"],
            "mean_profile_peak_hours": json.dumps(audit["mean_profile_peak_hours"]),
            "largest_hourly_power_kw": audit["largest_hourly_power_kw_in_run"],
            "warmup_days": summary["warmup_days"], "audit_passed": True,
        })
        for row in audit["context_diagnostics"] + [counts]:
            diagnostics.append({
                "replicate": index + 1, "seed": seed, "data_origin": "SYNTHETIC", **row,
                "realized_weekly_candidate_starts": row["study_candidate_starts"] * 7 / days,
                "realized_weekly_accepted_starts": row["study_accepted_starts"] * 7 / days,
            })
        for row in read_csv(run / "profile_24h.csv"):
            profiles.append({
                "replicate": index + 1, "seed": seed, "data_origin": "SYNTHETIC",
                "hour_of_day": int(row["hour_of_day"]), "context": row["context"],
                "mean_power_kw": float(row["mean_power_kw"]),
            })
        write_csv(output / "replicate_summaries.csv", runs, tuple(runs[0]))
        report["replicates_completed"] = len(runs)
        save_json(report_path, report)
        print(f"Replicate {index + 1}/{replicates}: seed={seed}, audit=PASS", flush=True)

    profile_summary = []
    for code in GROUPS:
        for clock in range(24):
            values = [row["mean_power_kw"] for row in profiles
                      if row["context"] == code and row["hour_of_day"] == clock]
            if len(values) != replicates:
                raise ValueError("Missing replicate profile entries.")
            profile_summary.append({"context": code, "hour_of_day": clock,
                                    "data_origin": "SYNTHETIC", **describe(values)})
    mean_profile = {(row["context"], row["hour_of_day"]): row["mean"] for row in profile_summary}
    mean_daily = statistics.mean(row["mean_daily_energy_kwh"] for row in runs)
    if not close(math.fsum(mean_profile[("TOTAL", clock)] for clock in range(24)), mean_daily):
        raise ValueError("Batch mean profile does not conserve mean daily energy.")
    for clock in range(24):
        if not close(math.fsum(mean_profile[(code, clock)] for code in CONTEXTS),
                     mean_profile[("TOTAL", clock)]):
            raise ValueError("Mean context profiles do not sum to TOTAL.")
    metrics, pooled = [], {}
    for code in GROUPS:
        rows = [row for row in diagnostics if row["group"] == code]
        proposed = sum(row["study_candidate_starts"] for row in rows)
        rejected = sum(row["study_rejected_starts"] for row in rows)
        pooled[code] = 100 * rejected / proposed if proposed else None
        values = {
            "mean_daily_energy_kwh": [row["study_energy_kwh_including_carry_in"] / days for row in rows],
            "study_rejection_percent": [100 * row["study_rejection_fraction"]
                                        if row["study_rejection_fraction"] is not None else None for row in rows],
            "sampled_weekly_candidate_rate": [row["sampled_weekly_candidate_rate"] for row in rows],
            "realized_weekly_accepted_starts": [row["study_accepted_starts"] * 7 / days for row in rows],
        }
        for name, numbers in values.items():
            metrics.append({"context": code, "metric": name, **describe(numbers)})
    for name in ("mean_profile_peak_kw", "largest_hourly_power_kw"):
        metrics.append({"context": "TOTAL", "metric": name,
                        **describe([row[name] for row in runs])})
    for name, rows in (
        ("context_replicates.csv", diagnostics), ("profile_replicates.csv", profiles),
        ("profile_summary.csv", profile_summary), ("metric_summary.csv", metrics),
    ):
        write_csv(output / name, rows, tuple(rows[0]))
    ensure_unchanged()
    peak = max(mean_profile[("TOTAL", clock)] for clock in range(24))
    report.update(
        status="complete", all_replicates_audited=True,
        mean_profile_energy_check_passed=True,
        mean_daily_energy_kwh=describe([row["mean_daily_energy_kwh"] for row in runs]),
        pooled_study_rejection_percent=pooled,
        peak_of_ensemble_mean_profile_kw=peak,
        ensemble_mean_profile_peak_hours=[clock for clock in range(24)
                                         if close(mean_profile[("TOTAL", clock)], peak)],
        output_sha256={path.name: sha256(path) for path in output.glob("*.csv")},
    )
    save_json(report_path, report)
    return output, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parameters", required=True)
    parser.add_argument("--days", type=int, default=28)
    parser.add_argument("--replicates", type=int, default=30)
    parser.add_argument("--base-seed", type=int, default=20261007)
    args = parser.parse_args()
    output, report = run_batch(Path(args.parameters), args.days, args.replicates, args.base_seed)
    energy = report["mean_daily_energy_kwh"]
    print("Repeated synthetic session test complete.")
    print(f"replicates_completed: {report['replicates_completed']}")
    print(f"owners_simulated: {report['owners_simulated']}")
    print(f"mean_daily_energy_kwh: {energy['mean']:.6f}")
    print(f"daily_energy_p05_p95_kwh: [{energy['p05']:.6f}, {energy['p95']:.6f}]")
    for code in GROUPS:
        value = report["pooled_study_rejection_percent"][code]
        label = f"{value:.2f}%" if value is not None else "N/A (no candidate starts)"
        print(f"{code}_pooled_study_rejection: {label}")
    print(f"peak_of_ensemble_mean_profile_kw: {report['peak_of_ensemble_mean_profile_kw']:.6f}")
    print(f"ensemble_mean_profile_peak_hours: {report['ensemble_mean_profile_peak_hours']}")
    print("All replicate audits and mean-profile energy checks passed.")
    print(f"Output: {output.relative_to(ROOT)}")
    print("Development batch only; convergence and empirical validation not established.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError, OverflowError) as exc:
        print(f"Repeated session test failed: {exc}", file=sys.stderr)
        raise SystemExit(1)