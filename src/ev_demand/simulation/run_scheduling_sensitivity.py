"""Pair the saved baseline with a same-day start-deferral test assumption."""
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
from ev_demand.parameters.build_public_power_layer import load_json, sha256
from ev_demand.simulation.audit_session_run import parse_sessions, read_csv, require
from ev_demand.simulation.diagnose_session_rejections import checked_path
from ev_demand.simulation.run_session_simulator import (
    CONTEXTS, ENERGY_FIELDS, SESSION_FIELDS, allocate_energy, close,
    schedule_owner, validate_package, write_csv,
)

GROUPS = CONTEXTS + ("TOTAL",)
EXTRA_FIELDS = (
    "baseline_status", "original_start_hour_absolute", "original_end_hour_absolute",
    "delay_hours", "rejection_reason", "conflicting_session_id",
)


def save_json(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def next_start(original, available, context):
    """Earliest allowed time at or after both arrival and owner availability."""
    day = int(original["start_day_index"]) * 24
    start, end = context["window_start_hour"], context["window_end_hour"]
    intervals = [(start, end)] if start < end else [(0, end), (start, 24)]
    earliest = max(original["start_hour_absolute"], available)
    for low, high in intervals:
        value = max(earliest, day + low)
        if value < day + high:
            return value
    return None


def defer_candidates(source, contexts):
    by_owner = defaultdict(list)
    for row in source:
        by_owner[row["owner_id"]].append(row)
    accepted, rejected = [], []
    for owner_id in sorted(by_owner):
        last_end, last_id = -math.inf, ""
        rows = sorted(by_owner[owner_id], key=lambda row: (row["start_hour_absolute"], row["session_id"]))
        for original in rows:
            row = {key: original[key] for key in SESSION_FIELDS}
            row.update(
                baseline_status=original["baseline_status"],
                original_start_hour_absolute=original["start_hour_absolute"],
                original_end_hour_absolute=original["end_hour_absolute"],
                delay_hours=0.0, rejection_reason="", conflicting_session_id="",
            )
            if row["session_energy_kwh"] <= 0:
                row["rejection_reason"] = "zero_energy_draw"
                rejected.append(row)
                continue
            context = contexts[(owner_id, row["context"])]
            start = next_start(original, last_end, context)
            if start is None:
                row.update(rejection_reason="no_later_start_in_original_day_window",
                           conflicting_session_id=last_id)
                rejected.append(row)
                continue
            row["delay_hours"] = start - original["start_hour_absolute"]
            row["start_hour_absolute"] = start
            row["start_hour_of_day"] = start - int(row["start_day_index"]) * 24
            row["end_hour_absolute"] = start + row["active_duration_hours"]
            require(start >= last_end and row["delay_hours"] >= 0, "Invalid deferred timeline.")
            require((row["start_hour_of_day"] - context["window_start_hour"]) % 24
                    < context["window_length_hours"] + 1e-9, "Deferred start left its reported window.")
            require(math.floor(start / 24) == int(original["start_day_index"]), "Deferred start changed day.")
            accepted.append(row)
            last_end, last_id = row["end_hour_absolute"], row["session_id"]
    lookup = {row["session_id"]: row for row in source}
    require(len(lookup) == len(source) == len(accepted) + len(rejected), "Candidate identities changed.")
    mutable = {"start_hour_absolute", "end_hour_absolute", "start_hour_of_day"}
    for row in accepted + rejected:
        original = lookup[row["session_id"]]
        require(all(row[key] == original[key] for key in SESSION_FIELDS if key not in mutable),
                "A candidate draw changed during deferral.")
    accepted.sort(key=lambda row: (row["start_hour_absolute"], row["owner_id"]))
    rejected.sort(key=lambda row: (row["start_hour_absolute"], row["owner_id"]))
    return accepted, rejected


def account(accepted, days):
    hourly = {code: [0.0] * (days * 24) for code in CONTEXTS}
    previous = {}
    for row in sorted(accepted, key=lambda item: (item["owner_id"], item["start_hour_absolute"])):
        start, end = row["start_hour_absolute"], row["end_hour_absolute"]
        require(start >= previous.get(row["owner_id"], -math.inf), "Accepted owner sessions overlap.")
        require(close(end - start, row["active_duration_hours"])
                and close(row["active_duration_hours"] * row["effective_average_power_kw"],
                          row["session_energy_kwh"]), "Duration, power and energy disagree.")
        previous[row["owner_id"]] = end
        row.update(allocate_energy(row, days * 24, hourly))
    hourly["TOTAL"] = [math.fsum(hourly[code][h] for code in CONTEXTS) for h in range(days * 24)]
    energy = math.fsum(hourly["TOTAL"])
    full = math.fsum(row["session_energy_kwh"] for row in accepted)
    before = math.fsum(row["energy_before_study_kwh"] for row in accepted)
    after = math.fsum(row["energy_after_study_kwh"] for row in accepted)
    require(close(energy, math.fsum(row["energy_in_study_kwh"] for row in accepted))
            and close(full, before + energy + after), "Scenario energy does not balance.")
    return hourly, dict(full_accepted_energy_kwh=full, energy_before_study_kwh=before,
                        energy_in_study_kwh=energy, energy_after_study_kwh=after)


def compare_run(kept, dropped, contexts, days):
    source = [{**row, "baseline_status": status}
              for status, rows in (("accepted", kept), ("rejected", dropped)) for row in rows]
    baseline = [{**row} for row in kept]
    alternative, rejected = defer_candidates(source, contexts)
    baseline_hourly, base_energy = account(baseline, days)
    alternative_hourly, alt_energy = account(alternative, days)
    comparisons, profiles = [], []
    for code in GROUPS:
        selected = lambda rows: rows if code == "TOTAL" else [row for row in rows if row["context"] == code]
        candidates = [row for row in selected(source) if row["start_phase"] == "study"]
        base_ids = {row["session_id"] for row in selected(baseline) if row["start_phase"] == "study"}
        alt_study = [row for row in selected(alternative) if row["start_phase"] == "study"]
        alt_ids = {row["session_id"] for row in alt_study}
        count = len(candidates)
        base_daily = math.fsum(baseline_hourly[code]) / days
        alt_daily = math.fsum(alternative_hourly[code]) / days
        comparisons.append({
            "context": code, "study_candidate_starts": count,
            "baseline_accepted_starts": len(base_ids), "deferred_accepted_starts": len(alt_ids),
            "baseline_rejected_starts": count - len(base_ids), "deferred_rejected_starts": count - len(alt_ids),
            "gained_accepted_starts": len(alt_ids - base_ids), "lost_accepted_starts": len(base_ids - alt_ids),
            "delayed_accepted_starts": sum(row["delay_hours"] > 0 for row in alt_study),
            "mean_delay_hours_for_delayed_starts": statistics.mean(row["delay_hours"] for row in alt_study
                                                                      if row["delay_hours"] > 0)
                                                       if any(row["delay_hours"] > 0 for row in alt_study) else None,
            "baseline_mean_daily_energy_kwh": base_daily, "deferred_mean_daily_energy_kwh": alt_daily,
            "daily_energy_difference_kwh": alt_daily - base_daily,
        })
        require(len(alt_ids) - len(base_ids) == len(alt_ids - base_ids) - len(base_ids - alt_ids),
                "Paired acceptance changes do not balance.")
        for clock in range(24):
            a = math.fsum(baseline_hourly[code][clock::24]) / days
            b = math.fsum(alternative_hourly[code][clock::24]) / days
            profiles.append(dict(context=code, hour_of_day=clock,
                                 baseline_mean_power_kw=a, deferred_mean_power_kw=b, difference_kw=b - a))
    return alternative, rejected, alternative_hourly, comparisons, profiles, base_energy, alt_energy


def run_sensitivity(batch, project_root=ROOT):
    root = project_root.resolve()
    batch = checked_path(root, batch, "data/simulated/synthetic")
    report_path = batch / "repeated_test_report.json"
    report = load_json(report_path)
    require(report.get("schema_version") == "repeated_session_engine_test_v1"
            and report.get("data_origin") == "SYNTHETIC" and report.get("status") == "complete"
            and report.get("all_replicates_audited") is True, "Expected a completed audited baseline batch.")
    watched = {report_path: sha256(report_path)}

    def verify(path, digest):
        require(sha256(path) == digest, f"Saved input changed: {path.name}.")
        require(path not in watched or watched[path] == digest, "Inconsistent input hash references.")
        watched[path] = digest

    for name, digest in report["output_sha256"].items():
        require(Path(name).name == name, "Invalid batch filename.")
        verify(batch / name, digest)
    folder = checked_path(root, report["parameter_folder"], "data/processed/synthetic")
    for name, digest in report["input_sha256"].items():
        require(Path(name).name == name, "Invalid parameter filename.")
        verify(folder / name, digest)
    for name, digest in report["source_sha256"].items():
        verify(checked_path(root, name, "src/ev_demand"), digest)
    verify(root / "src/ev_demand/simulation/run_scheduling_sensitivity.py", sha256(Path(__file__)))
    diagnostic_source = root / "src/ev_demand/simulation/diagnose_session_rejections.py"
    verify(diagnostic_source, sha256(diagnostic_source))
    package = load_json(folder / "simulator_parameters.json")
    validate_package(package)
    owners = {row["owner_id"]: row for row in package["owner_profiles"]}
    contexts = {(owner_id, row["context"]): row for owner_id, owner in owners.items()
                for row in owner["active_contexts"]}
    runs = read_csv(batch / "replicate_summaries.csv")
    n, days = report["replicates_completed"], report["study_days"]
    require(n == report["replicates_requested"] == len(runs) and n >= 2 and days > 0,
            "Batch settings or replicate counts disagree.")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    output = root / "data/simulated/synthetic" / f"scheduling_sensitivity_{stamp}"
    output.mkdir(parents=True, exist_ok=False)
    result = {
        "schema_version": "scheduling_sensitivity_v1", "data_origin": "SYNTHETIC", "status": "in_progress",
        "baseline_batch": batch.relative_to(root).as_posix(), "replicates": n, "study_days": days,
        "owners_simulated": len(owners), "power_scenario": report["power_scenario"],
        "scheduling_assumption": "Earliest feasible later start within original calendar day and context start window; original candidate order has priority.",
        "interpretation": [
            "Engineering sensitivity only; same-day deferral is not observed owner behaviour or a validated replacement engine.",
            "All original candidate identities, contexts, energy, power, active durations, days and phases are retained; no new draws.",
            "A deferred start can block a later candidate that the baseline accepted; both gains and losses are recorded.",
            "Deferral may change profile shape and boundary energy; energy partitions are recomputed.",
            "Start windows limit starts, not completion; long sessions are neither capped nor shortened.",
            "This models owner scheduling only, without station queues, travel feasibility, battery SOC or grid losses.",
            "A lower rejection rate alone does not validate this scheduling assumption or recover surveyed charging frequency.",
        ],
        "empirically_validated": False, "national_forecast_ready": False,
    }
    save_json(output / "scheduling_sensitivity_report.json", result)
    print(f"Sensitivity output: {output.relative_to(root)}", flush=True)
    all_comparisons, all_profiles, child_reports = [], [], []
    for index, entry in enumerate(runs, 1):
        seed = int(entry["seed"])
        require(int(entry["replicate"]) == index and seed == report["base_seed"] + index - 1, "Unexpected replicate seed/order.")
        run = checked_path(root, entry["run_folder"], "data/simulated/synthetic")
        audit_folder = checked_path(root, entry["audit_folder"], "data/processed/synthetic")
        verify(run / "simulation_report.json", entry["simulation_report_sha256"])
        verify(audit_folder / "session_audit_report.json", entry["audit_report_sha256"])
        saved, audit = load_json(run / "simulation_report.json"), load_json(audit_folder / "session_audit_report.json")
        require(saved["input_sha256"] == report["input_sha256"] and saved["summary"]["seed"] == seed
                and saved["summary"]["study_days"] == days, "Baseline replicate settings disagree.")
        require(audit["run_folder"] == entry["run_folder"] and all(audit["checks"].values())
                and audit["input_sha256"]["simulation_report.json"] == entry["simulation_report_sha256"],
                "Baseline audit does not cover this run.")
        for name, digest in saved["output_sha256"].items():
            require(Path(name).name == name and audit["input_sha256"][name] == digest, "Invalid baseline export/audit reference.")
            verify(run / name, digest)
        warmup = saved["summary"]["warmup_days"]
        kept = parse_sessions(read_csv(run / "sessions.csv"), True, owners, warmup, days * 24)
        dropped = parse_sessions(read_csv(run / "rejected_sessions.csv"), False, owners, warmup, days * 24)
        by_owner = defaultdict(list)
        for row in kept + dropped:
            by_owner[row["owner_id"]].append(row)
        replay_ids = set()
        for rows in by_owner.values():
            replay, rejected_replay = schedule_owner(rows)
            replay_ids.update(row["session_id"] for row in replay)
            reasons = {row["session_id"]: (row["rejection_reason"], row["conflicting_session_id"]) for row in rejected_replay}
            require(all(reasons[row["session_id"]] == (row["rejection_reason"], row["conflicting_session_id"])
                        for row in rows if "rejection_reason" in row), "Baseline rejection replay disagrees.")
        require(replay_ids == {row["session_id"] for row in kept}, "Baseline accepted replay disagrees.")
        alt, rejected, hourly, comparison, profiles, base_energy, alt_energy = compare_run(kept, dropped, contexts, days)
        require(close(base_energy["energy_in_study_kwh"], saved["summary"]["hourly_energy_in_study_kwh"]), "Rebuilt baseline energy disagrees.")
        total = comparison[-1]
        require(total["study_candidate_starts"] == int(entry["study_candidate_starts"])
                and total["baseline_accepted_starts"] == int(entry["study_accepted_starts"]), "Baseline start counts disagree.")
        parse_sessions(alt, True, owners, warmup, days * 24)
        child = output / f"replicate_{index:04d}"
        child.mkdir()
        write_csv(child / "scheduled_sessions.csv", alt, SESSION_FIELDS + EXTRA_FIELDS + ENERGY_FIELDS)
        write_csv(child / "rejected_sessions.csv", rejected, SESSION_FIELDS + EXTRA_FIELDS)
        hourly_rows = [dict(day_index=h // 24, hour_of_day=h % 24, context=code,
                            data_origin="SYNTHETIC", energy_kwh=values[h], average_power_kw=values[h])
                       for h in range(days * 24) for code, values in hourly.items()]
        write_csv(child / "hourly_demand.csv", hourly_rows, tuple(hourly_rows[0]))
        child_report = {
            "schema_version": "same_day_deferral_run_v1", "data_origin": "SYNTHETIC", "seed": seed,
            "baseline_run": entry["run_folder"], "baseline_energy": base_energy, "deferred_energy": alt_energy,
            "checks": dict(candidate_draws_unchanged=True, original_day_and_window_preserved=True,
                           no_owner_overlaps=True, energy_conserved=True, baseline_replay_matches=True),
            "output_sha256": {path.name: sha256(path) for path in child.glob("*.csv")},
        }
        save_json(child / "sensitivity_run_report.json", child_report)
        child_reports.append(dict(replicate=index, seed=seed,
                                  report_path=(child / "sensitivity_run_report.json").relative_to(root).as_posix(),
                                  report_sha256=sha256(child / "sensitivity_run_report.json")))
        all_comparisons.extend(dict(replicate=index, seed=seed, **row) for row in comparison)
        all_profiles.extend(dict(replicate=index, seed=seed, **row) for row in profiles)
        result["replicates_completed"] = index
        save_json(output / "scheduling_sensitivity_report.json", result)
        print(f"Paired replicate {index}/{n}: checks=PASS", flush=True)
    summary, mean_profiles = [], []
    for code in GROUPS:
        rows = [row for row in all_comparisons if row["context"] == code]
        count = sum(row["study_candidate_starts"] for row in rows)
        a = sum(row["baseline_rejected_starts"] for row in rows)
        b = sum(row["deferred_rejected_starts"] for row in rows)
        summary.append(dict(
            context=code, pooled_study_candidate_starts=count,
            baseline_rejection_percent=100 * a / count if count else None,
            deferred_rejection_percent=100 * b / count if count else None,
            baseline_mean_daily_energy_kwh=statistics.mean(row["baseline_mean_daily_energy_kwh"] for row in rows),
            deferred_mean_daily_energy_kwh=statistics.mean(row["deferred_mean_daily_energy_kwh"] for row in rows),
            mean_daily_energy_difference_kwh=statistics.mean(row["daily_energy_difference_kwh"] for row in rows),
            gained_accepted_starts=sum(row["gained_accepted_starts"] for row in rows),
            lost_accepted_starts=sum(row["lost_accepted_starts"] for row in rows),
            delayed_accepted_starts=sum(row["delayed_accepted_starts"] for row in rows),
        ))
        for clock in range(24):
            values = [row for row in all_profiles if row["context"] == code and row["hour_of_day"] == clock]
            mean_profiles.append(dict(context=code, hour_of_day=clock,
                                      baseline_mean_power_kw=statistics.mean(row["baseline_mean_power_kw"] for row in values),
                                      deferred_mean_power_kw=statistics.mean(row["deferred_mean_power_kw"] for row in values),
                                      mean_difference_kw=statistics.mean(row["difference_kw"] for row in values)))
    for case in ("baseline", "deferred"):
        require(close(math.fsum(row[f"{case}_mean_power_kw"] for row in mean_profiles if row["context"] == "TOTAL"),
                      summary[-1][f"{case}_mean_daily_energy_kwh"]), "Ensemble mean profile does not conserve daily energy.")
    require(all(sha256(path) == digest for path, digest in watched.items()), "An input changed during the paired test.")
    for name, rows in (("paired_comparisons.csv", all_comparisons), ("paired_profiles.csv", all_profiles),
                       ("comparison_summary.csv", summary), ("mean_profile_comparison.csv", mean_profiles)):
        write_csv(output / name, rows, tuple(rows[0]))
    (output / "owner_readiness.csv").write_bytes((folder / "owner_readiness.csv").read_bytes())
    result.update(status="complete", all_paired_checks_passed=True, summary=summary, child_reports=child_reports,
                  input_sha256={path.relative_to(root).as_posix(): digest for path, digest in watched.items()},
                  output_sha256={path.name: sha256(path) for path in output.glob("*.csv")})
    save_json(output / "scheduling_sensitivity_report.json", result)
    return output, result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", required=True)
    args = parser.parse_args()
    output, report = run_sensitivity(Path(args.batch))
    print("Paired scheduling sensitivity complete.")
    for row in report["summary"]:
        a, b = row["baseline_rejection_percent"], row["deferred_rejection_percent"]
        rejection = f"{a:.2f}% -> {b:.2f}%" if a is not None else "N/A (no candidate starts)"
        print(f"{row['context']}: rejection={rejection}, mean_daily_kwh={row['baseline_mean_daily_energy_kwh']:.3f} -> {row['deferred_mean_daily_energy_kwh']:.3f}")
    total = report["summary"][-1]
    for key in ("gained_accepted_starts", "lost_accepted_starts", "delayed_accepted_starts"):
        print(f"{key}: {total[key]}")
    print("All paired scheduling and energy checks passed.")
    print(f"Output: {output.relative_to(ROOT)}")
    print("Sensitivity assumption only; baseline retained; no empirical validation.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError, OverflowError) as exc:
        print(f"Scheduling sensitivity failed: {exc}", file=sys.stderr)
        raise SystemExit(1)