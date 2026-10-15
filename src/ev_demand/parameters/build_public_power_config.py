"""Prepare explicit public/non-home power assumptions for a synthetic test."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
CONTEXTS = ["T1", "T3", "T4"]

# Editable engineering settings, not a measured Sri Lankan charger inventory.
RATINGS_KW = {
    "SOCKET_PORTABLE": [2.0],
    "AC": [7.0, 22.0],
    "DC": [20.0, 30.0, 40.0, 50.0, 90.0],
}
SCENARIOS = {
    "reduced_test": {
        "description": "Reduced average output for testing; not measured charging powers.",
        "average_to_rated_power_fraction": {
            "SOCKET_PORTABLE": [1.0, 1.0],
            "AC": [0.7, 0.95],
            "DC": [0.6, 0.9],
        },
    },
    "rated_ceiling_test": {
        "description": "Rated output throughout a session: a sensitivity setting, not an observed profile or a guaranteed upper bound on aggregate hourly peaks.",
        "average_to_rated_power_fraction": {
            "SOCKET_PORTABLE": [1.0, 1.0],
            "AC": [1.0, 1.0],
            "DC": [1.0, 1.0],
        },
    },
}


def positive_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Power settings must be numbers.")
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Power settings must be finite and positive.")
    return float(value)


def source_note(code, power):
    if code == "DC" and power in {20, 30, 40, 90}:
        return "User-reported charger example; unverified test option."
    if (code, power) in {("SOCKET_PORTABLE", 2), ("AC", 7), ("DC", 50)}:
        return "Previous engine-test power setting; not verified equipment."
    return "Additional engineering test option; not verified equipment."


def make_config():
    expected = {"SOCKET_PORTABLE", "AC", "DC"}
    if set(RATINGS_KW) != expected:
        raise ValueError("Provide socket/portable, AC and DC rating lists.")
    if (not CONTEXTS or len(CONTEXTS) != len(set(CONTEXTS))
            or not set(CONTEXTS).issubset({"T1", "T3", "T4"})):
        raise ValueError("Use unique non-home contexts T1, T3 or T4.")
    if not SCENARIOS:
        raise ValueError("At least one power scenario is required.")
    for name, scenario in SCENARIOS.items():
        fractions = scenario["average_to_rated_power_fraction"]
        if not scenario["description"] or set(fractions) != expected:
            raise ValueError(f"Incomplete scenario: {name}.")
        for bounds in fractions.values():
            if not isinstance(bounds, list) or len(bounds) != 2:
                raise ValueError("Each power fraction needs two bounds.")
            low, high = map(positive_number, bounds)
            if not low <= high <= 1:
                raise ValueError("Power fractions must satisfy 0 < low <= high <= 1.")

    options = []
    for code, ratings in RATINGS_KW.items():
        if not isinstance(ratings, list) or not ratings:
            raise ValueError(f"Missing rating options for {code}.")
        values = [positive_number(value) for value in ratings]
        if len(values) != len(set(values)):
            raise ValueError(f"Duplicate rating in {code}.")
        for power in values:
            options.append({
                "rating_id": f"TEST_{code}_{power:g}",
                "charger_code": code,
                "rated_output_kw": power,
                "contexts": CONTEXTS.copy(),
                "selection_weight": 1.0,
                "evidence_status": "development_assumption",
                "source_reference": source_note(code, power),
            })
    return {
        "schema_version": "public_power_config_v1",
        "data_origin": "DEVELOPMENT_ASSUMPTIONS",
        "purpose": "Synthetic engineering test only; no verified station inventory or national usage mixture.",
        "fraction_meaning": "Average delivered session power divided by rated output; not conversion efficiency.",
        "weight_meaning": "Equal test weights within each context and charger type; not observed usage probabilities.",
        "scope": "Non-home equipment options; T1 may include private workplace charging. Public access and connector compatibility are unresolved.",
        "scenarios": SCENARIOS,
        "rating_options": options,
    }


def main():
    config = make_config()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    output = ROOT / "data/processed/synthetic" / f"public_power_config_{stamp}"
    output.mkdir(parents=True, exist_ok=False)
    path = output / "public_power_config.json"
    path.write_text(json.dumps(config, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    report = {
        "schema_version": "public_power_config_report_v1",
        "data_origin": "DEVELOPMENT_ASSUMPTIONS",
        "rating_options": len(config["rating_options"]),
        "scenarios": list(config["scenarios"]),
        "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "config_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "parameters_fitted": False,
        "station_inventory_verified": False,
        "owner_parameter_package_updated": False,
    }
    (output / "public_power_config_report.json").write_text(
        json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print("Public charging power configuration complete.")
    for code, values in RATINGS_KW.items():
        print(f"{code}_ratings_kw: {values}")
    print(f"rating_options: {len(config['rating_options'])}")
    print(f"power_scenarios: {list(config['scenarios'])}")
    print(f"Output: {output.relative_to(ROOT)}")
    print("Test assumptions only; owner parameters not updated; raw data unchanged.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"Public power configuration failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
