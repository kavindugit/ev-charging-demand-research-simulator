"""Explain rejected synthetic starts without changing sessions or parameters."""
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
from ev_demand.simulation.run_session_simulator import CONTEXTS, close, validate_package, write_csv
from ev_demand.simulation.run_repeated_session_test import describe


def checked_path(root, reference, area):
    path = (root / reference).resolve()
    require(path.is_relative_to((root / area).resolve()), "Reference leaves its permitted data folder.")
    return path


def conflict_detail(row, accepted, replicate, seed):
    blocker = accepted.get(row["conflicting_session_id"])
    require(blocker is not None, "Rejected overlap has no accepted blocking session.")
    start = row["start_hour_absolute"]
    require(blocker["owner_id"] == row["owner_id"]
            and blocker["start_hour_absolute"] <= start < blocker["end_hour_absolute"],
            "Blocking session does not overlap this owner's rejected start.")
    same_day = int(row["start_day_index"]) == int(blocker["start_day_index"])
    return {
        "replicate": replicate, "seed": seed, "data_origin": "SYNTHETIC",
        "owner_id": row["owner_id"], "rejected_session_id": row["session_id"],
        "rejected_context": row["context"], "blocking_session_id": blocker["session_id"],
        "blocking_context": blocker["context"], "same_context": row["context"] == blocker["context"],
        "same_calendar_start_day": same_day, "blocking_start_phase": blocker["start_phase"],
        "rejected_start_hour_absolute": start,
        "blocking_start_hour_absolute": blocker["start_hour_absolute"],
        "blocking_end_hour_absolute": blocker["end_hour_absolute"],
        "remaining_blocking_active_hours": blocker["end_hour_absolute"] - start,
        "blocking_active_duration_hours": blocker["active_duration_hours"],
        "blocking_effective_power_kw": blocker["effective_average_power_kw"],
        "rejected_candidate_duration_hours": row["active_duration_hours"],
        "rejected_candidate_energy_kwh": row["session_energy_kwh"],
    }


def summarize(label, candidates, conflicts, rate, weeks):
    durations = describe([row["active_duration_hours"] for row in candidates])
    remaining = [row["remaining_blocking_active_hours"] for row in conflicts]
    accepted = sum(row["diagnostic_status"] == "accepted" for row in candidates)
    rejected = len(candidates) - accepted
    same_day = sum(row["same_calendar_start_day"] for row in conflicts)
    same_context = sum(row["same_context"] for row in conflicts)
    zero = sum(row.get("rejection_reason") == "zero_energy_draw" for row in candidates)
    require(rejected == len(conflicts) + zero, "Rejected causes do not partition rejected starts.")
    return {
        "group": label, "data_origin": "SYNTHETIC", "study_candidate_starts": len(candidates),
        "study_accepted_starts": accepted, "study_rejected_starts": rejected,
        "study_rejection_percent": 100 * rejected / len(candidates) if candidates else None,
        "mean_sampled_weekly_candidate_rate": rate,
        "realized_weekly_candidate_starts": len(candidates) / weeks,
        "realized_weekly_accepted_starts": accepted / weeks,
        "candidate_duration_median_hours": durations["p50"],
        "candidate_duration_p95_hours": durations["p95"],
        "candidate_duration_max_hours": max((row["active_duration_hours"] for row in candidates), default=None),
        "candidate_durations_over_24_hours": sum(row["active_duration_hours"] > 24 for row in candidates),
        "candidate_durations_over_start_window": sum(row["active_duration_hours"] > row["diagnostic_window_hours"] for row in candidates),
        "same_day_overlap_rejections": same_day,
        "earlier_day_overlap_rejections": len(conflicts) - same_day,
        "same_context_overlap_rejections": same_context,
        "cross_context_overlap_rejections": len(conflicts) - same_context,
        "warmup_blocking_rejections": sum(row["blocking_start_phase"] == "warmup" for row in conflicts),
        "zero_energy_rejections": zero,
        "mean_remaining_blocking_active_hours": statistics.mean(remaining) if remaining else None,
    }


def diagnose_batch(batch, project_root=ROOT):
    root = project_root.resolve()
    batch = checked_path(root, batch, "data/simulated/synthetic")
    report_path = batch / "repeated_test_report.json"
    report = load_json(report_path)
    require(report.get("schema_version") == "repeated_session_engine_test_v1"
            and report.get("data_origin") == "SYNTHETIC" and report.get("status") == "complete"
            and report.get("all_replicates_audited") is True, "Expected a completed audited synthetic batch.")
    watched = {report_path: sha256(report_path)}

    def verify(path, expected):
        require(sha256(path) == expected, f"File changed since the saved run: {path.name}.")
        require(path not in watched or watched[path] == expected, "Inconsistent saved hash references.")
        watched[path] = expected

    for name, digest in report["output_sha256"].items():
        require(Path(name).name == name, "Invalid batch export name.")
        verify(batch / name, digest)
    folder = checked_path(root, report["parameter_folder"], "data/processed/synthetic")
    for name, digest in report["input_sha256"].items():
        require(Path(name).name == name, "Invalid parameter filename.")
        verify(folder / name, digest)
    for name, digest in report["source_sha256"].items():
        verify(checked_path(root, name, "src/ev_demand"), digest)
    package = load_json(folder / "simulator_parameters.json")
    validate_package(package)
    owners = {row["owner_id"]: row for row in package["owner_profiles"]}
    contexts = {(owner_id, row["context"]): row for owner_id, owner in owners.items()
                for row in owner["active_contexts"]}
    runs = read_csv(batch / "replicate_summaries.csv")
    n, days = report["replicates_completed"], report["study_days"]
    require(n == report["replicates_requested"] == len(runs) and n >= 2 and days > 0,
            "Batch replicate counts or study length disagree.")
    candidates, conflicts = [], []
    context_rates, owner_rates = defaultdict(float), defaultdict(float)
    for index, entry in enumerate(runs, 1):
        seed = int(entry["seed"])
        require(int(entry["replicate"]) == index and seed == report["base_seed"] + index - 1,
                "Unexpected replicate ordering or seed.")
        run = checked_path(root, entry["run_folder"], "data/simulated/synthetic")
        audit_folder = checked_path(root, entry["audit_folder"], "data/processed/synthetic")
        verify(run / "simulation_report.json", entry["simulation_report_sha256"])
        verify(audit_folder / "session_audit_report.json", entry["audit_report_sha256"])
        saved = load_json(run / "simulation_report.json")
        audit = load_json(audit_folder / "session_audit_report.json")
        require(saved["input_sha256"] == report["input_sha256"]
                and saved["parameter_folder"] == report["parameter_folder"]
                and saved["summary"]["seed"] == seed and saved["summary"]["study_days"] == days,
                "Replicate refers to different inputs or settings.")
        require(audit["run_folder"] == entry["run_folder"] and all(audit["checks"].values()),
                "Saved audit does not refer to this successful run.")
        for name, digest in saved["output_sha256"].items():
            require(Path(name).name == name, "Invalid run export name.")
            verify(run / name, digest)
        require(audit["input_sha256"]["simulation_report.json"] == entry["simulation_report_sha256"]
                and all(audit["input_sha256"][name] == digest for name, digest in saved["output_sha256"].items()),
                "Audit does not cover these unchanged run exports.")
        warmup = saved["summary"]["warmup_days"]
        accepted = parse_sessions(read_csv(run / "sessions.csv"), True, owners, warmup, days * 24)
        rejected = parse_sessions(read_csv(run / "rejected_sessions.csv"), False, owners, warmup, days * 24)
        accepted_map = {row["session_id"]: row for row in accepted}
        study_kept = [row for row in accepted if row["start_phase"] == "study"]
        study_dropped = [row for row in rejected if row["start_phase"] == "study"]
        require(len(study_kept) == int(entry["study_accepted_starts"])
                and len(study_dropped) == int(entry["study_rejected_starts"])
                and len(study_kept) + len(study_dropped) == int(entry["study_candidate_starts"]),
                "Replicate study-start counts disagree.")
        for status, rows in (("accepted", study_kept), ("rejected", study_dropped)):
            for row in rows:
                row["diagnostic_status"] = status
                row["diagnostic_window_hours"] = contexts[(row["owner_id"], row["context"])]["window_length_hours"]
                candidates.append(row)
        for row in study_dropped:
            if row["rejection_reason"] == "owner_already_charging":
                conflicts.append(conflict_detail(row, accepted_map, index, seed))
            else:
                require(row["rejection_reason"] == "zero_energy_draw", "Unknown rejection reason.")
        for row in read_csv(run / "owner_rates.csv"):
            require(row["owner_id"] in owners and row["context"] in CONTEXTS, "Invalid owner-rate key.")
            rate = float(row["weekly_context_rate"])
            require(math.isfinite(rate) and rate >= 0, "Invalid sampled context rate.")
            context_rates[row["context"]] += rate
            owner_rates[row["owner_id"]] += rate
        print(f"Diagnosed replicate {index}/{n}", flush=True)
    weeks = n * days / 7
    overview = []
    for code in CONTEXTS + ("TOTAL",):
        rows = candidates if code == "TOTAL" else [row for row in candidates if row["context"] == code]
        links = conflicts if code == "TOTAL" else [row for row in conflicts if row["rejected_context"] == code]
        rate = math.fsum(context_rates.values()) / n if code == "TOTAL" else context_rates[code] / n
        overview.append(summarize(code, rows, links, rate, weeks))
    by_owner = [summarize(owner_id, [row for row in candidates if row["owner_id"] == owner_id],
                          [row for row in conflicts if row["owner_id"] == owner_id],
                          owner_rates[owner_id] / n, weeks) for owner_id in sorted(owners)]
    pairs = [{"blocking_context": a, "rejected_context": b,
              "study_overlap_rejections": sum(row["blocking_context"] == a and row["rejected_context"] == b
                                              for row in conflicts)} for a in CONTEXTS for b in CONTEXTS]
    require(sum(row["study_overlap_rejections"] for row in pairs) == len(conflicts), "Conflict matrix does not balance.")
    total = overview[-1]
    expected_percent = report["pooled_study_rejection_percent"]["TOTAL"]
    require((expected_percent is None and total["study_rejection_percent"] is None)
            or (expected_percent is not None and total["study_rejection_percent"] is not None
                and close(expected_percent, total["study_rejection_percent"])), "Pooled rejection rate disagrees.")
    require(all(sha256(path) == digest for path, digest in watched.items()), "Inputs changed during diagnosis.")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    output = root / "data/processed/synthetic" / f"rejection_diagnostics_{stamp}"
    output.mkdir(parents=True, exist_ok=False)
    write_csv(output / "context_rejection_diagnostics.csv", overview, tuple(overview[0]))
    write_csv(output / "owner_rejection_diagnostics.csv", by_owner, tuple(by_owner[0]))
    write_csv(output / "conflict_context_pairs.csv", pairs, tuple(pairs[0]))
    conflict_fields = tuple(conflicts[0]) if conflicts else (
        "replicate", "seed", "data_origin", "owner_id", "rejected_session_id", "rejected_context",
        "blocking_session_id", "blocking_context", "same_context", "same_calendar_start_day",
        "blocking_start_phase", "rejected_start_hour_absolute", "blocking_start_hour_absolute",
        "blocking_end_hour_absolute", "remaining_blocking_active_hours", "blocking_active_duration_hours",
        "blocking_effective_power_kw", "rejected_candidate_duration_hours", "rejected_candidate_energy_kwh",
    )
    write_csv(output / "overlap_conflicts.csv", conflicts, conflict_fields)
    result = {
        "schema_version": "session_rejection_diagnostics_v1", "data_origin": "SYNTHETIC",
        "batch_folder": batch.relative_to(root).as_posix(), "replicates": n,
        "study_days_per_replicate": days, "owners_simulated": len(owners),
        "context_diagnostics": overview, "conflict_context_pairs": pairs,
        "input_sha256": {path.relative_to(root).as_posix(): digest for path, digest in watched.items()},
        "diagnostic_source_sha256": sha256(Path(__file__)),
        "output_sha256": {path.name: sha256(path) for path in output.glob("*.csv")},
        "interpretation": [
            "All candidate and rejected counts use study starts only; blocking accepted sessions can start in warm-up.",
            "Duration summaries include accepted and rejected study-start candidates, so rejection does not select the duration sample.",
            "Start windows constrain start time, not active duration; exceeding a window is not automatically invalid.",
            "Same-day means the same calendar start day; a midnight-wrapping start window can span two calendar days.",
            "Context rates are summed once per owner and divided by replicates; repeated weekly-total columns are not added.",
            "Rejected candidate energy is hypothetical, not measured unserved energy.",
            "These classifications describe the current generator and do not establish real-world causal effects.",
            "Diagnostics do not change any parameters or sessions, or establish convergence, calibration or national readiness.",
        ],
    }
    (output / "rejection_diagnostics_report.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return output, result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", required=True)
    args = parser.parse_args()
    output, report = diagnose_batch(Path(args.batch))
    print("Session rejection diagnostics complete.")
    total = report["context_diagnostics"][-1]
    for row in report["context_diagnostics"]:
        percent = row["study_rejection_percent"]
        label = f"{percent:.2f}%" if percent is not None else "N/A"
        median = row["candidate_duration_median_hours"]
        duration = f"{median:.2f}h" if median is not None else "N/A"
        p95 = row["candidate_duration_p95_hours"]
        tail = f"{p95:.2f}h" if p95 is not None else "N/A"
        print(f"{row['group']}: rejection={label}, candidate_duration_median={duration}, p95={tail}, durations_over_24h={row['candidate_durations_over_24_hours']}")
        print(f"  weekly_starts: sampled_candidate_rate={row['mean_sampled_weekly_candidate_rate']:.2f}, realized_candidates={row['realized_weekly_candidate_starts']:.2f}, realized_accepted={row['realized_weekly_accepted_starts']:.2f}")
    for key in ("same_day_overlap_rejections", "earlier_day_overlap_rejections",
                "same_context_overlap_rejections", "cross_context_overlap_rejections",
                "warmup_blocking_rejections", "zero_energy_rejections"):
        print(f"{key}: {total[key]}")
    print("Largest blocking-context -> rejected-context pairs:")
    pairs = sorted(report["conflict_context_pairs"], key=lambda row: (-row["study_overlap_rejections"], row["blocking_context"], row["rejected_context"]))
    for row in [item for item in pairs if item["study_overlap_rejections"] > 0][:5]:
        print(f"{row['blocking_context']} -> {row['rejected_context']}: {row['study_overlap_rejections']}")
    print(f"Output: {output.relative_to(ROOT)}")
    print("Diagnostics only; existing sessions and parameters unchanged.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError, OverflowError) as exc:
        print(f"Rejection diagnosis failed: {exc}", file=sys.stderr)
        raise SystemExit(1)