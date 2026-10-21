"""Resolve declared district aliases in saved synthetic charging-geography evidence."""

import argparse
import csv
import json
import math
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
from ev_demand.geography import prepare_charging_geography_evidence as original

sources = original.sources
require, resolve, rel, sha = sources.require, sources.resolve, sources.rel, sources.sha
table, text = original.table, original.text
COHORTS = ("common_energy_cases", "open_upper_extension", "outside_screened_cohorts")
NO_AWAY = "i do not charge away from home"


def norm(value):
    return text(value).casefold()


def boolean(value):
    require(value in ("True", "False"), f"Invalid required boolean: {value!r}")
    return value == "True"


def alias_index(districts, aliases):
    identities = {row["district_id"]: row["district_name"] for row in districts}
    require(districts and len(identities) == len(districts)
            and len({norm(name) for name in identities.values()}) == len(districts),
            "Empty or duplicate registry district identities.")
    index, canonical = {}, set()
    for row in aliases:
        key, code, kind = norm(row["alias"]), row["district_id"], row["alias_kind"]
        require(key and key == row["normalized_alias"] and key not in index,
                f"Invalid or duplicate normalized alias: {key!r}")
        require(code in identities and row["district_name"] == identities[code]
                and kind in {"canonical", "declared_spelling_variant"},
                f"Invalid alias identity or kind: {key}")
        is_canonical = key == norm(identities[code])
        require(is_canonical == (kind == "canonical"), f"Incorrect alias kind: {key}")
        index[key] = row
        if is_canonical:
            canonical.add(code)
    require(canonical == set(identities), "Registry aliases do not cover all canonical districts.")
    return index


def resolve_rows(owners, index):
    require(owners and len({r["owner_id"] for r in owners}) == len(owners), "Empty or duplicate owner IDs.")
    result, audit = [], []
    groups = {name: set() for name in ("all_retained", "common_Car_profiles", *COHORTS)}
    for source in owners:
        owner, cohort = source["owner_id"], source["screened_cohort"]
        require(owner and source["data_origin"] == "SYNTHETIC" and cohort in COHORTS,
                "Unexpected owner ID, origin or cohort.")
        car = boolean(source["in_saved_Car_profiles"])
        require(car == (cohort == "common_energy_cases" and source["vehicle_class"] == "Car"),
                f"Inconsistent saved Car membership: {owner}")
        groups["all_retained"].add(owner)
        groups[cohort].add(owner)
        if car:
            groups["common_Car_profiles"].add(owner)
        row, matches = dict(source), {}
        for role, answer_field, id_field, status_field in (
            ("home", "home_district_answer", "home_district_id", "home_match_status"),
            ("outside", "outside_district_answer", "named_outside_district_id", "outside_match_status"),
        ):
            key = norm(source[answer_field])
            match = index.get(key)
            status = ("canonical_name_match" if match and match["alias_kind"] == "canonical" else
                      "declared_alias_match" if match else
                      "reported_not_applicable" if role == "outside" and key == "not applicable" else
                      "blank_answer" if not key else "unresolved_text_for_review")
            code = match["district_id"] if match else ""
            require(not source[id_field] or source[id_field] == code,
                    f"Existing district ID conflicts with registry: {owner}/{role}")
            row[id_field], row[status_field] = code, status
            row[f"{role}_canonical_district_name"] = match["district_name"] if match else ""
            row[f"{role}_alias_kind"] = match["alias_kind"] if match else ""
            audit.append({"owner_id": owner, "answer_role": role, "raw_answer": source[answer_field],
                "previous_district_id": source[id_field], "previous_status": source[status_field],
                "resolved_district_id": code, "resolved_status": status,
                "canonical_district_name": row[f"{role}_canonical_district_name"],
                "matched_declared_alias": match["alias"] if match else "",
                "alias_kind": row[f"{role}_alias_kind"]})
            matches[role] = match
        row["outside_answer_status"] = ("named_district" if matches["outside"] else row["outside_match_status"])
        row["named_outside_equals_home"] = (
            row["home_district_id"] == row["named_outside_district_id"]
            if matches["home"] and matches["outside"] else "")
        result.append(row)
    return result, audit, groups


def summaries(owners, groups):
    coverage, joint, pairs = [], [], []
    for group, ids in groups.items():
        rows = [row for row in owners if row["owner_id"] in ids]
        summary = {"population_group": group, "owners": len(rows),
            "home_district_matched": sum(bool(r["home_district_id"]) for r in rows),
            "home_declared_alias_matches": sum(r["home_match_status"] == "declared_alias_match" for r in rows),
            "named_outside_district_matched": sum(r["outside_answer_status"] == "named_district" for r in rows),
            "outside_declared_alias_matches": sum(r["outside_match_status"] == "declared_alias_match" for r in rows),
            **{status: sum(r["outside_answer_status"] == status for r in rows) for status in (
                "reported_not_applicable", "blank_answer", "unresolved_text_for_review")},
            "named_outside_equals_home": sum(r["named_outside_equals_home"] is True for r in rows)}
        require(sum(summary[k] for k in ("named_outside_district_matched", "reported_not_applicable",
                "blank_answer", "unresolved_text_for_review")) == len(ids), "Outside-answer accounting failed.")
        coverage.append(summary)
        counts = Counter((r["away_geography_answer"], r["outside_answer_status"]) for r in rows)
        require(sum(counts.values()) == len(ids), "Joint answer denominator mismatch.")
        for (answer, status), count in sorted(counts.items()):
            joint.append({"population_group": group, "away_geography_answer": answer,
                "outside_answer_status": status, "owners": count, "group_denominator": len(ids)})
        counts = Counter((r["home_district_id"], r["named_outside_district_id"]) for r in rows
                         if r["home_district_id"] and r["named_outside_district_id"])
        for (home, outside), count in sorted(counts.items()):
            pairs.append({"population_group": group, "home_district_id": home,
                "reported_most_common_outside_district_id": outside, "reporting_owners": count,
                "group_denominator": len(ids), "statistic_basis": "owner_reports_not_session_flows"})
        require(sum(counts.values()) == sum(bool(r["home_district_id"] and r["named_outside_district_id"])
                for r in rows), "Reported district-pair accounting failed.")
    return coverage, joint, pairs


def no_away_checks(owners, contexts):
    context_index = {(r["owner_id"], r["context"]): r for r in contexts}
    require(len(context_index) == len(contexts) and set(context_index) == {
        (r["owner_id"], code) for r in owners for code in original.CONTEXT_LABELS},
        "Expected the unchanged four-context owner grid.")
    result = []
    for owner in owners:
        if norm(owner["away_geography_answer"]) != NO_AWAY:
            continue
        for code in ("T1", "T3", "T4"):
            row = context_index[owner["owner_id"], code]
            bounds = [row[k] for k in ("context_frequency_min", "context_frequency_max")]
            parsed = [float(v) if text(v) else None for v in bounds]
            require(all(v is None or (math.isfinite(v) and v >= 0) for v in parsed),
                    f"Invalid context frequency bounds: {owner['owner_id']}/{code}")
            lo, hi = parsed
            require(lo is None or hi is None or lo <= hi, "Reversed context frequency bounds.")
            status = ("reported_zero" if lo == 0 and hi == 0 else "positive_report_for_review"
                      if lo is not None and lo > 0 else "positive_possible_for_review"
                      if hi is not None and hi > 0 else "not_evaluable")
            result.append({"owner_id": owner["owner_id"], "screened_cohort": owner["screened_cohort"],
                "in_saved_Car_profiles": owner["in_saved_Car_profiles"], "context": code,
                "frequency_answer": row["frequency_answer"], "context_frequency_min": bounds[0],
                "context_frequency_max": bounds[1], "context_frequency_state": row["context_frequency_state"],
                "check_status": status})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--output-root", default="data/processed/synthetic")
    args = parser.parse_args()
    watched = sources.Sources()
    for path in (__file__, original.__file__, sources.__file__):
        watched.watch(Path(path).resolve())
    folder, registry_folder = map(resolve, (args.evidence, args.registry))
    print("Verifying saved geography sources and declared district aliases...")
    report = watched.package(folder, "charging_geography_report.json")
    require(report.get("schema_version") == "charging_geography_evidence_v1"
            and report.get("status") == "evidence_prepared" and report.get("data_origin") == "SYNTHETIC"
            and report.get("checks") and all(v is True for v in report["checks"].values()),
            "Unsupported source geography evidence package.")
    owner_path = folder / "owner_geography_evidence.csv"
    require(owner_path.name in report["output_sha256"], "Owner geography table is not hash-bound.")
    owners = table(owner_path, ("owner_id", "data_origin", "vehicle_class", "screened_cohort", "in_saved_Car_profiles",
        *original.OWNER_QUESTIONS, "home_district_id", "home_match_status", "named_outside_district_id",
        "outside_match_status", "named_outside_equals_home"))
    require(len(owners) == report["owners_retained"], "Retained owner denominator mismatch.")
    registry_path, aliases_path = registry_folder / "district_registry.csv", registry_folder / "district_aliases.csv"
    for path in (registry_path, aliases_path, registry_folder / "district_registry_report.json"):
        watched.watch(path)
    districts = table(registry_path, ("district_id", "district_name"))
    aliases = table(aliases_path, ("alias", "normalized_alias", "district_id", "district_name", "alias_kind"))
    index = alias_index(districts, aliases)
    require(len(districts) == 25, "Expected the existing 25-district registry.")
    index_paths = [resolve(name) for name in report["input_sha256"] if Path(name).name == "district_profile_index.csv"]
    require(len(index_paths) == 1, "Missing or ambiguous hash-bound profile district index.")
    expected = {(r["district_id"], r["district_name"]) for r in table(index_paths[0], ("district_id", "district_name"))}
    require(expected == {(r["district_id"], r["district_name"]) for r in districts},
            "Registry differs from saved profile district identities.")
    context_path = resolve(report["context_feature_file"])
    require(rel(context_path) in report["input_sha256"], "Context feature file is not hash-bound.")
    contexts = table(context_path, ("owner_id", "context", "frequency_answer", "context_frequency_min",
                                   "context_frequency_max", "context_frequency_state"))
    require(len(contexts) == report["context_records"], "Context record denominator mismatch.")
    resolved, audit, groups = resolve_rows(owners, index)
    require(len(groups["common_Car_profiles"]) == report["profile_Car_owners"], "Saved Car cohort denominator changed.")
    coverage, joint, pairs = summaries(resolved, groups)
    home_only = no_away_checks(resolved, contexts)
    watched.unchanged()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    output = resolve(args.output_root) / f"charging_geography_resolved_{stamp}"
    output.mkdir(parents=True, exist_ok=False)
    exports = {"owner_geography_resolved.csv": (resolved, None),
        "district_name_resolution_audit.csv": (audit, None), "geography_coverage_summary.csv": (coverage, None),
        "geography_joint_answer_counts.csv": (joint, None),
        "reported_district_pairs.csv": (pairs, ("population_group", "home_district_id",
            "reported_most_common_outside_district_id", "reporting_owners", "group_denominator", "statistic_basis")),
        "home_only_context_checks.csv": (home_only, ("owner_id", "screened_cohort", "in_saved_Car_profiles", "context",
            "frequency_answer", "context_frequency_min", "context_frequency_max", "context_frequency_state", "check_status"))}
    for name, (rows, fields) in exports.items():
        with (output / name).open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields or list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    (output / "README.md").write_text(
        "# Resolved synthetic charging-geography evidence\n\n"
        "Raw answers and all owner/cohort memberships are preserved. Only exact canonical names and existing declared aliases "
        "are resolved, using case and whitespace normalization. No fuzzy matching or new aliases are introduced. "
        "The registry files are recorded as input snapshots and canonical identities checked against the hash-bound profile index; "
        "the registry report is retained as a source reference, without assuming its provenance schema.\n\n"
        "Not applicable, blank and unresolved text are separate answer states. A non-district answer does not identify a zero "
        "probability of outside charging. Equal home/outside districts remain visible review flags. "
        "The no-away check compares Q20 with reported non-home frequency bounds; it changes no eligibility.\n\n"
        "Groups overlap and must not be added. District-pair tables count owners reporting a most-common district, "
        "not sessions or energy flows. Q20/Q21 do not establish per-context routing probabilities. "
        "T1/T2/T3/T4 remain verified survey contexts, without automatic geographic place-typology mappings.\n\n"
        "This step performs no imputation, new parameter draws, demand simulations, fleet activation or empirical validation.\n",
        encoding="utf-8")
    flags = {"unmatched_home_owners": sum(not r["home_district_id"] for r in resolved),
        "unresolved_outside_text_owners": sum(r["outside_answer_status"] == "unresolved_text_for_review" for r in resolved),
        "blank_outside_answer_owners": sum(r["outside_answer_status"] == "blank_answer" for r in resolved),
        "named_outside_equals_home_owners": sum(r["named_outside_equals_home"] is True for r in resolved),
        "no_away_positive_frequency_owners": len({r["owner_id"] for r in home_only if r["check_status"] == "positive_report_for_review"}),
        "no_away_possible_positive_frequency_owners": len({r["owner_id"] for r in home_only if r["check_status"] == "positive_possible_for_review"}),
        "no_away_unevaluable_frequency_owners": len({r["owner_id"] for r in home_only if r["check_status"] == "not_evaluable"})}
    watched.unchanged()
    result = {"schema_version": "charging_geography_resolved_v1",
        "status": "resolved_with_review_flags" if any(flags.values()) else "resolved",
        "data_origin": "SYNTHETIC", "source_evidence_folder": rel(folder), "owners_retained": len(resolved),
        "profile_Car_owners": len(groups["common_Car_profiles"]), "context_records": len(contexts),
        "population_groups": coverage, "review_flags": flags,
        "no_away_context_check_counts": dict(Counter(r["check_status"] for r in home_only)),
        "checks": {"source_package_hashes_verified": True, "declared_aliases_unambiguous": True,
            "canonical_profile_identities_match": True, "raw_answers_and_cohorts_preserved": True,
            "complete_context_grid": True, "answer_denominators_checked": True, "source_files_unchanged": True},
        "context_specific_outside_shares_identified": False, "session_origin_destination_matrix_estimated": False,
        "geographic_place_mapping_activated": False, "eligibility_changed": False,
        "new_demand_simulations": 0, "new_parameter_draws": 0, "empirical_validation": False,
        "input_sha256": {rel(p): value for p, value in sorted(watched.hashes.items(), key=lambda item: str(item[0]))},
        "output_sha256": {p.name: sha(p) for p in sorted(output.iterdir()) if p.is_file()}}
    (output / "charging_geography_resolved_report.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print("Declared-alias charging-geography resolution complete.")
    for row in coverage:
        print(f"{row['population_group']}: owners={row['owners']}, home_matched={row['home_district_matched']}, "
              f"home_aliases={row['home_declared_alias_matches']}, named_outside={row['named_outside_district_matched']}, "
              f"outside_aliases={row['outside_declared_alias_matches']}, not_applicable={row['reported_not_applicable']}")
    print(f"Review flags retained: {flags}")
    car_checks = Counter(r["check_status"] for r in home_only if boolean(r["in_saved_Car_profiles"]))
    print(f"Saved Car no-away context checks: {dict(car_checks)}")
    print("Source hashes, declared aliases, owner membership and answer accounting checked.")
    print(f"Output: {rel(output)}")
    print("Evidence resolution only; no routing probabilities, eligibility changes or new demand simulations.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError, csv.Error) as error:
        raise SystemExit(f"Charging geography resolution failed: {error}") from error
