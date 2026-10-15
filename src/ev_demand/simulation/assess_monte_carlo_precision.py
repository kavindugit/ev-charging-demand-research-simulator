"""Assess mean-estimation precision in an existing synthetic power comparison."""
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
from ev_demand.parameters.build_public_power_layer import sha256
from ev_demand.simulation.audit_session_run import read_csv, require
from ev_demand.simulation.plot_session_profiles import load_plot_data, numeric
from ev_demand.simulation.run_session_simulator import CONTEXTS, close, write_csv

GROUPS = CONTEXTS + ("TOTAL",)
CASES = ("reduced", "rated", "difference")


def mean_precision(values, signed=False):
    require(len(values) >= 2 and all(math.isfinite(x) for x in values),
            "Precision needs at least two finite replicate values.")
    mean = statistics.mean(values)
    sd = statistics.stdev(values)
    se = sd / math.sqrt(len(values))
    relative = not signed and mean > 0
    return dict(n=len(values), mean=mean, sample_std=sd, mc_standard_error=se,
                cv_percent=100 * sd / mean if relative else None,
                relative_mcse_percent=100 * se / mean if relative else None)


def checkpoints(n):
    return sorted({k for k in (5, 10, 20, 30, 50, 75, 100, n) if 2 <= k <= n})


def assess_precision(comparison, project_root=ROOT):
    root = project_root.resolve()
    folder, parent, means, watched = load_plot_data(comparison, root)
    n = parent["replicates"]
    require(type(n) is int and n >= 2, "Invalid replicate count.")
    profiles = read_csv(folder / "paired_profile_differences.csv")
    metrics = read_csv(folder / "paired_metrics.csv")
    p = {(int(r["replicate"]), r["context"], int(r["hour_of_day"])): r for r in profiles}
    m = {(int(r["replicate"]), r["context"]): r for r in metrics}
    require(len(p) == len(profiles) == n * 120 and set(p) == {
        (i, c, h) for i in range(1, n + 1) for c in GROUPS for h in range(24)},
        "Missing or duplicated paired profile rows.")
    require(len(m) == len(metrics) == n * 5 and set(m) == {
        (i, c) for i in range(1, n + 1) for c in GROUPS},
        "Missing or duplicated paired energy rows.")
    mean_lookup = {(r["context"], r["hour_of_day"]): r for r in means}
    base = int(profiles[0]["seed"]) - int(profiles[0]["replicate"]) + 1
    power, energy = {}, {}
    for i in range(1, n + 1):
        for c in GROUPS:
            row = m[(i, c)]
            require(int(row["seed"]) == base + i - 1, "Unexpected energy seed.")
            a, b = (numeric(row, f"{case}_mean_daily_energy_kwh") for case in CASES[:2])
            require(a >= 0 and b >= 0 and close(numeric(row, "daily_energy_difference_kwh"), b - a),
                    "Invalid paired daily energy.")
            for case, value in zip(CASES, (a, b, b - a)):
                energy[(i, c, case)] = value
            for h in range(24):
                row = p[(i, c, h)]
                require(int(row["seed"]) == base + i - 1, "Unexpected profile seed.")
                a, b = (numeric(row, f"{case}_mean_power_kw") for case in CASES[:2])
                require(a >= 0 and b >= 0 and close(numeric(row, "difference_kw"), b - a),
                        "Invalid paired hourly power.")
                for case, value in zip(CASES, (a, b, b - a)):
                    power[(i, c, h, case)] = value
            for case in CASES:
                require(close(math.fsum(power[(i, c, h, case)] for h in range(24)), energy[(i, c, case)]),
                        "A replicate profile does not conserve daily energy.")
        for case in CASES:
            require(close(math.fsum(energy[(i, c, case)] for c in CONTEXTS), energy[(i, "TOTAL", case)]),
                    "Context energies do not sum to TOTAL.")
            for h in range(24):
                require(close(math.fsum(power[(i, c, h, case)] for c in CONTEXTS), power[(i, "TOTAL", h, case)]),
                        "Context powers do not sum to TOTAL.")
    for c in GROUPS:
        for h in range(24):
            for case in CASES:
                field = "mean_difference_kw" if case == "difference" else f"{case}_mean_power_kw"
                require(close(statistics.mean(power[(i, c, h, case)] for i in range(1, n + 1)),
                              mean_lookup[(c, h)][field]), "Rebuilt mean differs from the comparison.")
    hourly, daily, peaks = [], [], []
    previous = {}
    for k in checkpoints(n):
        for c in GROUPS:
            for case in CASES:
                values = [energy[(i, c, case)] for i in range(1, k + 1)]
                stats = mean_precision(values, case == "difference")
                old = previous.get((c, case))
                daily.append(dict(context=c, case=case, unit="kWh/day", **stats,
                                  change_from_previous_checkpoint=stats["mean"] - old if old is not None else None))
                previous[(c, case)] = stats["mean"]
                profile = []
                for h in range(24):
                    stats = mean_precision([power[(i, c, h, case)] for i in range(1, k + 1)], case == "difference")
                    hourly.append(dict(context=c, case=case, hour_of_day=h, unit="kW", **stats))
                    profile.append(stats["mean"])
                if c == "TOTAL" and case != "difference":
                    peak = max(profile)
                    replicate_peaks = [max(power[(i, c, h, case)] for h in range(24)) for i in range(1, k + 1)]
                    peaks.append(dict(case=case, peak_of_ensemble_mean_kw=peak,
                                      ensemble_mean_peak_hours=json.dumps([h for h, v in enumerate(profile) if close(v, peak)]),
                                      **mean_precision(replicate_peaks)))
    require(all(sha256(path) == digest for path, digest in watched.items()), "An input changed during assessment.")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    output = root / "data/processed/synthetic" / f"monte_carlo_precision_{stamp}"
    output.mkdir(parents=True, exist_ok=False)
    for name, rows in (("hourly_mean_precision.csv", hourly), ("daily_energy_precision.csv", daily),
                       ("peak_diagnostics.csv", peaks)):
        write_csv(output / name, rows, tuple(rows[0]))
    final_daily = [r for r in daily if r["n"] == n and r["context"] == "TOTAL"]
    report = dict(
        schema_version="synthetic_mean_precision_v1", data_origin="SYNTHETIC",
        comparison_folder=folder.relative_to(root).as_posix(), replicates=n,
        owners_simulated=parent["owners_simulated"], study_days=parent["study_days"],
        checkpoints=checkpoints(n), total_daily_energy=final_daily,
        total_max_hourly_mcse_kw={case: max(r["mc_standard_error"] for r in hourly
            if r["n"] == n and r["context"] == "TOTAL" and r["case"] == case) for case in CASES},
        checks=dict(input_hashes_match=True, paired_rows_complete=True, replicate_energy_balances=True,
                    rebuilt_means_match=True),
        input_sha256={path.relative_to(root).as_posix(): digest for path, digest in watched.items()},
        source_sha256={"src/ev_demand/simulation/assess_monte_carlo_precision.py": sha256(Path(__file__)),
                       "src/ev_demand/simulation/plot_session_profiles.py": sha256(root / "src/ev_demand/simulation/plot_session_profiles.py")},
        output_sha256={path.name: sha256(path) for path in output.glob("*.csv")},
        convergence_established=False, empirically_validated=False,
        interpretation=[
            "MCSE = sample standard deviation / sqrt(number of replicates), assuming independent RNG streams.",
            "The sampling unit is a complete replicate; days, hours and owners are not independent replicate samples.",
            "These errors measure mean-estimation precision under fixed synthetic inputs, not population or model uncertainty.",
            "CV describes variation between replicates; it need not decrease as more replicates are added.",
            "Prefixes are nested, ordered by replicate number; mean changes are descriptive and are not independent comparisons.",
            "Signed differences use paired replicate differences; relative errors and CV are omitted for them.",
            "Hourly errors are pointwise standard errors, not simultaneous bands or confidence intervals.",
            "Peak rows estimate the mean of replicate peaks; that differs from the peak of the ensemble mean profile.",
            "No precision threshold, stopping rule, convergence declaration or new simulation is introduced.",
        ])
    (output / "precision_report.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return output, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--comparison", required=True)
    args = parser.parse_args()
    output, report = assess_precision(Path(args.comparison))
    print("Monte Carlo mean-precision assessment complete.")
    print(f"replicates: {report['replicates']}")
    print(f"checkpoints: {report['checkpoints']}")
    for row in report["total_daily_energy"]:
        relative = row["relative_mcse_percent"]
        suffix = f", relative_mcse={relative:.3f}%" if relative is not None else ""
        print(f"{row['case']}: daily_mean={row['mean']:.6f} kWh/day, mcse={row['mc_standard_error']:.6f} kWh/day{suffix}")
    for case, value in report["total_max_hourly_mcse_kw"].items():
        print(f"{case}_maximum_hourly_mcse_kw: {value:.6f}")
    print("All input-integrity, pairing and profile-energy checks passed.")
    print(f"Output: {output.relative_to(ROOT)}")
    print("Precision diagnostics only; no convergence or empirical validation established.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError, OverflowError) as exc:
        print(f"Precision assessment failed: {exc}", file=sys.stderr)
        raise SystemExit(1)