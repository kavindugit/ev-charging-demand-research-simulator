"""Build a bounded synthetic test pool; preserve unresolved owners for review."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
CONTEXTS = {"T1", "T2", "T3", "T4"}

POLICY = {
    "purpose": (
        "Synthetic engineering test only; not an empirical "
        "or nationally representative forecast"
    ),
    "days_per_month": 365.25 / 12,
    "within_bin_sampling": (
        "Continuous uniform over declared feasible ranges; "
        "a development assumption, not a fitted distribution"
    ),
    "weekly_rate_meaning": (
        "Expected sessions per week; future Poisson counts may lie "
        "outside the usual-frequency category"
    ),
    "frequency_reconciliation": (
        "Intersect total weekly bounds with summed context bounds. "
        "For each sampled total, allocate rates between context bounds "
        "in proportion to their available widths"
    ),
    "context_open_upper": (
        "Bound a context's weekly rate by the owner's finite "
        "total upper bound; otherwise defer the owner"
    ),
    "start_sampling": (
        "Uniform inside the reported charging-start window; "
        "no additional TOU shift"
    ),
    "energy_sampling": (
        "Use the owner's usual energy range as shared session evidence "
        "across active contexts; it is not context-specific energy evidence"
    ),
    "nonhome_test_average_power_kw": {
        "SOCKET_PORTABLE": 2.0,
        "AC": 7.0,
        "DC": 50.0,
    },
    "nonhome_power_status": (
        "Chosen constant average powers for exercising the simulator, "
        "not measured powers or station ratings"
    ),
    "home_power": (
        "Use bounded reported home-power ranges; "
        "unresolved ranges are deferred"
    ),
    "electrical_boundary": (
        "For the test only, treat reported delivered energy as "
        "charging-load energy; conversion losses and auxiliary "
        "loads are not modelled"
    ),
    "missing_policy": (
        "No imputation of unknown energy, open total-frequency bins, "
        "unknown or mixed active charger types"
    ),
    "deferred_effects": [
        "Urgent-trip event rates and energy adjustments",
        "Queues, station failures and location-switch probabilities",
    ],
    "no_national_scaling": True,
}


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def number(value):
    if value == "":
        return None

    result = float(value)

    if not math.isfinite(result):
        raise ValueError("nonfinite_numeric_value")

    return result


def bounded(row, prefix, positive_upper=True):
    low = number(row[prefix + "_min"])
    high = number(row[prefix + "_max"])

    if low is None or high is None:
        raise ValueError(prefix + "_unresolved_bounds")

    if (
        low < 0
        or high < low
        or (positive_upper and high <= 0)
    ):
        raise ValueError(prefix + "_invalid_bounds")

    return low, high


def make_profile(owner, context_rows):
    total_low, total_high = bounded(owner, "sessions_per_week")
    energy_low, energy_high = bounded(owner, "usual_energy_kwh")

    access = owner["home_access_answer"]

    if access not in {
        "Yes, I can charge at home",
        "No, I cannot charge at home",
        "Sometimes / limited access",
    }:
        raise ValueError("home_access_unrecognised")

    active = []
    inactive = []

    for row in context_rows:
        code = row["context"]

        if row["nonuse_conflict"] not in {"False", "false", "0"}:
            raise ValueError(code + "_context_nonuse_conflict")

        if row["context_frequency_state"] == "reported_zero":
            low, high = bounded(
                row,
                "context_frequency",
                positive_upper=False,
            )

            if low != 0 or high != 0:
                raise ValueError(code + "_invalid_zero_frequency")

            inactive.append(code)
            continue

        low = number(row["context_frequency_min"])
        high = number(row["context_frequency_max"])
        period = row["context_frequency_period"]

        if (
            low is None
            or low < 0
            or period not in {"week", "month"}
        ):
            raise ValueError(code + "_context_frequency_unresolved")

        factor = (
            1.0
            if period == "week"
            else 7 / POLICY["days_per_month"]
        )

        low *= factor
        high = (
            total_high
            if high is None
            else min(high * factor, total_high)
        )

        if high < low or high <= 0:
            raise ValueError(
                code + "_context_frequency_conflicts_with_total"
            )

        if (
            code == "T2"
            and access == "No, I cannot charge at home"
        ):
            raise ValueError(
                "home_access_conflicts_with_home_frequency"
            )

        if row["window_state"] != "reported_window":
            raise ValueError(code + "_start_window_unresolved")

        start, end, length = [
            number(row[key])
            for key in (
                "window_start_hour",
                "window_end_hour",
                "window_length_hours",
            )
        ]

        if (
            any(value is None for value in (start, end, length))
            or not (
                0 <= start < 24
                and 0 <= end < 24
                and 0 < length < 24
            )
            or not math.isclose((end - start) % 24, length)
        ):
            raise ValueError(code + "_invalid_start_window")

        charger = row["charger_code"]

        if charger not in POLICY["nonhome_test_average_power_kw"]:
            raise ValueError(code + "_charger_type_unresolved")

        if code == "T2":
            power_low, power_high = bounded(owner, "home_power_kw")

            if (
                power_low <= 0
                or charger not in {"AC", "SOCKET_PORTABLE"}
            ):
                raise ValueError("home_charger_or_power_conflict")

            equipment = owner["home_equipment_answer"]

            if (
                (
                    charger == "SOCKET_PORTABLE"
                    and not equipment.startswith("Standard socket")
                )
                or (
                    charger == "AC"
                    and not equipment.startswith("Dedicated AC")
                )
            ):
                raise ValueError("home_equipment_type_conflict")

            power_source = "reported_approximate_home_range"

        else:
            power_low = power_high = (
                POLICY["nonhome_test_average_power_kw"][charger]
            )
            power_source = "declared_nonhome_test_setting"

        active.append({
            "context": code,
            "frequency_answer": row["frequency_answer"],
            "weekly_rate_min": low,
            "weekly_rate_max": high,
            "window_start_hour": start,
            "window_end_hour": end,
            "window_length_hours": length,
            "charger_code": charger,
            "power_kw_min": power_low,
            "power_kw_max": power_high,
            "power_source": power_source,
        })

    if not active:
        raise ValueError("no_active_contexts")

    feasible_low = max(
        total_low,
        sum(row["weekly_rate_min"] for row in active),
    )
    feasible_high = min(
        total_high,
        sum(row["weekly_rate_max"] for row in active),
    )

    if feasible_high < feasible_low or feasible_high <= 0:
        raise ValueError(
            "total_and_context_frequencies_do_not_overlap"
        )

    return {
        "owner_id": owner["owner_id"],
        "data_origin": "SYNTHETIC",
        "home_district": owner["home_district_answer"],
        "district_interpretation": (
            "Owner residence only; not an observed charging-session district"
        ),
        "source_file": owner["source_file"],
        "source_sha256": owner["source_sha256"],
        "source_row": owner["source_row"],
        "vehicle_class_answer": owner["vehicle_class_answer"],
        "weekly_total_reported_min": total_low,
        "weekly_total_reported_max": total_high,
        "weekly_total_feasible_min": feasible_low,
        "weekly_total_feasible_max": feasible_high,
        "session_energy_kwh_min": energy_low,
        "session_energy_kwh_max": energy_high,
        "energy_boundary": owner["usual_energy_kwh_boundary"],
        "energy_answer": owner["usual_energy_answer"],
        "active_contexts": active,
        "inactive_contexts": sorted(inactive),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", required=True)
    parser.add_argument(
        "--rules",
        required=True,
        help="Folder containing behaviour_rules.json",
    )
    args = parser.parse_args()

    allowed = (ROOT / "data/processed/synthetic").resolve()

    features, rules_folder = [
        (ROOT / value).resolve()
        for value in (args.features, args.rules)
    ]

    if any(
        not folder.is_relative_to(allowed)
        for folder in (features, rules_folder)
    ):
        raise ValueError(
            "Inputs must be under data/processed/synthetic."
        )

    paths = {
        name: features / name
        for name in (
            "owners_features.csv",
            "owner_contexts_features.csv",
            "recent_sessions_features.csv",
            "feature_report.json",
        )
    }
    paths["behaviour_rules.json"] = (
        rules_folder / "behaviour_rules.json"
    )

    hashes = {
        name: sha256(path)
        for name, path in paths.items()
    }

    report = json.loads(
        paths["feature_report.json"].read_text(encoding="utf-8")
    )
    rules = json.loads(
        paths["behaviour_rules.json"].read_text(encoding="utf-8")
    )

    if (
        report.get("data_origin") != "SYNTHETIC"
        or rules.get("data_origin") != "SYNTHETIC"
    ):
        raise ValueError("Expected synthetic inputs.")

    required_rules = {"R01", "R02", "R03", "R04", "R05", "R06"}

    available_rules = {
        rule["rule_id"]
        for rule in rules["rules"]
        if rule["status"] == "design_constraint"
    }

    if not required_rules.issubset(available_rules):
        raise ValueError(
            "Required design constraints are missing."
        )

    owners = read_csv(paths["owners_features.csv"])
    contexts = read_csv(paths["owner_contexts_features.csv"])
    recent = read_csv(paths["recent_sessions_features.csv"])

    for name, rows in (
        ("owners", owners),
        ("owner_contexts", contexts),
        ("recent_sessions", recent),
    ):
        if len(rows) != report["counts"][name]:
            raise ValueError(
                "Feature counts differ from the report."
            )

        if any(
            row["data_origin"] != "SYNTHETIC"
            or row["source_sha256"] != report["source_sha256"]
            for row in rows
        ):
            raise ValueError(
                "Feature provenance differs from the report."
            )

    owner_ids = {
        row["owner_id"]
        for row in owners
    }

    if len(owner_ids) != len(owners):
        raise ValueError("Duplicate owner IDs.")

    grouped = defaultdict(list)

    for row in contexts:
        if row["owner_id"] not in owner_ids:
            raise ValueError(
                "Context has no matching owner."
            )

        grouped[row["owner_id"]].append(row)

    if any(
        len(grouped[owner_id]) != 4
        or {
            row["context"]
            for row in grouped[owner_id]
        } != CONTEXTS
        for owner_id in owner_ids
    ):
        raise ValueError(
            "Every owner must have exactly four context records."
        )

    if (
        len({row["owner_id"] for row in recent}) != len(recent)
        or any(
            row["owner_id"] not in owner_ids
            for row in recent
        )
    ):
        raise ValueError(
            "Recent-session owner relationships are invalid."
        )

    profiles = []
    statuses = []
    reasons = Counter()

    for owner in owners:
        try:
            profiles.append(
                make_profile(
                    owner,
                    grouped[owner["owner_id"]],
                )
            )
            status = "ready_for_bounded_test"
            reason = ""

        except ValueError as error:
            status = "deferred_for_review"
            reason = str(error)
            reasons[reason] += 1

        statuses.append({
            "owner_id": owner["owner_id"],
            "data_origin": "SYNTHETIC",
            "status": status,
            "first_deferral_reason": reason,
        })

    if not profiles:
        raise ValueError(
            "No bounded profiles available; review the "
            "deferral policy before simulation."
        )

    if any(
        sha256(paths[name]) != value
        for name, value in hashes.items()
    ):
        raise ValueError(
            "Input changed during processing; rerun."
        )

    stamp = datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%S_%fZ"
    )
    output = allowed / f"parameters_{stamp}"
    output.mkdir(parents=True, exist_ok=False)

    summary = {
        "owners_retained_in_audit": len(owners),
        "owners_ready_for_bounded_test": len(profiles),
        "owners_deferred": len(owners) - len(profiles),
        "first_deferral_reasons": dict(reasons),
        "ready_context_counts": dict(Counter(
            row["context"]
            for profile in profiles
            for row in profile["active_contexts"]
        )),
        "recent_sessions_retained_upstream": len(recent),
        "recent_sessions_used_for_fitting": 0,
        "warning": (
            "The selected pool is a biased completeness subset. "
            "Do not extrapolate its composition to Sri Lankan EV owners."
        ),
    }

    package = {
        "schema_version": "bounded_test_parameters_v1",
        "data_origin": "SYNTHETIC",
        "policy": POLICY,
        "owner_profiles": profiles,
        "summary": summary,
        "input_sha256": hashes,
        "behaviour_constraints": rules["rules"],
        "ready_for_synthetic_engine_test": True,
        "empirically_calibrated": False,
        "district_demand_ready": False,
        "national_forecast_ready": False,
    }

    (
        output / "simulator_parameters.json"
    ).write_text(
        json.dumps(
            package,
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )

    with (
        output / "owner_readiness.csv"
    ).open(
        "w",
        encoding="utf-8-sig",
        newline="",
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "owner_id",
                "data_origin",
                "status",
                "first_deferral_reason",
            ],
        )
        writer.writeheader()
        writer.writerows(statuses)

    print("Simulator parameter package complete.")

    for key in (
        "owners_retained_in_audit",
        "owners_ready_for_bounded_test",
        "owners_deferred",
    ):
        print(f"{key}: {summary[key]}")

    print(
        f"ready_context_counts: {summary['ready_context_counts']}"
    )
    print(f"Output: {output.relative_to(ROOT)}")
    print(
        "Unresolved owners retained for review; "
        "test assumptions recorded; raw data unchanged."
    )


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(
            f"Parameter preparation failed: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1)