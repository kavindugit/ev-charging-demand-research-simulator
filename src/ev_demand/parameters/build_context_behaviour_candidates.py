"""Build linked context timing/charger candidates from preserved survey evidence."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
from ev_demand.parameters import build_simulator_parameters as baseline
from ev_demand.parameters.build_population_evidence import (
    CONTEXTS, interval_kind, require, window_kind, write_csv,
)

KNOWN_CHARGERS = ("SOCKET_PORTABLE", "AC", "DC")
USE_STATUSES = ("use_category", "reported_nonuse", "unresolved_use",
                "invalid_frequency", "conflict_review")
WINDOW_KINDS = ("reported_circular_window", "unresolved", "invalid_encoding")


def use_status(row):
    flag = row["nonuse_conflict"]
    require(flag in {"False", "false", "0", "True", "true", "1"},
            "Unrecognised nonuse-conflict flag.")
    if flag in {"True", "true", "1"}:
        return "conflict_review"
    low, high = row["context_frequency_min"], row["context_frequency_max"]
    state = row["context_frequency_state"]
    kind = interval_kind(low, high, state, "sessions")
    if kind == "invalid_encoding":
        return "invalid_frequency"
    if state == "reported_zero":
        return "reported_nonuse" if low.strip() and high.strip() and float(low) == float(high) == 0 else "invalid_frequency"
    if kind in {"unresolved", "not_applicable"}:
        return "unresolved_use"
    if row["context_frequency_period"] not in {"week", "month"}:
        return "invalid_frequency"
    positive_support = (bool(low.strip()) and float(low) > 0) or (bool(high.strip()) and float(high) > 0)
    return "use_category" if positive_support else "unresolved_use"


def hourly_probabilities(start, end, length):
    require(all(math.isfinite(v) for v in (start, end, length))
            and 0 <= start < 24 and 0 <= end < 24 and 0 < length <= 24
            and math.isclose((end - start) % 24, length % 24, abs_tol=1e-8),
            "Invalid circular start window.")
    probabilities = []
    for hour in range(24):
        overlap = sum(max(0.0, min(start + length, hour + shift + 1)
                          - max(start, hour + shift)) for shift in (0, 24))
        probabilities.append(overlap / length)
    require(all(0 <= p <= 1 for p in probabilities)
            and math.isclose(sum(probabilities), 1, abs_tol=1e-10, rel_tol=0),
            "Circular window probability was not conserved.")
    return probabilities


def summarise(audit):
    coverage, chargers, hourly, joint = [], [], [], []
    for code, label in CONTEXTS.items():
        rows = [r for r in audit if r["legacy_context"] == code]
        used = [r for r in rows if r["use_status"] == "use_category"]
        valid = [r for r in used if r["timing_candidate_included"]]
        n, m = len(used), len(valid)
        types = Counter(r["charger_code_raw"] for r in used)
        known = sum(types[t] for t in KNOWN_CHARGERS)
        prefix = dict(legacy_context=code, charging_context=label)
        counts = Counter(r["use_status"] for r in rows)
        windows = Counter(r["window_evidence_kind"] for r in used)
        require(set(counts) <= set(USE_STATUSES) and sum(counts.values()) == len(rows),
                "Context-use accounting failed.")
        require(set(windows) <= set(WINDOW_KINDS) and sum(windows.values()) == n,
                "Context-window accounting failed.")
        coverage.append(dict(prefix, n_retained_owners=len(rows),
            **{"n_" + status: counts[status] for status in USE_STATUSES},
            n_valid_windows=m, n_unresolved_windows=windows["unresolved"],
            n_invalid_windows=windows["invalid_encoding"], n_resolved_charger_reports=known,
            n_other_or_missing_charger_reports=n - known,
            valid_window_share_among_use_reports=m / n if n else "",
            n_use_reports_with_cross_field_flags=sum(bool(r["cross_field_review_flags"]) for r in used)))
        for charger in sorted(set(KNOWN_CHARGERS) | set(types)):
            resolved = charger in KNOWN_CHARGERS
            chargers.append(dict(prefix, charger_code_raw=charger, resolved_type=resolved,
                n_owner_reports=types[charger], n_use_reports=n, n_resolved_type_reports=known,
                share_among_use_reports=types[charger] / n if n else "",
                share_among_resolved_type_reports=types[charger] / known if resolved and known else ""))
        vectors = [hourly_probabilities(float(r["raw_start_hour"]), float(r["raw_end_hour"]),
                                       float(r["raw_length_hours"])) for r in valid]
        totals = [math.fsum(v[h] for v in vectors) for h in range(24)]
        conditional = [total / m if m else "" for total in totals]
        known_mass = [total / n if n else "" for total in totals]
        require(not m or math.isclose(math.fsum(conditional), 1, abs_tol=1e-10, rel_tol=0),
                "Conditional hourly probabilities do not sum to one.")
        require(not n or math.isclose(math.fsum(known_mass), m / n, abs_tol=1e-10, rel_tol=0),
                "Known-window mass does not match window coverage.")
        require(not n or math.isclose(sum(types.values()) / n, 1, abs_tol=1e-10),
                "Charger shares do not reconcile.")
        for h in range(24):
            hourly.append(dict(prefix, hour_of_day=h, n_use_reports=n, n_valid_window_reports=m,
                conditional_start_probability=conditional[h], known_window_mass_per_use_report=known_mass[h]))
        grouped = defaultdict(list)
        fields = ("window_evidence_kind", "raw_window_state", "raw_start_hour", "raw_end_hour",
                  "raw_length_hours", "charger_code_raw")
        for row in used:
            grouped[tuple(row[f] for f in fields)].append(row["owner_id"])
        for values, ids in sorted(grouped.items()):
            joint.append(dict(prefix, **dict(zip(fields, values)), n_owner_reports=len(ids),
                n_use_reports=n, empirical_report_share=len(ids) / n,
                donor_ids_json=json.dumps(sorted(ids), ensure_ascii=False)))
        require(sum(len(ids) for ids in grouped.values()) == n, "Joint category accounting failed.")
    return coverage, chargers, hourly, joint


def build_candidates(evidence_folder, project_root=ROOT):
    root, watched = Path(project_root).resolve(), {}

    def watch(value):
        path = (root / value).resolve()
        require(path.is_relative_to(root) and path.is_file(), "Missing project input: " + str(value))
        digest = baseline.sha256(path)
        require(path not in watched or watched[path] == digest, "Input changed while being read.")
        watched[path] = digest
        return path

    for name in ("docs/research_pipeline_specification.md",
                 "src/ev_demand/parameters/build_simulator_parameters.py",
                 "src/ev_demand/parameters/build_population_evidence.py",
                 "src/ev_demand/parameters/build_context_behaviour_candidates.py"):
        watch(name)
    folder = (root / evidence_folder).resolve()
    allowed = (root / "data/processed/synthetic").resolve()
    require(folder.is_relative_to(allowed) and folder.is_dir(),
            "Use the completed synthetic population-evidence folder.")
    spec = json.loads(watch(folder / "behaviour_population_spec.json").read_text(encoding="utf-8-sig"))
    require(spec.get("schema_version") == "behaviour_population_evidence_v1"
            and spec.get("status") == "evidence_prepared" and spec.get("data_origin") == "SYNTHETIC"
            and spec.get("context_labels") == CONTEXTS, "Unexpected population-evidence specification.")
    require(bool(spec.get("checks")) and all(v is True for v in spec["checks"].values()),
            "An upstream population-evidence check did not pass.")
    names = {"numeric_evidence.csv", "numeric_coverage.csv", "context_windows.csv",
             "categorical_answer_counts.csv", "linked_owner_evidence.jsonl"}
    require(set(spec["output_sha256"]) == names, "Unexpected evidence manifest.")
    for name in sorted(names):
        path = watch(folder / name)
        require(watched[path] == spec["output_sha256"][name], "Evidence export hash mismatch: " + name)
    donors = [json.loads(line) for line in (folder / "linked_owner_evidence.jsonl").read_text(
        encoding="utf-8").splitlines() if line.strip()]
    ids = {r["owner_id"] for r in donors}
    require(bool(ids) and "" not in ids and len(donors) == len(ids) == spec["owners_retained"],
            "Invalid linked-owner accounting.")
    expected = {(oid, code) for oid in ids for code in CONTEXTS}

    def index(rows, context_field):
        keys = [(r["owner_id"], r[context_field]) for r in rows]
        require(len(keys) == len(set(keys)) and set(keys) == expected,
                "Incomplete or duplicate owner/context evidence.")
        return dict(zip(keys, rows))

    window_rows = baseline.read_csv(folder / "context_windows.csv")
    require(len(window_rows) == spec["context_window_rows"] == spec["context_rows_retained"],
            "Context-window row count mismatch.")
    windows = index(window_rows, "legacy_context")
    numeric = baseline.read_csv(folder / "numeric_evidence.csv")
    require(len(numeric) == spec["numeric_evidence_rows"], "Numeric evidence row count mismatch.")
    frequencies = index([r for r in numeric if r["parameter_id"] == "context_frequency"], "legacy_context")
    audit, status_counts = [], Counter()
    for donor in sorted(donors, key=lambda r: r["owner_id"]):
        oid, owner, readiness = donor["owner_id"], donor["owner_features"], donor["original_readiness"]
        status = readiness["status"]
        require(all(r["owner_id"] == oid and r["data_origin"] == "SYNTHETIC"
                    for r in (donor, owner, readiness)), "Owner provenance mismatch.")
        require(status in {"ready_for_bounded_test", "deferred_for_review"}, "Unknown original readiness.")
        status_counts[status] += 1
        sources = donor["context_features"]
        require(len(sources) == 4 and {r["context"] for r in sources} == set(CONTEXTS),
                "Each linked owner must retain four context rows.")
        for source in sorted(sources, key=lambda r: r["context"]):
            code = source["context"]
            require(source["owner_id"] == oid and source["data_origin"] == "SYNTHETIC",
                    "Context provenance mismatch.")
            window, frequency = windows[(oid, code)], frequencies[(oid, code)]
            for row in (window, frequency):
                require(row["data_origin"] == "SYNTHETIC" and row["original_readiness"] == status
                        and row["charging_context"] == CONTEXTS[code], "Evidence context/status mismatch.")
            window_kind_value = window_kind(source)
            reproduced = dict(context_frequency_state=source["context_frequency_state"],
                nonuse_conflict=source["nonuse_conflict"], raw_window_state=source["window_state"],
                raw_start_hour=source["window_start_hour"], raw_end_hour=source["window_end_hour"],
                raw_length_hours=source["window_length_hours"], evidence_kind=window_kind_value,
                charger_code=source["charger_code"])
            require(all(window[k] == v for k, v in reproduced.items()), "Window evidence did not reproduce.")
            kind = interval_kind(source["context_frequency_min"], source["context_frequency_max"],
                                 source["context_frequency_state"], "sessions")
            if source["nonuse_conflict"] in {"True", "true", "1"}:
                kind = "conflict_review"
            reproduced = dict(raw_min=source["context_frequency_min"], raw_max=source["context_frequency_max"],
                raw_state=source["context_frequency_state"], raw_boundary=source.get("context_frequency_boundary", ""),
                raw_answer=source["frequency_answer"], reported_period=source["context_frequency_period"],
                unit="sessions", evidence_kind=kind)
            require(all(frequency[k] == v for k, v in reproduced.items()), "Frequency evidence did not reproduce.")
            use = use_status(source)
            flags = []
            if use == "use_category" and code == "T2":
                if owner["home_access_answer"] == "No, I cannot charge at home":
                    flags.append("home_use_with_no_home_access")
                if source["charger_code"] == "DC":
                    flags.append("home_dc_requires_review")
            audit.append(dict(owner_id=oid, data_origin="SYNTHETIC", original_readiness=status,
                legacy_context=code, charging_context=CONTEXTS[code], use_status=use,
                raw_frequency_answer=source["frequency_answer"], raw_frequency_state=source["context_frequency_state"],
                raw_frequency_min=source["context_frequency_min"], raw_frequency_max=source["context_frequency_max"],
                raw_frequency_period=source["context_frequency_period"], nonuse_conflict=source["nonuse_conflict"],
                raw_window_state=source["window_state"], raw_start_hour=source["window_start_hour"],
                raw_end_hour=source["window_end_hour"], raw_length_hours=source["window_length_hours"],
                window_evidence_kind=window_kind_value,
                timing_candidate_included=use == "use_category" and window_kind_value == "reported_circular_window",
                charger_code_raw=source["charger_code"], resolved_charger_type=source["charger_code"] in KNOWN_CHARGERS,
                home_access_answer=owner["home_access_answer"], home_equipment_answer=owner["home_equipment_answer"],
                cross_field_review_flags=";".join(flags)))
    require(status_counts["ready_for_bounded_test"] == spec["original_ready_owners"]
            and status_counts["deferred_for_review"] == spec["original_deferred_owners"],
            "Original readiness totals disagree.")
    coverage, chargers, hourly, joint = summarise(audit)
    require(bool(joint), "No usable context-frequency reports; review evidence before making candidates.")
    require(all(baseline.sha256(p) == old for p, old in watched.items()), "An input changed during processing.")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    output = allowed / ("context_behaviour_candidates_" + stamp)
    output.mkdir(parents=True, exist_ok=False)
    tables = {"context_report_audit.csv": audit, "context_coverage.csv": coverage,
              "charger_report_shares.csv": chargers, "start_hour_candidates.csv": hourly,
              "joint_window_charger_categories.csv": joint}
    for name, rows in tables.items():
        write_csv(output / name, rows)
    model = dict(schema_version="context_behaviour_candidates_v1", status="candidates_prepared",
        data_origin="SYNTHETIC", owners_retained=len(donors), context_records_reviewed=len(audit),
        original_ready_owners=spec["original_ready_owners"], original_deferred_owners=spec["original_deferred_owners"],
        context_labels=CONTEXTS, context_summary=coverage,
        assumptions={"TIMING_UNIFORM_WITHIN_REPORTED_WINDOW_V1":
                     "Continuous uniform timing inside each reported circular start window; not observed exact timestamps."},
        interpretation=dict(
            use_category="Frequency bounds permit positive use; categories that also permit zero are retained. Not proof of a realised session.",
            weighting="Each owner contributes once per context. Shares are conditional owner-report shares, not session, charger-stock or fleet shares.",
            timing="Conditional hourly probabilities sum to one among valid-window use reports. Known-window mass sums to coverage among all use reports; missing mass is unresolved.",
            charger="All use reports remain in the denominator, including mixed, unknown and blank codes. Resolved-only shares have a separate denominator.",
            joint="Joint categories preserve within-context window/charger links. Their donor IDs link back to whole-owner evidence; sampling contexts independently would break cross-context dependencies.",
            missing="No timing, charger, energy, frequency or home-power imputation. Other unresolved owner fields do not remove valid context evidence.",
            compatibility="Use-report inclusion is not simulator eligibility. Home DC/no-access flags require review; unknown equipment detail is not automatically a contradiction.",
            tou="No additional TOU shift; reported timing may already reflect intentional delay.",
            power="Charger type is not power. Existing public-rating/derating assumptions and home-power evidence remain separate.",
            geography="Charging contexts are not place typologies or observed charging districts. No district or national weighting is inferred."),
        simulator_activation=False, owner_eligibility_changed=False, population_generation_ready=False,
        district_demand_ready=False, empirical_validation=False,
        upstream_evidence_input_sha256=spec["input_sha256"],
        checks=dict(upstream_export_hashes_match=True, owner_context_links_reproduced=True,
            source_fields_reproduced=True, denominators_reconciled=True,
            circular_probability_mass_conserved=True, joint_categories_reconciled=True, input_hashes_unchanged=True),
        input_sha256={p.relative_to(root).as_posix(): old for p, old in watched.items()},
        output_sha256={name: baseline.sha256(output / name) for name in tables})
    require(all(baseline.sha256(p) == old for p, old in watched.items()), "An input changed during export.")
    (output / "context_candidate_model.json").write_text(
        json.dumps(model, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    return output, model


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", required=True)
    args = parser.parse_args()
    output, model = build_candidates(args.evidence)
    print("Context timing and charger candidates complete.")
    print(f"owners_retained: {model['owners_retained']}")
    print(f"context_records_reviewed: {model['context_records_reviewed']}")
    for row in model["context_summary"]:
        print(f"{row['legacy_context']}: use_reports={row['n_use_category']}, "
              f"valid_windows={row['n_valid_windows']}, "
              f"resolved_chargers={row['n_resolved_charger_reports']}, "
              f"other_or_missing_chargers={row['n_other_or_missing_charger_reports']}, "
              f"reported_nonuse={row['n_reported_nonuse']}")
    print("All source-integrity, context-link, denominator and circular-probability checks passed.")
    print(f"Output: {output.relative_to(ROOT)}")
    print("Synthetic owner-report candidates; within-window uniform timing assumed; simulator and eligibility unchanged.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"Context candidate preparation failed: {exc}", file=sys.stderr)
        raise SystemExit(1)