"""Build and exercise coupled owner/context weekly-rate constraints."""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import random
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
from ev_demand.parameters import build_simulator_parameters as baseline
from ev_demand.parameters.build_population_evidence import (
    CONTEXTS, interval_kind, require, write_csv,
)

MONTH_TO_WEEK = 7 / (365.25 / 12)
INFORMATIVE = {"finite_interval", "point", "lower_bound_only", "upper_bound_only"}
DRAW_FIELDS = ("owner_id", "test_draw", "total_weekly_rate", "T1_weekly_rate",
               "T2_weekly_rate", "T3_weekly_rate", "T4_weekly_rate", "sum_error")


def finite_or_none(value):
    return value if math.isfinite(value) else None


def rate_bounds(row, is_total=False):
    period = row["reported_period"]
    require(row["unit"] == "sessions" and period in ({"week"} if is_total else {"week", "month"}),
            "Unsupported frequency unit or period.")
    kind = interval_kind(row["raw_min"], row["raw_max"], row["raw_state"], "sessions")
    if row["evidence_kind"] == "conflict_review":
        return None
    require(kind == row["evidence_kind"], "Frequency classification did not reproduce.")
    if kind not in INFORMATIVE | {"unresolved"}:
        return None
    if not is_total and row["raw_state"] == "reported_zero":
        if kind != "point" or float(row["raw_min"]) != 0:
            return None
    factor = 1.0 if period == "week" else MONTH_TO_WEEK
    low = float(row["raw_min"]) * factor if row["raw_min"].strip() else 0.0
    high = float(row["raw_max"]) * factor if row["raw_max"].strip() else math.inf
    return low, high


def endpoint_membership(row, value):
    """Return True/False/None for included/excluded/unreviewed source membership."""
    factor = 1.0 if row["reported_period"] == "week" else MONTH_TO_WEEK
    has_low, has_high = bool(row["raw_min"].strip()), bool(row["raw_max"].strip())
    low = float(row["raw_min"]) * factor if has_low else 0.0
    high = float(row["raw_max"]) * factor if has_high else math.inf
    if value < low or value > high:
        return False
    boundary = row["raw_boundary"]
    if row["raw_state"] == "reported_zero" and low == high == 0 and boundary in {"closed", "", "none"}:
        return value == 0
    flags = {
        "closed": (True, True), "open": (False, False),
        "left_closed": (True, None), "left_open": (False, None),
        "right_closed": (None, True), "right_open": (None, False),
    }.get(boundary, (None, None))
    decisions = []
    if has_low and value == low:
        decisions.append(flags[0])
    if has_high and value == high:
        decisions.append(flags[1])
    if any(v is False for v in decisions):
        return False
    return None if any(v is None for v in decisions) else True


def solve_owner(total, contexts):
    require(set(contexts) == set(CONTEXTS), "Incomplete owner context frequencies.")
    total_bounds = rate_bounds(total, True)
    bounds = {code: rate_bounds(row) for code, row in contexts.items()}
    result = dict(status="evidence_review", total_min=None, total_max=None,
                  context_bounds={}, sample_allowed=False, endpoint_checks=[])
    if total_bounds is None or any(value is None for value in bounds.values()):
        return result
    sum_low = math.fsum(value[0] for value in bounds.values())
    sum_high = sum(value[1] for value in bounds.values())
    low, high = max(total_bounds[0], sum_low), min(total_bounds[1], sum_high)
    result.update(total_min=low, total_max=finite_or_none(high))
    if low > high:
        result["status"] = "empty_closed_envelope"
        return result
    for code, (lower, upper) in bounds.items():
        other_low = math.fsum(value[0] for key, value in bounds.items() if key != code)
        other_high = sum(value[1] for key, value in bounds.items() if key != code)
        projected_low = max(lower, low - other_high)
        projected_high = min(upper, high - other_low)
        require(projected_low <= projected_high, "Inconsistent projected frequency bounds.")
        result["context_bounds"][code] = dict(
            min=projected_low, max=finite_or_none(projected_high),
            upper_tightened_by_joint_constraints=projected_high < upper)
    if high == 0:
        result["status"] = "zero_rate_only"
    elif not math.isfinite(high):
        result["status"] = "unbounded_joint_region"
    else:
        result["status"] = "bounded_joint_region"
    result["closed_envelope_status"] = result["status"]
    forced = [("TOTAL", total, low)] if low == high else []
    forced.extend((code, contexts[code], value["min"]) for code, value in result["context_bounds"].items()
                  if value["min"] == value["max"])
    for code, source, value in forced:
        membership = endpoint_membership(source, value)
        result["endpoint_checks"].append(dict(
            constraint=code, weekly_rate=value, raw_boundary=source["raw_boundary"],
            decision="included" if membership is True else "excluded" if membership is False else "unreviewed"))
    if any(r["decision"] == "excluded" for r in result["endpoint_checks"]):
        result["status"] = "empty_open_boundary_region"
    elif any(r["decision"] == "unreviewed" for r in result["endpoint_checks"]):
        result["status"] = "endpoint_review"
    result["sample_allowed"] = (result["status"] == "bounded_joint_region"
        and total["evidence_kind"] in INFORMATIVE
        and all(r["evidence_kind"] in INFORMATIVE for r in contexts.values()))
    return result


def sample_joint(model, rng):
    require(model["sample_allowed"], "Owner is not eligible for this parameter-sampling test.")
    low, high = model["total_min"], model["total_max"]

    def within(a, b):
        require(a <= b, "Empty conditional sampling interval.")
        if a == b:
            return a
        value = a + (b - a) * rng.random()
        value = max(math.nextafter(a, b), min(math.nextafter(b, a), value))
        require(a < value < b, "No representable interior value; review numerical bounds.")
        return value

    total = within(low, high)
    limits = {code: (row["min"], row["max"]) for code, row in model["context_bounds"].items()}
    values = {code: lo for code, (lo, hi) in limits.items() if lo == hi}
    pending = [code for code in CONTEXTS if code not in values]
    rng.shuffle(pending)
    tolerance = 1e-9 * max(1.0, total)
    for i, code in enumerate(pending):
        remaining = total - math.fsum(values.values())
        others = pending[i + 1:]
        lo, hi = limits[code]
        a = max(lo, remaining - math.fsum(limits[c][1] for c in others))
        b = min(hi, remaining - math.fsum(limits[c][0] for c in others))
        if a > b:
            require(a - b <= tolerance, "Sequential allocation lost feasibility.")
            value = min(hi, max(lo, (a + b) / 2))
        elif not others:
            require(lo - tolerance <= remaining <= hi + tolerance, "Final context is outside its bounds.")
            value = min(hi, max(lo, remaining))
        else:
            value = within(a, b)
        values[code] = value
    error = math.fsum(values.values()) - total
    require(set(values) == set(CONTEXTS) and abs(error) <= tolerance
            and all(math.isfinite(v) and limits[c][0] <= v <= limits[c][1] for c, v in values.items()),
            "A joint draw violated its bounds or total/context sum.")
    return total, values, error


def build_model(evidence_folder, draws_per_owner=100, seed=20261017, project_root=ROOT):
    require(isinstance(draws_per_owner, int) and draws_per_owner > 0, "Use a positive draw count.")
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
                 "src/ev_demand/parameters/build_joint_frequency_model.py"):
        watch(name)
    allowed = (root / "data/processed/synthetic").resolve()
    folder = (root / evidence_folder).resolve()
    require(folder.is_relative_to(allowed) and folder.is_dir(), "Use the completed synthetic evidence folder.")
    spec = json.loads(watch(folder / "behaviour_population_spec.json").read_text(encoding="utf-8-sig"))
    require(spec.get("schema_version") == "behaviour_population_evidence_v1"
            and spec.get("status") == "evidence_prepared" and spec.get("data_origin") == "SYNTHETIC"
            and spec.get("context_labels") == CONTEXTS, "Unexpected population-evidence specification.")
    require(bool(spec.get("checks")) and all(v is True for v in spec["checks"].values()),
            "An upstream evidence check did not pass.")
    expected = {"numeric_evidence.csv", "numeric_coverage.csv", "context_windows.csv",
                "categorical_answer_counts.csv", "linked_owner_evidence.jsonl"}
    require(set(spec["output_sha256"]) == expected, "Unexpected evidence manifest.")
    for name in sorted(expected):
        path = watch(folder / name)
        require(watched[path] == spec["output_sha256"][name], "Evidence export hash mismatch: " + name)
    donors = [json.loads(line) for line in (folder / "linked_owner_evidence.jsonl").read_text(
        encoding="utf-8").splitlines() if line.strip()]
    ids = {r["owner_id"] for r in donors}
    require(bool(ids) and "" not in ids and len(ids) == len(donors) == spec["owners_retained"],
            "Invalid linked-owner accounting.")
    numeric = baseline.read_csv(folder / "numeric_evidence.csv")
    require(len(numeric) == spec["numeric_evidence_rows"], "Numeric evidence count mismatch.")
    rows = [r for r in numeric if r["parameter_id"] in {"sessions_per_week", "context_frequency"}]
    keys = [(r["owner_id"], r["legacy_context"]) for r in rows]
    require(len(keys) == len(set(keys)) and set(keys) == {(oid, c) for oid in ids for c in ("", *CONTEXTS)},
            "Incomplete or duplicate owner/frequency evidence.")
    indexed = dict(zip(keys, rows))
    owners, audit, context_audit, draws, statuses = [], [], [], [], Counter()
    for donor in sorted(donors, key=lambda r: r["owner_id"]):
        oid, owner, readiness = donor["owner_id"], donor["owner_features"], donor["original_readiness"]
        require(all(r["owner_id"] == oid and r["data_origin"] == "SYNTHETIC" for r in (donor, owner, readiness)),
                "Owner provenance mismatch.")
        sources = donor["context_features"]
        require(len(sources) == 4 and {r["context"] for r in sources} == set(CONTEXTS), "Incomplete context links.")
        for source, code, prefix, answer in [(owner, "", "sessions_per_week", owner["weekly_frequency_answer"])] + [
                (r, r["context"], "context_frequency", r["frequency_answer"]) for r in sources]:
            row = indexed[(oid, code)]
            require(source["owner_id"] == oid and source["data_origin"] == "SYNTHETIC"
                    and row["data_origin"] == "SYNTHETIC" and row["original_readiness"] == readiness["status"]
                    and row["parameter_id"] == prefix and row["charging_context"] == CONTEXTS.get(code, ""),
                    "Frequency provenance/context mismatch.")
            reproduced = dict(raw_min=source[prefix + "_min"], raw_max=source[prefix + "_max"],
                raw_state=source[prefix + "_state"], raw_boundary=source.get(prefix + "_boundary", ""), raw_answer=answer,
                reported_period=source["context_frequency_period"] if code else "week", unit="sessions")
            kind = interval_kind(reproduced["raw_min"], reproduced["raw_max"], reproduced["raw_state"], "sessions")
            if code:
                require(source["nonuse_conflict"] in {"False", "false", "0", "True", "true", "1"},
                        "Unrecognised nonuse-conflict flag.")
            if code and source["nonuse_conflict"] in {"True", "true", "1"}:
                kind = "conflict_review"
            reproduced["evidence_kind"] = kind
            require(all(row[k] == v for k, v in reproduced.items()), "Frequency source fields did not reproduce.")
        total = indexed[(oid, "")]
        contexts = {c: indexed[(oid, c)] for c in CONTEXTS}
        model = solve_owner(total, contexts)
        statuses[model["status"]] += 1
        owner_seed = int.from_bytes(hashlib.sha256(f"joint-rate-v1|{seed}|{oid}".encode()).digest()[:8], "big")
        rng = random.Random(owner_seed)
        if model["sample_allowed"]:
            for draw in range(1, draws_per_owner + 1):
                target, values, error = sample_joint(model, rng)
                require(endpoint_membership(total, target) is True
                        and all(endpoint_membership(contexts[c], values[c]) is True for c in CONTEXTS),
                        "A generated draw touched an excluded or unreviewed source endpoint.")
                draws.append(dict(owner_id=oid, test_draw=draw, total_weekly_rate=target,
                    **{c + "_weekly_rate": values[c] for c in CONTEXTS}, sum_error=error))
        owners.append(dict(owner_id=oid, original_readiness=readiness, source_total=total,
                           source_contexts=contexts, joint_region=model, parameter_test_seed=owner_seed))
        audit.append(dict(owner_id=oid, original_readiness=readiness["status"],
            total_evidence_kind=total["evidence_kind"], raw_total_answer=total["raw_answer"],
            raw_total_min=total["raw_min"], raw_total_max=total["raw_max"], raw_total_boundary=total["raw_boundary"],
            joint_status=model["status"], feasible_total_min=model["total_min"], feasible_total_max=model["total_max"],
            parameter_test_allowed=model["sample_allowed"], test_draws=draws_per_owner if model["sample_allowed"] else 0))
        for c, source in contexts.items():
            projection = model["context_bounds"].get(c, {})
            context_audit.append(dict(owner_id=oid, legacy_context=c, charging_context=CONTEXTS[c],
                raw_answer=source["raw_answer"], raw_min=source["raw_min"], raw_max=source["raw_max"],
                raw_boundary=source["raw_boundary"], reported_period=source["reported_period"],
                evidence_kind=source["evidence_kind"], joint_status=model["status"],
                projected_weekly_min=projection.get("min"), projected_weekly_max=projection.get("max"),
                upper_tightened_by_joint_constraints=projection.get("upper_tightened_by_joint_constraints", False)))
    require(len(context_audit) == spec["context_rows_retained"] == 4 * len(donors), "Context accounting failed.")
    require(sum(r["original_readiness"] == "ready_for_bounded_test" for r in audit) == spec["original_ready_owners"]
            and sum(r["original_readiness"] == "deferred_for_review" for r in audit) == spec["original_deferred_owners"],
            "Original readiness totals disagree.")
    require(all(baseline.sha256(p) == old for p, old in watched.items()), "An input changed during processing.")
    output = allowed / ("joint_frequency_model_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ"))
    output.mkdir(parents=True, exist_ok=False)
    write_csv(output / "frequency_owner_audit.csv", audit)
    write_csv(output / "frequency_context_constraints.csv", context_audit)
    with (output / "joint_frequency_test_draws.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=DRAW_FIELDS)
        writer.writeheader()
        writer.writerows(draws)
    report = dict(schema_version="joint_frequency_rate_model_v2", data_origin="SYNTHETIC",
        behaviour_input_origin="SYNTHETIC", output_origin="COMPUTED_PARAMETER_MODEL",
        status="parameter_sampling_test_complete", owners_retained=len(donors),
        owners_with_test_draws=sum(r["parameter_test_allowed"] for r in audit),
        owners_with_unresolved_reported_total=sum(r["total_evidence_kind"] == "unresolved" for r in audit),
        joint_status_counts=dict(statuses), draws_per_eligible_owner=draws_per_owner,
        parameter_test_draws=len(draws), base_seed=seed, owners=owners,
        assumptions=dict(
            FREQUENCY_RATE_INTERPRETATION_TEST_V1="Usual frequency ranges constrain expected completed sessions/week; not exact calendar-week counts.",
            CONTEXT_PARTITION_TEST_V1="Four charging contexts form a mutually exclusive, exhaustive partition; total rate equals their sum.",
            MONTH_CONVERSION_TEST_V1=f"Monthly usual rates multiply by {MONTH_TO_WEEK!r}, using 365.25/12 days/month.",
            ENCODED_BOUNDARY_INTERPRETATION_V2="Numeric envelopes use interval closures. Forced points and generated draws are checked against encoded endpoint membership; excluded or unreviewed points are not sampled.",
            JOINT_RATE_SAMPLING_TEST_V1="Uniform feasible total, then shuffled context order and sequential uniform feasible allocations. This is not uniform over the joint polytope or a fitted joint distribution."),
        limits=[
            "Missing numerical bounds use nonnegative-domain envelopes, never observed zeros. Raw missing answers are retained.",
            "Unresolved total/context reports are not sampled, even when other constraints imply finite envelopes.",
            "Infinite upper bounds are JSON null and CSV blank; they are not zero or an imposed tail cap.",
            "Projected context intervals remain coupled by the total equation; never sample their marginal bounds independently.",
            "These draws are parameter tests, not sessions, Monte Carlo demand replicates, virtual owners or national populations.",
            "The earlier Poisson/NB usual-week count candidates are a separate reporting interpretation and are not activated here.",
            "No owner eligibility, energy, charger, power, scheduling or archived simulation is changed.",
            "Scheduling must demonstrate realised completed-frequency agreement before this becomes a population simulator input."],
        sampler_implemented=True, simulator_activation=False, owner_eligibility_changed=False,
        population_generation_ready=False, district_demand_ready=False, empirical_validation=False,
        checks=dict(evidence_hashes_match=True, frequency_owner_context_links_reproduced=True,
            joint_bounds_and_draws_checked=True, context_totals_conserved=True,
            encoded_endpoints_checked=True, input_hashes_unchanged=True),
        input_sha256={p.relative_to(root).as_posix(): old for p, old in watched.items()},
        output_sha256={p.name: baseline.sha256(p) for p in sorted(output.iterdir())})
    require(all(baseline.sha256(p) == old for p, old in watched.items()), "An input changed during export.")
    (output / "joint_frequency_model.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    return output, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--draws-per-owner", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20261017)
    args = parser.parse_args()
    output, report = build_model(args.evidence, args.draws_per_owner, args.seed)
    print("Joint weekly-frequency parameter model complete.")
    for key in ("owners_retained", "owners_with_test_draws", "owners_with_unresolved_reported_total",
                "joint_status_counts", "draws_per_eligible_owner", "parameter_test_draws"):
        print(f"{key}: {report[key]}")
    print("Source-integrity and generated-draw checks passed; owner-level issues retained in status tables.")
    print(f"Output: {output.relative_to(ROOT)}")
    print("Rate-interpretation and allocation assumptions only; no demand simulations or eligibility changes.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"Joint frequency preparation failed: {exc}", file=sys.stderr)
        raise SystemExit(1)