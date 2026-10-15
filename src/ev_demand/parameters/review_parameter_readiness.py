"""Review available readiness blockers without changing owner eligibility."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
from ev_demand.parameters import build_simulator_parameters as builder


def require(condition, message):
    if not condition:
        raise ValueError(message)


def action(reason):
    if "energy" in reason:
        return "Review usual-energy answer and encoding; unknown or open bounds need evidence or a separate assumption scenario."
    if "charger" in reason or "equipment" in reason:
        return "Review charger type and equipment answer; mixed or unknown types need clarification or an explicit mixture scenario."
    if "home_power" in reason:
        return "Review reported home power and equipment; public charger ratings cannot supply missing home power."
    if "do_not_overlap" in reason:
        return "Review total versus context frequency answers and unit conversion; do not silently force agreement."
    if "frequency" in reason or "sessions_per_week" in reason:
        return "Review frequency answer, units and bounds; open upper bounds need clarification or a separate bounded scenario."
    return "Review the source answer and encoding; retain the existing eligibility decision."


def inspect_owner(owner, contexts):
    checks, rates = [], []

    def emit(key, status, reason="", context=""):
        checks.append(dict(
            owner_id=owner["owner_id"],
            context=context,
            check=key,
            status=status,
            reason=reason,
            next_action=action(reason) if status == "issue" else "",
        ))

    def bounds(prefix):
        try:
            result = builder.bounded(owner, prefix)
        except ValueError as error:
            emit(prefix, "issue", str(error))
            return None
        emit(prefix, "pass")
        return result

    total = bounds("sessions_per_week")
    bounds("usual_energy_kwh")
    access = owner["home_access_answer"]

    emit(
        "home_access",
        "pass" if access in {
            "Yes, I can charge at home",
            "No, I cannot charge at home",
            "Sometimes / limited access",
        } else "issue",
        "" if access in {
            "Yes, I can charge at home",
            "No, I cannot charge at home",
            "Sometimes / limited access",
        } else "home_access_unrecognised",
    )

    active_count, frequencies_complete = 0, True

    for row in contexts:
        code = row["context"]

        emit(
            "nonuse_consistency",
            "pass" if row["nonuse_conflict"] in {
                "False", "false", "0"
            } else "issue",
            "" if row["nonuse_conflict"] in {
                "False", "false", "0"
            } else code + "_context_nonuse_conflict",
            code,
        )

        if row["context_frequency_state"] == "reported_zero":
            try:
                low, high = builder.bounded(
                    row, "context_frequency", positive_upper=False
                )
                if low != 0 or high != 0:
                    raise ValueError(code + "_invalid_zero_frequency")
                emit("zero_frequency", "pass", context=code)
            except ValueError as error:
                emit("zero_frequency", "issue", str(error), code)
                frequencies_complete = False
            continue

        active_count += 1

        try:
            low = builder.number(row["context_frequency_min"])
            high = builder.number(row["context_frequency_max"])
            period = row["context_frequency_period"]

            if low is None or low < 0 or period not in {"week", "month"}:
                raise ValueError(code + "_context_frequency_unresolved")

            factor = (
                1.0 if period == "week"
                else 7 / builder.POLICY["days_per_month"]
            )
            low *= factor

            if high is not None and (
                high * factor < low or high <= 0
            ):
                raise ValueError(
                    code + "_context_frequency_conflicts_with_total"
                )

            if total is None:
                frequencies_complete = False
                emit(
                    "context_frequency",
                    "not_evaluable",
                    "requires_valid_total_frequency",
                    code,
                )
            else:
                high = (
                    total[1] if high is None
                    else min(high * factor, total[1])
                )
                if high < low or high <= 0:
                    raise ValueError(
                        code + "_context_frequency_conflicts_with_total"
                    )
                rates.append((low, high))
                emit("context_frequency", "pass", context=code)

        except ValueError as error:
            frequencies_complete = False
            emit("context_frequency", "issue", str(error), code)

        if code == "T2":
            emit(
                "home_access_frequency",
                "issue" if access == "No, I cannot charge at home"
                else "pass" if access in {
                    "Yes, I can charge at home",
                    "Sometimes / limited access",
                } else "not_evaluable",
                "home_access_conflicts_with_home_frequency"
                if access == "No, I cannot charge at home" else "",
                code,
            )

        try:
            if row["window_state"] != "reported_window":
                raise ValueError(code + "_start_window_unresolved")

            start, end, length = [
                builder.number(row[k])
                for k in (
                    "window_start_hour",
                    "window_end_hour",
                    "window_length_hours",
                )
            ]

            if (
                any(v is None for v in (start, end, length))
                or not (
                    0 <= start < 24
                    and 0 <= end < 24
                    and 0 < length < 24
                )
                or not math.isclose((end - start) % 24, length)
            ):
                raise ValueError(code + "_invalid_start_window")

            emit("start_window", "pass", context=code)

        except ValueError as error:
            emit("start_window", "issue", str(error), code)

        charger = row["charger_code"]
        known = charger in builder.POLICY["nonhome_test_average_power_kw"]

        emit(
            "charger_type",
            "pass" if known else "issue",
            "" if known else code + "_charger_type_unresolved",
            code,
        )

        if code == "T2":
            power = bounds("home_power_kw")

            if power is None or not known:
                emit(
                    "home_charger_power",
                    "not_evaluable",
                    "requires_valid_home_power_and_charger",
                    code,
                )
            else:
                bad = (
                    power[0] <= 0
                    or charger not in {"AC", "SOCKET_PORTABLE"}
                )
                emit(
                    "home_charger_power",
                    "issue" if bad else "pass",
                    "home_charger_or_power_conflict" if bad else "",
                    code,
                )

            if charger in {"AC", "SOCKET_PORTABLE"}:
                equipment = owner["home_equipment_answer"]
                bad = (
                    (
                        charger == "SOCKET_PORTABLE"
                        and not equipment.startswith("Standard socket")
                    )
                    or (
                        charger == "AC"
                        and not equipment.startswith("Dedicated AC")
                    )
                )
                emit(
                    "home_equipment",
                    "issue" if bad else "pass",
                    "home_equipment_type_conflict" if bad else "",
                    code,
                )
            else:
                emit(
                    "home_equipment",
                    "not_evaluable",
                    "requires_resolved_home_charger_type",
                    code,
                )

    if not active_count:
        emit("active_contexts", "issue", "no_active_contexts")
    elif total is None or not frequencies_complete:
        emit(
            "frequency_intersection",
            "not_evaluable",
            "requires_valid_total_and_all_context_frequencies",
        )
    else:
        low = max(total[0], sum(r[0] for r in rates))
        high = min(total[1], sum(r[1] for r in rates))
        bad = high < low or high <= 0
        emit(
            "frequency_intersection",
            "issue" if bad else "pass",
            "total_and_context_frequencies_do_not_overlap" if bad else "",
        )

    return checks


def write_csv(path, rows, fields):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def review(parameters, features, root=ROOT):
    root = Path(root).resolve()
    allowed = (root / "data/processed/synthetic").resolve()
    parameters, features = Path(parameters).resolve(), Path(features).resolve()

    require(
        all(p.is_relative_to(allowed) for p in (parameters, features)),
        "Inputs must be under data/processed/synthetic.",
    )

    paths = {
        n: features / n
        for n in (
            "owners_features.csv",
            "owner_contexts_features.csv",
            "recent_sessions_features.csv",
            "feature_report.json",
        )
    }
    paths.update({
        n: parameters / n
        for n in ("simulator_parameters.json", "owner_readiness.csv")
    })
    paths["review_source"] = Path(__file__).resolve()
    paths["builder_source"] = Path(builder.__file__).resolve()

    hashes = {n: builder.sha256(p) for n, p in paths.items()}
    package = json.loads(
        paths["simulator_parameters.json"].read_text(encoding="utf-8-sig")
    )

    require(
        package.get("schema_version") == "bounded_test_parameters_v1"
        and package.get("data_origin") == "SYNTHETIC",
        "Use the original bounded synthetic parameter package.",
    )

    for name in (
        "owners_features.csv",
        "owner_contexts_features.csv",
        "recent_sessions_features.csv",
        "feature_report.json",
    ):
        require(
            hashes[name] == package["input_sha256"][name],
            "Feature input hash mismatch: " + name,
        )

    report = json.loads(
        paths["feature_report.json"].read_text(encoding="utf-8-sig")
    )
    require(report["data_origin"] == "SYNTHETIC", "Expected synthetic features.")

    owners = builder.read_csv(paths["owners_features.csv"])
    contexts = builder.read_csv(paths["owner_contexts_features.csv"])
    recent = builder.read_csv(paths["recent_sessions_features.csv"])

    for name, rows in (
        ("owners", owners),
        ("owner_contexts", contexts),
        ("recent_sessions", recent),
    ):
        require(
            len(rows) == report["counts"][name],
            "Feature count mismatch: " + name,
        )
        require(
            all(
                r["data_origin"] == "SYNTHETIC"
                and r["source_sha256"] == report["source_sha256"]
                for r in rows
            ),
            "Feature provenance mismatch.",
        )

    owner_ids = {r["owner_id"] for r in owners}
    require(len(owner_ids) == len(owners), "Duplicate owner IDs.")

    grouped = defaultdict(list)
    for row in contexts:
        require(row["owner_id"] in owner_ids, "Context has an unknown owner.")
        grouped[row["owner_id"]].append(row)

    require(
        all(
            len(grouped[i]) == 4
            and {r["context"] for r in grouped[i]} == builder.CONTEXTS
            for i in owner_ids
        ),
        "Each owner must have four distinct context records.",
    )
    require(
        len({r["owner_id"] for r in recent}) == len(recent)
        and all(r["owner_id"] in owner_ids for r in recent),
        "Invalid recent-session relationships.",
    )

    readiness_rows = builder.read_csv(paths["owner_readiness.csv"])
    readiness = {r["owner_id"]: r for r in readiness_rows}

    require(
        len(readiness) == len(readiness_rows)
        and set(readiness) == owner_ids
        and all(r["data_origin"] == "SYNTHETIC" for r in readiness_rows),
        "Invalid readiness owner coverage.",
    )

    profiles = {r["owner_id"]: r for r in package["owner_profiles"]}
    require(
        len(profiles) == len(package["owner_profiles"]),
        "Duplicate parameter profiles.",
    )

    details, owner_review = [], []
    first_counts, issue_counts = Counter(), Counter()
    replayed = set()

    for owner in owners:
        oid = owner["owner_id"]

        try:
            profile = builder.make_profile(owner, grouped[oid])
            status, reason = "ready_for_bounded_test", ""
            require(
                oid in profiles and profile == profiles[oid],
                "Rebuilt profile differs: " + oid,
            )
            replayed.add(oid)
        except ValueError as error:
            if oid in profiles:
                raise
            status, reason = "deferred_for_review", str(error)

        require(
            (status, reason)
            == (
                readiness[oid]["status"],
                readiness[oid]["first_deferral_reason"],
            ),
            "Readiness replay differs: " + oid,
        )

        checks = inspect_owner(owner, grouped[oid])
        issues = sorted({
            r["reason"] for r in checks if r["status"] == "issue"
        })

        require(
            (not issues) if not reason else reason in issues,
            "Independent checks disagree with first deferral: " + oid,
        )

        if reason:
            first_counts[reason] += 1

        issue_counts.update(issues)
        details.extend(checks)
        owner_review.append(dict(
            owner_id=oid,
            data_origin="SYNTHETIC",
            original_status=status,
            first_deferral_reason=reason,
            detected_issue_count=len(issues),
            detected_issues=json.dumps(issues),
            not_evaluable_checks=sum(
                r["status"] == "not_evaluable" for r in checks
            ),
        ))

    summary = package["summary"]
    require(
        replayed == set(profiles)
        and len(owners) == summary["owners_retained_in_audit"]
        and len(replayed) == summary["owners_ready_for_bounded_test"]
        and len(owners) - len(replayed) == summary["owners_deferred"]
        and dict(first_counts) == summary["first_deferral_reasons"],
        "Original summary does not reconcile.",
    )

    require(
        all(builder.sha256(paths[n]) == h for n, h in hashes.items()),
        "Input or source changed during review.",
    )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    output = allowed / ("readiness_review_" + stamp)
    output.mkdir(parents=True, exist_ok=False)

    write_csv(
        output / "owner_readiness_review.csv",
        owner_review,
        tuple(owner_review[0]),
    )
    write_csv(
        output / "readiness_checks.csv",
        details,
        tuple(details[0]),
    )

    reasons = [
        dict(
            reason=r,
            owners_with_detected_issue=n,
            owners_with_first_deferral=first_counts[r],
            next_action=action(r),
        )
        for r, n in sorted(
            issue_counts.items(),
            key=lambda item: (-item[1], item[0]),
        )
    ]
    write_csv(
        output / "issue_summary.csv",
        reasons,
        (
            "reason",
            "owners_with_detected_issue",
            "owners_with_first_deferral",
            "next_action",
        ),
    )

    result = dict(
        schema_version="readiness_review_v1",
        data_origin="SYNTHETIC",
        status="complete",
        owners_reviewed=len(owners),
        owners_ready=len(replayed),
        owners_deferred=len(owners) - len(replayed),
        owners_with_multiple_detected_issues=sum(
            r["detected_issue_count"] > 1 for r in owner_review
        ),
        not_evaluable_checks=sum(
            r["status"] == "not_evaluable" for r in details
        ),
        first_deferral_counts=dict(first_counts),
        detected_issue_owner_counts=dict(issue_counts),
        input_and_source_sha256=hashes,
        input_and_source_paths={n: str(p) for n, p in paths.items()},
        original_readiness_reproduced=True,
        owner_eligibility_changed=False,
        empirical_validation=False,
        limitations=[
            "Only checks on available encoded fields; conditional checks may remain not_evaluable.",
            "Issue categories overlap; their owner counts must not be summed as distinct owners.",
            "Behaviour rules and raw survey answers are not independently re-audited here.",
            "No imputation, parameter fitting or owner promotion was performed.",
        ],
        output_sha256={
            p.name: builder.sha256(p)
            for p in sorted(output.glob("*.csv"))
        },
    )

    require(
        all(builder.sha256(paths[n]) == h for n, h in hashes.items()),
        "Input or source changed during export.",
    )
    (output / "readiness_review_report.json").write_text(
        json.dumps(result, indent=2) + "\n",
        encoding="utf-8",
    )
    return output, result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parameters", required=True)
    parser.add_argument("--features", required=True)
    args = parser.parse_args()

    output, report = review(
        ROOT / args.parameters,
        ROOT / args.features,
    )

    print("Synthetic parameter-readiness review complete.")
    for key in (
        "owners_reviewed",
        "owners_ready",
        "owners_deferred",
        "owners_with_multiple_detected_issues",
        "not_evaluable_checks",
    ):
        print(f"{key}: {report[key]}")

    print("Detected issues by affected owner (categories may overlap):")
    for reason, count in sorted(
        report["detected_issue_owner_counts"].items(),
        key=lambda item: (-item[1], item[0]),
    ):
        print(f"{reason}: {count}")

    print("Original readiness and ready profiles reproduced; source hashes unchanged.")
    print(f"Output: {output.relative_to(ROOT)}")
    print("Review only; no owner eligibility or parameters changed; no empirical validation.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(f"Readiness review failed: {error}", file=sys.stderr)
        raise SystemExit(1)