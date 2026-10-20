"""Prepare owner-conditioned usual-energy sampling cases and frequency links."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import random
import sys

import numpy as np
import scipy

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
from ev_demand.parameters import build_simulator_parameters as baseline
from ev_demand.parameters.build_population_evidence import CONTEXTS, interval_kind, require, write_csv
from ev_demand.parameters.fit_usual_energy_candidates import FAMILIES, log_contributions, prepare_energy

CASES = {"bounded_uniform_test": None, "conditional_gamma_test": "gamma",
         "conditional_lognormal_test": "lognormal"}
EVIDENCE_FILES = {"numeric_evidence.csv", "numeric_coverage.csv", "context_windows.csv",
                  "categorical_answer_counts.csv", "linked_owner_evidence.jsonl"}
FIT_FILES = {"energy_evidence_audit.csv", "energy_candidate_fits.csv",
             "optimizer_attempts.csv", "likelihood_group_checks.csv"}
FREQUENCY_FILES = {"frequency_owner_audit.csv", "frequency_context_constraints.csv",
                   "joint_frequency_test_draws.csv"}


def conditional_quantiles(family, candidate, low, high, quantiles):
    u = np.asarray(quantiles, dtype=float)
    require(np.all(np.isfinite(u)) and np.all((u > 0) & (u < 1))
            and 0 <= low <= high and high > 0, "Invalid energy bounds or common quantiles.")
    if low == high:
        return np.full(u.shape, low, dtype=float)
    if family is None:
        require(math.isfinite(high), "Uniform sampling needs a finite upper bound.")
        values = low + (high - low) * u
    else:
        shape, scale = candidate["shape"], candidate["scale_kwh"]
        require(shape > 0 and scale > 0 and math.isfinite(shape) and math.isfinite(scale),
                "Invalid fitted energy parameters.")
        dist = FAMILIES[family](shape, loc=0, scale=scale)
        if float(dist.cdf(high)) <= 0.5:
            a, b = float(dist.cdf(low)), float(dist.cdf(high))
            require(0 <= a < b <= 1, "Insufficient numeric precision for the conditional CDF.")
            values = dist.ppf(a + (b - a) * u)
        else:
            a, b = float(dist.sf(low)), float(dist.sf(high))
            require(0 <= b < a <= 1, "Insufficient numeric precision for the conditional survival function.")
            values = dist.isf((1 - u) * a + u * b)
    require(np.all(np.isfinite(values)) and np.all(values > 0)
            and np.all(values >= low) and (not math.isfinite(high) or np.all(values <= high)),
            "A conditional energy draw violated its positive reported support; no clipping was applied.")
    return np.asarray(values, dtype=float)


def build_model(evidence_folder, fits_folder, frequency_folder, draws_per_owner=100,
                seed=20261017, project_root=ROOT):
    require(isinstance(draws_per_owner, int) and draws_per_owner > 0, "Use a positive draw count.")
    root, watched = Path(project_root).resolve(), {}
    allowed = (root / "data/processed/synthetic").resolve()

    def watch(value):
        path = (root / value).resolve()
        require(path.is_relative_to(root) and path.is_file(), "Missing project input: " + str(value))
        digest = baseline.sha256(path)
        require(path not in watched or watched[path] == digest, "Input changed while being read.")
        watched[path] = digest
        return path

    def package(value, report_name, schema, status, files):
        folder = (root / value).resolve()
        require(folder.is_relative_to(allowed) and folder.is_dir(), "Use a completed synthetic input folder.")
        report = json.loads(watch(folder / report_name).read_text(encoding="utf-8-sig"))
        require(report.get("schema_version") == schema and report.get("status") == status
                and report.get("data_origin") == "SYNTHETIC", "Unexpected package: " + report_name)
        require(bool(report.get("checks")) and all(v is True for v in report["checks"].values()),
                "An upstream check did not pass: " + report_name)
        require(set(report["output_sha256"]) == files, "Unexpected export manifest: " + report_name)
        for name in sorted(files):
            path = watch(folder / name)
            require(watched[path] == report["output_sha256"][name], "Export hash mismatch: " + name)
        return folder, report

    for name in ("docs/research_pipeline_specification.md",
                 "src/ev_demand/parameters/build_simulator_parameters.py",
                 "src/ev_demand/parameters/build_population_evidence.py",
                 "src/ev_demand/parameters/fit_usual_energy_candidates.py",
                 "src/ev_demand/parameters/build_usual_energy_model.py"):
        watch(name)
    evidence, spec = package(evidence_folder, "behaviour_population_spec.json",
        "behaviour_population_evidence_v1", "evidence_prepared", EVIDENCE_FILES)
    fits, fitted = package(fits_folder, "usual_energy_fit_report.json",
        "usual_energy_candidate_fits_v1", "candidates_for_review", FIT_FILES)
    frequency, frequency_report = package(frequency_folder, "joint_frequency_model.json",
        "joint_frequency_rate_model_v2", "parameter_sampling_test_complete", FREQUENCY_FILES)
    require(spec.get("context_labels") == CONTEXTS and fitted.get("parameter_id") == "usual_energy_kwh",
            "Unexpected parameter/context definitions.")
    require(draws_per_owner == frequency_report["draws_per_eligible_owner"],
            "Use the same draw count as the supplied joint-frequency parameter test.")
    for name in EVIDENCE_FILES | {"behaviour_population_spec.json"}:
        path = evidence / name
        key = path.relative_to(root).as_posix()
        require(fitted["input_sha256"].get(key) == watched[path]
                and frequency_report["input_sha256"].get(key) == watched[path],
                "Energy fits and frequency model must use this exact evidence snapshot: " + name)

    donors = [json.loads(line) for line in (evidence / "linked_owner_evidence.jsonl").read_text(
        encoding="utf-8").splitlines() if line.strip()]
    ids = {r["owner_id"] for r in donors}
    require(bool(ids) and "" not in ids and len(ids) == len(donors) == spec["owners_retained"],
            "Invalid retained-owner accounting.")
    all_numeric = baseline.read_csv(evidence / "numeric_evidence.csv")
    require(len(all_numeric) == spec["numeric_evidence_rows"], "Numeric evidence count mismatch.")
    energy_rows = [r for r in all_numeric if r["parameter_id"] == "usual_energy_kwh" and not r["legacy_context"]]
    require(len(energy_rows) == len(ids) and {r["owner_id"] for r in energy_rows} == ids,
            "Incomplete energy-owner links.")
    by_id = {r["owner_id"]: r for r in energy_rows}
    audit, intervals = prepare_energy(energy_rows)
    prepared = {r["owner_id"]: r for r in audit}
    saved_audit = baseline.read_csv(fits / "energy_evidence_audit.csv")
    require(len(saved_audit) == len(ids) and {r["owner_id"] for r in saved_audit} == ids,
            "Incomplete fitted-energy audit.")
    for saved in saved_audit:
        reproduced = prepared[saved["owner_id"]]
        require(all(saved[k] == str(v) for k, v in reproduced.items()), "Saved energy inclusion did not reproduce.")
    require(len(intervals) == fitted["owners_used"] and len(ids) - len(intervals) == fitted["owners_excluded"],
            "Fitted energy coverage mismatch.")
    candidates = {r["family"]: r for r in fitted["candidates"]}
    table = baseline.read_csv(fits / "energy_candidate_fits.csv")
    require(len(candidates) == len(table) == 2 and set(candidates) == set(FAMILIES)
            and {r["family"] for r in table} == set(FAMILIES), "Invalid candidate families.")
    for row in table:
        require(row == {k: str(v) for k, v in candidates[row["family"]].items()}, "Candidate CSV/JSON mismatch.")
    groups = Counter(intervals)
    low = np.array([key[0] for key in sorted(groups)])
    high = np.array([key[1] for key in sorted(groups)])
    counts = np.array([groups[key] for key in sorted(groups)])
    for family, candidate in candidates.items():
        logs = log_contributions(family, candidate["shape"], candidate["scale_kwh"], low, high)
        ll = float(np.dot(counts, logs))
        require(candidate["fixed_location_kwh"] == 0 and candidate["n_owners_used"] == len(intervals)
                and math.isfinite(ll) and math.isclose(ll, candidate["log_likelihood"], abs_tol=1e-6, rel_tol=0),
                "Candidate likelihood did not reproduce.")

    freq_owners = {r["owner_id"]: r for r in frequency_report["owners"]}
    require(len(freq_owners) == len(frequency_report["owners"]) == len(ids) and set(freq_owners) == ids,
            "Incomplete joint-frequency owner links.")
    frequency_rows = baseline.read_csv(frequency / "joint_frequency_test_draws.csv")
    freq_draws = {(r["owner_id"], int(r["test_draw"])): r for r in frequency_rows}
    require(len(freq_draws) == len(frequency_rows) == frequency_report["parameter_test_draws"],
            "Invalid or duplicate frequency test draws.")
    for oid, record in freq_owners.items():
        require(isinstance(record["joint_region"]["sample_allowed"], bool), "Invalid frequency availability flag.")
        expected_draws = draws_per_owner if record["joint_region"]["sample_allowed"] else 0
        require({i for owner_id, i in freq_draws if owner_id == oid} == set(range(1, expected_draws + 1)),
                "Incomplete frequency draw grid.")
    require(all(oid in ids for oid, i in freq_draws), "Frequency draw has no source owner.")

    owner_models, owner_audit, draws = [], [], []
    summary_values, summary_ids, summary_joint_ids = defaultdict(list), defaultdict(set), defaultdict(set)
    capacity_counts, capacity_above = Counter(), Counter()
    case_counts, joint_counts = Counter(), Counter()
    for donor in sorted(donors, key=lambda r: r["owner_id"]):
        oid, owner, readiness = donor["owner_id"], donor["owner_features"], donor["original_readiness"]
        require(all(r["owner_id"] == oid and r["data_origin"] == "SYNTHETIC" for r in (donor, owner, readiness)),
                "Owner provenance mismatch.")
        row, energy = by_id[oid], prepared[oid]
        require(row["data_origin"] == "SYNTHETIC" and row["original_readiness"] == readiness["status"],
                "Energy readiness/origin mismatch.")
        for field, source in (("raw_min", "usual_energy_kwh_min"), ("raw_max", "usual_energy_kwh_max"),
                              ("raw_state", "usual_energy_kwh_state"), ("raw_boundary", "usual_energy_kwh_boundary"),
                              ("raw_answer", "usual_energy_answer")):
            require(row[field] == owner[source], "Energy evidence differs from linked owner features.")
        freq = freq_owners[oid]
        require(freq["original_readiness"] == readiness, "Energy/frequency readiness links differ.")
        frequency_available = freq["joint_region"]["sample_allowed"]
        battery_kind = interval_kind(owner["battery_kwh_min"], owner["battery_kwh_max"],
                                     owner["battery_kwh_state"], "kWh")
        cap_upper = (float(owner["battery_kwh_max"]) if battery_kind in {"finite_interval", "point", "upper_bound_only"}
                     and owner["battery_kwh_max"].strip() and float(owner["battery_kwh_max"]) > 0 else None)
        lo = float(row["raw_min"]) if energy["fit_included"] and row["raw_min"].strip() else 0.0
        hi = float(row["raw_max"]) if energy["fit_included"] and row["raw_max"].strip() else math.inf
        cohort = "bounded" if math.isfinite(hi) else "open_upper"
        owner_seed = int.from_bytes(hashlib.sha256(f"usual-energy-v1|{seed}|{oid}".encode()).digest()[:8], "big")
        rng = random.Random(owner_seed)
        u = np.array([(rng.getrandbits(52) + 0.5) / (1 << 52) for _ in range(draws_per_owner)])
        case_rules = {}
        for case, family in CASES.items():
            reason = energy["exclusion_reason"]
            if not reason and lo == hi and row["raw_boundary"] != "closed":
                reason = "point_boundary_requires_review"
            if not reason and family is None and not math.isfinite(hi):
                reason = "uniform_upper_bound_unresolved"
            case_rules[case] = dict(sample_allowed=not bool(reason), deferral_reason=reason)
            owner_audit.append(dict(owner_id=oid, original_readiness=readiness["status"], case=case,
                vehicle_class_answer=owner["vehicle_class_answer"], raw_energy_answer=row["raw_answer"],
                raw_energy_min=row["raw_min"], raw_energy_max=row["raw_max"], evidence_kind=row["evidence_kind"],
                energy_sample_allowed=not bool(reason), deferral_reason=reason,
                joint_frequency_available=frequency_available, battery_evidence_kind=battery_kind,
                reported_battery_upper_kwh=cap_upper))
            if reason:
                continue
            values = conditional_quantiles(family, candidates.get(family), lo, hi, u)
            case_counts[case] += 1
            joint_counts[case] += int(frequency_available)
            key = (case, cohort)
            summary_values[key].extend(map(float, values))
            summary_ids[key].add(oid)
            if frequency_available:
                summary_joint_ids[key].add(oid)
            for i, value in enumerate(values, 1):
                over_capacity = bool(value > cap_upper) if cap_upper is not None else ""
                capacity_counts[key] += int(cap_upper is not None)
                capacity_above[key] += int(over_capacity is True)
                frequency_draw = freq_draws.get((oid, i))
                draws.append(dict(case=case, owner_id=oid, parameter_draw=i, energy_cohort=cohort,
                    common_quantile=float(u[i - 1]), usual_energy_kwh=float(value),
                    joint_frequency_available=frequency_available,
                    total_weekly_rate=frequency_draw["total_weekly_rate"] if frequency_draw else "",
                    exceeds_reported_battery_upper=over_capacity))
        owner_models.append(dict(owner_id=oid, original_readiness=readiness, source_energy=row,
            vehicle_class_answer=owner["vehicle_class_answer"], joint_frequency_available=frequency_available,
            battery_evidence_kind=battery_kind, reported_battery_upper_kwh=cap_upper,
            case_rules=case_rules, parameter_test_seed=owner_seed))
    require(len(owner_audit) == 3 * len(ids) and len(draws) == draws_per_owner * sum(case_counts.values()),
            "Energy owner/case/draw accounting failed.")
    summaries = []
    for case in CASES:
        for cohort in ("bounded", "open_upper"):
            key = (case, cohort)
            values = summary_values[key]
            percentiles = np.quantile(values, [0.05, 0.5, 0.95]) if values else (None, None, None)
            summaries.append(dict(case=case, energy_cohort=cohort, n_owners=len(summary_ids[key]),
                n_owners_with_joint_frequency=len(summary_joint_ids[key]), n_parameter_draws=len(values),
                mean_sampled_usual_energy_kwh=math.fsum(values) / len(values) if values else None,
                p05_sampled_usual_energy_kwh=percentiles[0], median_sampled_usual_energy_kwh=percentiles[1],
                p95_sampled_usual_energy_kwh=percentiles[2], battery_upper_comparisons=capacity_counts[key],
                draws_above_reported_battery_upper=capacity_above[key]))
    require(all(baseline.sha256(p) == old for p, old in watched.items()), "An input changed during processing.")
    require(bool(draws), "No supported energy parameter draws were produced.")
    output = allowed / ("usual_energy_model_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ"))
    output.mkdir(parents=True, exist_ok=False)
    for name, rows in (("energy_owner_case_audit.csv", owner_audit), ("usual_energy_test_draws.csv", draws),
                       ("energy_case_summary.csv", summaries)):
        write_csv(output / name, rows)
    report = dict(schema_version="usual_energy_sampling_model_v1", status="parameter_sampling_test_complete",
        data_origin="SYNTHETIC", behaviour_input_origin="SYNTHETIC", output_origin="COMPUTED_PARAMETER_MODEL",
        owners_retained=len(ids), informative_energy_owners=len(intervals), unresolved_or_excluded_energy_owners=len(ids) - len(intervals),
        owners_with_energy_draws_by_case={case: case_counts[case] for case in CASES},
        owners_with_energy_and_joint_frequency_by_case={case: joint_counts[case] for case in CASES},
        parameter_test_draws=len(draws), draws_per_eligible_owner=draws_per_owner, base_seed=seed,
        candidates=candidates, owners=owner_models, electrical_boundary=fitted["electrical_boundary"],
        assumptions=dict(
            OWNER_USUAL_ENERGY_V1="Each parameter draw represents an owner's latent usual session energy; it does not estimate within-owner session variation.",
            OWNER_CONDITIONING_V1="Owner reporting intervals and frequency links are retained. Candidate families are conditioned on that owner's energy bounds.",
            UNIFORM_BOUNDED_ENERGY_TEST_V1="Uniform positive energy inside a finite reported interval; open-upper answers remain deferred in this case.",
            CONDITIONAL_ENERGY_FAMILY_TEST_V1="Pooled fitted Gamma/Lognormal shapes provide alternative within-interval/tail laws. Category frequencies remain those of retained owner records.",
            COMMON_QUANTILES_TEST_V1="The same owner/draw quantile is used across supported energy cases. Energy and frequency draws are conditionally independent within the linked owner's constraints."),
        limits=[
            "No lowest-AIC family is automatically selected for the population simulator.",
            "Compare cases on the same supported owner cohort; the bounded-uniform case excludes open-upper energy reports.",
            "Unresolved energy reports are not imputed. No arbitrary tail cap or battery-capacity clipping is applied.",
            "The fitted shape/scale values are fixed; parameter-estimation uncertainty has not been propagated.",
            "Candidate fits describe owner-level usual energy, not a measured per-session distribution or vehicle-specific within-bin behaviour.",
            "Battery-upper comparisons are boundary-dependent review flags, not proof of survey error or a validated physical compatibility test.",
            "Retain one sampled usual energy per virtual owner/parameter realisation across contexts unless a separate session-variation model is specified.",
            "No SOC-derived or distance-derived energy is added to reported energy; conversion losses remain unresolved.",
            "Frequency links do not establish full owner readiness: charger, home power, physical compatibility and accepted-session scheduling still require resolution.",
            "These are parameter test draws, not demand profiles, virtual populations, national forecasts or empirical validation."],
        simulator_activation=False, owner_eligibility_changed=False, population_generation_ready=False,
        district_demand_ready=False, empirical_validation=False,
        software=dict(numpy=np.__version__, scipy=scipy.__version__),
        checks=dict(source_manifests_match=True, shared_evidence_snapshot_verified=True,
            source_owner_and_frequency_links_verified=True, fitted_likelihoods_reproduced=True,
            conditional_draw_support_checked=True, complete_case_accounting=True, input_hashes_unchanged=True),
        input_sha256={p.relative_to(root).as_posix(): old for p, old in watched.items()},
        output_sha256={p.name: baseline.sha256(p) for p in sorted(output.iterdir())})
    require(all(baseline.sha256(p) == old for p, old in watched.items()), "An input changed during export.")
    (output / "usual_energy_model.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    return output, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--fits", required=True)
    parser.add_argument("--frequency", required=True)
    parser.add_argument("--draws-per-owner", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20261017)
    args = parser.parse_args()
    output, report = build_model(args.evidence, args.fits, args.frequency, args.draws_per_owner, args.seed)
    print("Owner-conditioned usual-energy parameter model complete.")
    for key in ("owners_retained", "informative_energy_owners", "unresolved_or_excluded_energy_owners"):
        print(f"{key}: {report[key]}")
    for case in CASES:
        print(f"{case}: energy_owners={report['owners_with_energy_draws_by_case'][case]}, "
              f"energy_and_frequency_owners={report['owners_with_energy_and_joint_frequency_by_case'][case]}")
    print(f"parameter_test_draws: {report['parameter_test_draws']}")
    print("Source, likelihood, owner-link and conditional-support checks passed.")
    print(f"Output: {output.relative_to(ROOT)}")
    print("Sampling assumptions only; battery comparisons require boundary review; no demand simulations or eligibility changes.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError, RuntimeError) as exc:
        print(f"Usual-energy model preparation failed: {exc}", file=sys.stderr)
        raise SystemExit(1)