"""Verify and compare paired reduced-power and rated-power synthetic batches."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
from ev_demand.parameters.build_public_power_layer import load_json, sha256
from ev_demand.simulation.audit_session_run import parse_sessions, read_csv, require
from ev_demand.simulation.diagnose_session_rejections import checked_path
from ev_demand.simulation.run_repeated_session_test import describe
from ev_demand.simulation.run_session_simulator import CONTEXTS, SESSION_FIELDS, close, validate_package, write_csv

GROUPS = CONTEXTS + ("TOTAL",)
POWER_DEPENDENT = {
    "end_hour_absolute", "active_duration_hours", "effective_average_power_kw",
    "sampled_average_to_rated_fraction", "uncapped_effective_average_power_kw", "scenario",
}


def owner_inputs(package):
    owners = deepcopy(package["owner_profiles"])
    for owner in owners:
        for context in owner["active_contexts"]:
            if context["context"] != "T2":
                context.pop("power_model")
    return owners


def candidate_map(kept, dropped):
    rows = kept + dropped
    lookup = {row["session_id"]: row for row in rows}
    require(len(lookup) == len(rows), "Duplicate candidate IDs.")
    return lookup


def validate_candidate_pairs(a_kept, a_dropped, b_kept, b_dropped, a_warmup, b_warmup):
    require(a_warmup == b_warmup, "Warm-up lengths differ; same seeds do not establish candidate pairing.")
    a, b = candidate_map(a_kept, a_dropped), candidate_map(b_kept, b_dropped)
    require(a.keys() == b.keys(), "Candidate IDs differ between power scenarios.")
    for session_id, first in a.items():
        second = b[session_id]
        fields = SESSION_FIELDS if first["context"] == "T2" else [key for key in SESSION_FIELDS if key not in POWER_DEPENDENT]
        require(all(first[key] == second[key] for key in fields),
                f"Candidate draws do not match: {session_id}.")
        if first["context"] != "T2":
            require(second["effective_average_power_kw"] >= first["effective_average_power_kw"] - 1e-9,
                    "Rated scenario has lower effective power for a matched non-home candidate.")
            require(close(second["effective_average_power_kw"], float(second["rated_output_kw"]))
                    and close(float(second["sampled_average_to_rated_fraction"]), 1),
                    "Rated scenario is not using full listed output.")
    return a, b


def compare_batches(reduced, rated, project_root=ROOT):
    root = project_root.resolve()
    watched = {}

    def verify(path, digest):
        require(sha256(path) == digest, f"Saved file changed: {path.name}.")
        require(path not in watched or watched[path] == digest, "Inconsistent saved hash references.")
        watched[path] = digest

    def load_batch(reference, scenario):
        folder = checked_path(root, reference, "data/simulated/synthetic")
        path = folder / "repeated_test_report.json"
        verify(path, sha256(path))
        report = load_json(path)
        require(report.get("schema_version") == "repeated_session_engine_test_v1"
                and report.get("data_origin") == "SYNTHETIC" and report.get("status") == "complete"
                and report.get("all_replicates_audited") is True
                and report.get("power_scenario") == scenario, "Expected a completed audited batch for " + scenario)
        for name, digest in report["output_sha256"].items():
            require(Path(name).name == name, "Invalid batch filename.")
            verify(folder / name, digest)
        parameters = checked_path(root, report["parameter_folder"], "data/processed/synthetic")
        for name, digest in report["input_sha256"].items():
            require(Path(name).name == name, "Invalid parameter filename.")
            verify(parameters / name, digest)
        for name, digest in report["source_sha256"].items():
            verify(checked_path(root, name, "src/ev_demand"), digest)
        package = load_json(parameters / "simulator_parameters.json")
        validate_package(package)
        entries = read_csv(folder / "replicate_summaries.csv")
        require(report["replicates_completed"] == report["replicates_requested"] == len(entries),
                "Batch replicate counts disagree.")
        return folder, parameters, report, package, entries

    def load_run(entry, batch_report, package, index):
        require(int(entry["replicate"]) == index
                and int(entry["seed"]) == batch_report["base_seed"] + index - 1, "Unexpected replicate index or seed.")
        folder = checked_path(root, entry["run_folder"], "data/simulated/synthetic")
        audit_folder = checked_path(root, entry["audit_folder"], "data/processed/synthetic")
        verify(folder / "simulation_report.json", entry["simulation_report_sha256"])
        verify(audit_folder / "session_audit_report.json", entry["audit_report_sha256"])
        report = load_json(folder / "simulation_report.json")
        audit = load_json(audit_folder / "session_audit_report.json")
        require(report["input_sha256"] == batch_report["input_sha256"]
                and report["power_scenario"] == batch_report["power_scenario"]
                and report["summary"]["seed"] == int(entry["seed"])
                and report["summary"]["study_days"] == batch_report["study_days"], "Run settings or inputs differ from its batch.")
        require(audit["run_folder"] == entry["run_folder"] and all(audit["checks"].values())
                and audit["input_sha256"]["simulation_report.json"] == entry["simulation_report_sha256"],
                "Saved audit does not cover this run.")
        for name, digest in report["output_sha256"].items():
            require(Path(name).name == name and audit["input_sha256"][name] == digest, "Invalid run export/audit reference.")
            verify(folder / name, digest)
        owners = {owner["owner_id"]: owner for owner in package["owner_profiles"]}
        warmup, days = report["summary"]["warmup_days"], report["summary"]["study_days"]
        kept = parse_sessions(read_csv(folder / "sessions.csv"), True, owners, warmup, days * 24)
        dropped = parse_sessions(read_csv(folder / "rejected_sessions.csv"), False, owners, warmup, days * 24)
        profile_rows = read_csv(folder / "profile_24h.csv")
        profile = {(row["context"], int(row["hour_of_day"])): float(row["mean_power_kw"]) for row in profile_rows}
        require(len(profile) == len(profile_rows) == 120
                and set(profile) == {(code, hour) for code in GROUPS for hour in range(24)}, "Profile entries are incomplete or duplicated.")
        return folder, report, kept, dropped, profile

    a_folder, a_parameters, a_report, a_package, a_entries = load_batch(reduced, "reduced_test")
    b_folder, b_parameters, b_report, b_package, b_entries = load_batch(rated, "rated_ceiling_test")
    verify(root / "src/ev_demand/simulation/compare_public_power_runs.py", sha256(Path(__file__)))
    diagnostic_source = root / "src/ev_demand/simulation/diagnose_session_rejections.py"
    verify(diagnostic_source, sha256(diagnostic_source))
    for key in ("study_days", "base_seed", "replicates_completed", "owners_simulated", "source_sha256"):
        require(a_report[key] == b_report[key], "Batches have different " + key)
    require(owner_inputs(a_package) == owner_inputs(b_package), "Underlying owner parameters differ.")
    require(a_package["public_power_layer"]["input_sha256"] == b_package["public_power_layer"]["input_sha256"]
            and a_package["public_power_layer"]["config_snapshot"] == b_package["public_power_layer"]["config_snapshot"],
            "Power scenarios do not share the same upstream inputs and rating configuration.")
    require((a_parameters / "owner_readiness.csv").read_bytes() == (b_parameters / "owner_readiness.csv").read_bytes(),
            "Owner readiness differs.")
    days, n = a_report["study_days"], len(a_entries)
    require(n == len(b_entries) and n >= 2 and days > 0, "Invalid paired batch size.")
    metrics, profiles = [], []
    matched = 0
    for index, (a_entry, b_entry) in enumerate(zip(a_entries, b_entries), 1):
        a_run, a_saved, a_kept, a_dropped, a_profile = load_run(a_entry, a_report, a_package, index)
        b_run, b_saved, b_kept, b_dropped, b_profile = load_run(b_entry, b_report, b_package, index)
        require(a_saved["policy"] == b_saved["policy"], "Scheduling or other simulator policies differ.")
        require((a_run / "owner_rates.csv").read_bytes() == (b_run / "owner_rates.csv").read_bytes(),
                "Sampled owner/context rates differ; this is not a candidate-paired test.")
        a_candidates, b_candidates = validate_candidate_pairs(a_kept, a_dropped, b_kept, b_dropped,
                                                              a_saved["summary"]["warmup_days"], b_saved["summary"]["warmup_days"])
        matched += len(a_candidates)
        for code in GROUPS:
            selected = lambda rows: rows if code == "TOTAL" else [row for row in rows if row["context"] == code]
            study = [row for row in selected(list(a_candidates.values())) if row["start_phase"] == "study"]
            a_ids = {row["session_id"] for row in selected(a_kept) if row["start_phase"] == "study"}
            b_ids = {row["session_id"] for row in selected(b_kept) if row["start_phase"] == "study"}
            count = len(study)
            a_energy = math.fsum(row["energy_in_study_kwh"] for row in selected(a_kept)) / days
            b_energy = math.fsum(row["energy_in_study_kwh"] for row in selected(b_kept)) / days
            a_reject = 100 * (count - len(a_ids)) / count if count else None
            b_reject = 100 * (count - len(b_ids)) / count if count else None
            metrics.append(dict(
                replicate=index, seed=int(a_entry["seed"]), context=code, study_candidate_starts=count,
                reduced_accepted_starts=len(a_ids), rated_accepted_starts=len(b_ids),
                gained_accepted_starts=len(b_ids - a_ids), lost_accepted_starts=len(a_ids - b_ids),
                reduced_rejection_percent=a_reject, rated_rejection_percent=b_reject,
                rejection_difference_percentage_points=b_reject - a_reject if count else None,
                reduced_mean_daily_energy_kwh=a_energy, rated_mean_daily_energy_kwh=b_energy,
                daily_energy_difference_kwh=b_energy - a_energy,
            ))
            for hour in range(24):
                a, b = a_profile[(code, hour)], b_profile[(code, hour)]
                profiles.append(dict(replicate=index, seed=int(a_entry["seed"]), context=code, hour_of_day=hour,
                                     reduced_mean_power_kw=a, rated_mean_power_kw=b, difference_kw=b - a))
        print(f"Verified power pair {index}/{n}: candidate draws=MATCH", flush=True)
    summary, mean_profiles = [], []
    for code in GROUPS:
        rows = [row for row in metrics if row["context"] == code]
        count = sum(row["study_candidate_starts"] for row in rows)
        a_accepted = sum(row["reduced_accepted_starts"] for row in rows)
        b_accepted = sum(row["rated_accepted_starts"] for row in rows)
        a_energy = statistics.mean(row["reduced_mean_daily_energy_kwh"] for row in rows)
        b_energy = statistics.mean(row["rated_mean_daily_energy_kwh"] for row in rows)
        delta = describe([row["daily_energy_difference_kwh"] for row in rows])
        summary.append(dict(
            context=code, pooled_study_candidate_starts=count,
            reduced_rejection_percent=100 * (count - a_accepted) / count if count else None,
            rated_rejection_percent=100 * (count - b_accepted) / count if count else None,
            reduced_mean_daily_energy_kwh=a_energy, rated_mean_daily_energy_kwh=b_energy,
            mean_daily_energy_difference_kwh=b_energy - a_energy,
            daily_energy_change_percent=100 * (b_energy / a_energy - 1) if a_energy else None,
            paired_daily_energy_difference_p05_kwh=delta["p05"], paired_daily_energy_difference_p95_kwh=delta["p95"],
            gained_accepted_starts=sum(row["gained_accepted_starts"] for row in rows),
            lost_accepted_starts=sum(row["lost_accepted_starts"] for row in rows),
        ))
        for hour in range(24):
            values = [row for row in profiles if row["context"] == code and row["hour_of_day"] == hour]
            differences = describe([row["difference_kw"] for row in values])
            mean_profiles.append(dict(
                context=code, hour_of_day=hour,
                reduced_mean_power_kw=statistics.mean(row["reduced_mean_power_kw"] for row in values),
                rated_mean_power_kw=statistics.mean(row["rated_mean_power_kw"] for row in values),
                mean_difference_kw=differences["mean"], difference_p05_kw=differences["p05"], difference_p95_kw=differences["p95"],
            ))
    peaks = {}
    total_profile = [row for row in mean_profiles if row["context"] == "TOTAL"]
    for case, batch_report in (("reduced", a_report), ("rated", b_report)):
        peak = max(row[f"{case}_mean_power_kw"] for row in total_profile)
        require(close(math.fsum(row[f"{case}_mean_power_kw"] for row in total_profile), summary[-1][f"{case}_mean_daily_energy_kwh"]),
                "Mean profile does not conserve daily energy.")
        require(close(peak, batch_report["peak_of_ensemble_mean_profile_kw"])
                and close(summary[-1][f"{case}_mean_daily_energy_kwh"], batch_report["mean_daily_energy_kwh"]["mean"]),
                "Rebuilt comparison does not match its batch summary.")
        peaks[case] = dict(peak_kw=peak, peak_hours=[row["hour_of_day"] for row in total_profile
                                                  if close(row[f"{case}_mean_power_kw"], peak)])
    require(all(sha256(path) == digest for path, digest in watched.items()), "An input changed during comparison.")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    output = root / "data/processed/synthetic" / f"public_power_comparison_{stamp}"
    output.mkdir(parents=True, exist_ok=False)
    for name, rows in (("paired_metrics.csv", metrics), ("paired_profile_differences.csv", profiles),
                       ("context_comparison_summary.csv", summary), ("mean_profile_comparison.csv", mean_profiles)):
        write_csv(output / name, rows, tuple(rows[0]))
    result = {
        "schema_version": "paired_public_power_comparison_v1", "data_origin": "SYNTHETIC",
        "reduced_batch": a_folder.relative_to(root).as_posix(), "rated_batch": b_folder.relative_to(root).as_posix(),
        "replicates": n, "study_days": days, "owners_simulated": a_report["owners_simulated"],
        "matched_candidates_including_warmup": matched,
        "checks": dict(candidate_draws_match=True, home_power_draws_match=True,
                       sampled_owner_rates_match=True, source_hashes_match=True, mean_profile_energy_balances=True),
        "context_comparison": summary, "ensemble_mean_peaks": peaks,
        "input_sha256": {path.relative_to(root).as_posix(): digest for path, digest in watched.items()},
        "output_sha256": {path.name: sha256(path) for path in output.glob("*.csv")},
        "interpretation": [
            "These paired development scenarios differ in configured non-home output, using matched candidates and the baseline scheduling rule.",
            "Home power draws are unchanged; home acceptance can change through non-home occupancy of the same owner's vehicle.",
            "Higher power changes active durations, acceptance and study-boundary energy; daily energy need not remain constant.",
            "P05/P95 of paired differences describe replicate variation, not empirical confidence intervals.",
            "A rated-power scenario is not a guaranteed upper bound on each aggregate hourly load.",
            "No deferral sensitivity outputs, scaling, calibration or physical battery/SOC evolution are included.",
        ],
        "empirically_validated": False, "national_forecast_ready": False, "convergence_established": False,
    }
    (output / "public_power_comparison_report.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return output, result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reduced", required=True)
    parser.add_argument("--rated", required=True)
    args = parser.parse_args()
    output, report = compare_batches(Path(args.reduced), Path(args.rated))
    print("Paired public charging power comparison complete.")
    print(f"matched_candidates_including_warmup: {report['matched_candidates_including_warmup']}")
    for row in report["context_comparison"]:
        a, b = row["reduced_rejection_percent"], row["rated_rejection_percent"]
        rejection = f"{a:.2f}% -> {b:.2f}%" if a is not None else "N/A"
        print(f"{row['context']}: mean_daily_kwh={row['reduced_mean_daily_energy_kwh']:.3f} -> {row['rated_mean_daily_energy_kwh']:.3f}, rejection={rejection}")
    total = report["context_comparison"][-1]
    print(f"mean_daily_energy_difference_kwh: {total['mean_daily_energy_difference_kwh']:.6f}")
    change = total["daily_energy_change_percent"]
    print(f"daily_energy_change_percent: {change:.3f}" if change is not None else "daily_energy_change_percent: N/A")
    for case, peak in report["ensemble_mean_peaks"].items():
        print(f"{case}_ensemble_mean_peak: {peak['peak_kw']:.6f} kW, hours={peak['peak_hours']}")
    print("All pairing, input-integrity and mean-profile energy checks passed.")
    print(f"Output: {output.relative_to(ROOT)}")
    print("Engineering sensitivity only; no convergence or empirical validation established.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError, OverflowError) as exc:
        print(f"Public power comparison failed: {exc}", file=sys.stderr)
        raise SystemExit(1)