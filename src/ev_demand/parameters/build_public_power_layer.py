"""Add public/non-home power models to an existing synthetic parameter package."""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from random import Random
import sys

ROOT = Path(__file__).resolve().parents[3]

CONTEXTS = {"T1", "T2", "T3", "T4"}
NONHOME_CONTEXTS = {"T1", "T3", "T4"}
CHARGER_CODES = {"SOCKET_PORTABLE", "AC", "DC"}


def finite_number(value, label, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a JSON number.")

    value = float(value)

    if not math.isfinite(value) or (positive and value <= 0):
        raise ValueError(
            f"{label} must be finite"
            + (" and positive." if positive else ".")
        )

    return value


def positive_bounds(low, high, label):
    low = finite_number(low, label + " minimum", positive=True)
    high = finite_number(high, label + " maximum", positive=True)

    if high < low:
        raise ValueError(f"{label} has reversed bounds.")

    return low, high


def compile_power_config(config, scenario):
    """Validate assumptions and build context/type conditional mixtures."""
    if config.get("schema_version") != "public_power_config_v1":
        raise ValueError("Expected public_power_config_v1.")

    if config.get("data_origin") != "DEVELOPMENT_ASSUMPTIONS":
        raise ValueError(
            "This version accepts development assumptions only."
        )

    scenarios = config.get("scenarios", {})

    if scenario not in scenarios:
        raise ValueError(f"Unknown power scenario: {scenario}.")

    selected = scenarios[scenario]

    if not selected.get("description"):
        raise ValueError("Each scenario needs a description.")

    factors = {}

    for code in sorted(CHARGER_CODES):
        bounds = selected["average_to_rated_power_fraction"][code]

        if not isinstance(bounds, list) or len(bounds) != 2:
            raise ValueError(f"{code} power fraction needs two bounds.")

        low, high = positive_bounds(
            *bounds,
            code + " power fraction",
        )

        if high > 1:
            raise ValueError(
                "Average-to-rated output fraction cannot exceed 1."
            )

        factors[code] = (low, high)

    rows = config.get("rating_options")

    if not isinstance(rows, list) or not rows:
        raise ValueError("At least one rating option is required.")

    ids = set()
    pools = {}

    for row in rows:
        rating_id = row["rating_id"]

        if (
            not isinstance(rating_id, str)
            or not rating_id.strip()
            or rating_id in ids
        ):
            raise ValueError("Rating IDs must be nonempty and unique.")

        ids.add(rating_id)

        code = row["charger_code"]

        if code not in CHARGER_CODES:
            raise ValueError(f"Unknown charger code: {code}.")

        contexts = row["contexts"]

        if (
            not isinstance(contexts, list)
            or not contexts
            or len(contexts) != len(set(contexts))
            or not set(contexts).issubset(NONHOME_CONTEXTS)
        ):
            raise ValueError(
                "Rating contexts must be unique T1, T3 or T4 codes."
            )

        if row.get("evidence_status") != "development_assumption":
            raise ValueError(
                "Do not label test options as measured inventory."
            )

        if not row.get("source_reference"):
            raise ValueError(
                "Every rating needs a source or assumption reference."
            )

        rated = finite_number(
            row["rated_output_kw"],
            rating_id + " rating",
            positive=True,
        )
        weight = finite_number(
            row["selection_weight"],
            rating_id + " weight",
        )

        if weight < 0:
            raise ValueError("Selection weights cannot be negative.")

        f_low, f_high = factors[code]

        for context in contexts:
            pools.setdefault((context, code), []).append({
                "rating_id": rating_id,
                "charger_code": code,
                "rated_output_kw": rated,
                "selection_weight": weight,
                "power_fraction_min": f_low,
                "power_fraction_max": f_high,
                "effective_average_kw_min": rated * f_low,
                "effective_average_kw_max": rated * f_high,
                "evidence_status": row["evidence_status"],
                "source_reference": row["source_reference"],
            })

    for key, options in pools.items():
        total = math.fsum(
            option["selection_weight"]
            for option in options
        )

        if not math.isfinite(total) or total <= 0:
            raise ValueError(
                f"No positive selection weight for {key}."
            )

        for option in options:
            option["selection_probability"] = (
                option["selection_weight"] / total
            )

    return pools


def apply_public_power_layer(package, config, scenario="reduced_test"):
    """Return a new package without mutating or refitting owner inputs."""
    if package.get("schema_version") != "bounded_test_parameters_v1":
        raise ValueError(
            "Expected the original bounded_test_parameters_v1 package."
        )

    if package.get("data_origin") != "SYNTHETIC":
        raise ValueError(
            "Only synthetic owner inputs are supported at this stage."
        )

    if package.get("ready_for_synthetic_engine_test") is not True:
        raise ValueError(
            "Input package is not marked ready for a synthetic engine test."
        )

    for flag in (
        "empirically_calibrated",
        "district_demand_ready",
        "national_forecast_ready",
    ):
        if package.get(flag) is not False:
            raise ValueError(
                f"Unexpected research-readiness flag: {flag}."
            )

    pools = compile_power_config(config, scenario)
    profiles = package.get("owner_profiles")

    if not isinstance(profiles, list) or not profiles:
        raise ValueError("Input needs nonempty owner_profiles.")

    summary = package["summary"]
    ready = summary["owners_ready_for_bounded_test"]
    retained = summary["owners_retained_in_audit"]
    deferred = summary["owners_deferred"]

    if any(
        type(value) is not int or value < 0
        for value in (ready, retained, deferred)
    ):
        raise ValueError("Owner counts must be nonnegative integers.")

    if ready != len(profiles) or retained != ready + deferred:
        raise ValueError(
            "Owner counts do not match the parameter package."
        )

    output = deepcopy(package)
    seen = set()
    counts = Counter()

    for profile in output["owner_profiles"]:
        owner_id = profile["owner_id"]

        if (
            not isinstance(owner_id, str)
            or not owner_id
            or owner_id in seen
        ):
            raise ValueError("Duplicate or missing owner ID.")

        seen.add(owner_id)

        if profile.get("data_origin") != "SYNTHETIC":
            raise ValueError(f"Owner {owner_id} is not synthetic.")

        active = profile["active_contexts"]
        codes = [row["context"] for row in active]
        inactive = profile["inactive_contexts"]

        if (
            not active
            or len(codes) != len(set(codes))
            or len(inactive) != len(set(inactive))
            or set(codes) & set(inactive)
            or set(codes) | set(inactive) != CONTEXTS
        ):
            raise ValueError(
                f"Invalid active/inactive contexts for {owner_id}."
            )

        for context in active:
            code = context["context"]
            charger = context["charger_code"]

            if charger not in CHARGER_CODES:
                raise ValueError(
                    f"Unknown charger for {owner_id}/{code}."
                )

            old_low, old_high = positive_bounds(
                context["power_kw_min"],
                context["power_kw_max"],
                owner_id + "/" + code,
            )

            if code == "T2":
                if charger not in {"AC", "SOCKET_PORTABLE"}:
                    raise ValueError(
                        "Home DC is outside the current reported-home model."
                    )

                context["power_model"] = {
                    "kind": "reported_home_uniform",
                    "effective_average_kw_min": old_low,
                    "effective_average_kw_max": old_high,
                    "evidence_status": "reported_approximate_home_range",
                }

                counts["home_contexts_preserved"] += 1

            else:
                key = (code, charger)

                if key not in pools:
                    raise ValueError(
                        f"No rating options for {owner_id}/{code}/{charger}."
                    )

                context["superseded_power_input"] = {
                    field: context.pop(field)
                    for field in (
                        "power_kw_min",
                        "power_kw_max",
                        "power_source",
                    )
                }

                context["power_model"] = {
                    "kind": "nonhome_rating_mixture",
                    "scenario": scenario,
                    "options": deepcopy(pools[key]),
                    "sampling": (
                        "Choose rating by normalized test weights; "
                        "draw fraction uniformly."
                    ),
                    "scope": (
                        "Non-home equipment assumption; "
                        "public/private access not observed."
                    ),
                    "evidence_status": "development_assumption",
                }

                counts["nonhome_contexts_updated"] += 1
                counts["nonhome_" + charger] += 1

    policy = output["policy"]

    old_nonhome = policy.pop(
        "nonhome_test_average_power_kw",
        None,
    )
    old_status = policy.pop("nonhome_power_status", None)

    policy["nonhome_power"] = (
        "Use each context's power_model; no legacy fixed DC fallback."
    )
    policy["power_boundary"] = (
        "Effective power and energy at the same delivered-energy boundary. "
        "No grid-side conversion loss or auxiliary-load estimate is added."
    )

    output["schema_version"] = (
        "bounded_test_parameters_with_public_power_v1"
    )
    output["public_power_layer"] = {
        "schema_version": "public_power_layer_v1",
        "scenario": scenario,
        "config_snapshot": deepcopy(config),
        "summary": dict(counts),
        "superseded_policy": {
            "nonhome_test_average_power_kw": old_nonhome,
            "nonhome_power_status": old_status,
        },
        "limitations": [
            (
                "Ratings, weights and power fractions are test assumptions; "
                "no national mix was estimated."
            ),
            (
                "Vehicle-specific acceptance limits and connectors "
                "are unresolved unless supplied separately."
            ),
            (
                "No site port counts, power sharing, station allocation, "
                "queues or charging curves are simulated."
            ),
            (
                "T1 may include private workplace charging; "
                "no claim of public station access is made."
            ),
            (
                "This change does not release any deferred owner "
                "or complete battery-state modelling."
            ),
        ],
    }

    return output


def sample_session_power(context, rng: Random, *, vehicle_limit_kw=None):
    """Draw power once per session and return traceable metadata.

    A supplied vehicle limit must match this session's AC/DC charging path.
    Its source and connector check remain the caller's responsibility.
    """
    if vehicle_limit_kw is not None:
        vehicle_limit_kw = finite_number(
            vehicle_limit_kw,
            "vehicle limit",
            positive=True,
        )

    model = context["power_model"]
    kind = model["kind"]

    if kind == "reported_home_uniform":
        low, high = positive_bounds(
            model["effective_average_kw_min"],
            model["effective_average_kw_max"],
            "home power",
        )
        candidate = rng.uniform(low, high)
        rating_id, rated, fraction = None, None, None

    elif kind == "nonhome_rating_mixture":
        options = model["options"]

        option = rng.choices(
            options,
            weights=[
                item["selection_probability"]
                for item in options
            ],
            k=1,
        )[0]

        rated = finite_number(
            option["rated_output_kw"],
            "rated output",
            positive=True,
        )
        low, high = positive_bounds(
            option["power_fraction_min"],
            option["power_fraction_max"],
            "power fraction",
        )

        if high > 1:
            raise ValueError("Power fraction exceeds rated output.")

        fraction = rng.uniform(low, high)
        candidate = rated * fraction
        rating_id = option["rating_id"]

    else:
        raise ValueError(f"Unknown power model: {kind}.")

    effective = (
        min(candidate, vehicle_limit_kw)
        if vehicle_limit_kw is not None
        else candidate
    )

    return {
        "effective_average_power_kw": effective,
        "power_model_kind": kind,
        "rating_id": rating_id,
        "rated_output_kw": rated,
        "sampled_average_to_rated_fraction": fraction,
        "uncapped_effective_average_power_kw": candidate,
        "vehicle_limit_kw": vehicle_limit_kw,
        "vehicle_limit_applied": (
            vehicle_limit_kw is not None
            and candidate > vehicle_limit_kw
        ),
        "vehicle_limit_status": (
            "supplied_by_caller"
            if vehicle_limit_kw is not None
            else "unresolved"
        ),
        "connector_compatibility_checked": False,
        "power_evidence_status": model["evidence_status"],
        "scenario": model.get("scenario", "reported_home"),
    }


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reject_constant(value):
    raise ValueError(f"Nonfinite JSON value: {value}.")


def load_json(path):
    return json.loads(
        path.read_text(encoding="utf-8-sig"),
        parse_constant=reject_constant,
    )


def validate_readiness(path, package):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    ids = [row["owner_id"] for row in rows]

    if len(ids) != len(set(ids)):
        raise ValueError("Readiness file has duplicate owner IDs.")

    if any(row["data_origin"] != "SYNTHETIC" for row in rows):
        raise ValueError(
            "Readiness file is not consistently synthetic."
        )

    allowed = {
        "ready_for_bounded_test",
        "deferred_for_review",
    }

    if any(row["status"] not in allowed for row in rows):
        raise ValueError("Unknown readiness status.")

    ready_ids = {
        row["owner_id"]
        for row in rows
        if row["status"] == "ready_for_bounded_test"
    }
    expected = {
        owner["owner_id"]
        for owner in package["owner_profiles"]
    }
    counts = Counter(row["status"] for row in rows)
    summary = package["summary"]

    if (
        ready_ids != expected
        or len(rows) != summary["owners_retained_in_audit"]
        or counts["deferred_for_review"] != summary["owners_deferred"]
    ):
        raise ValueError(
            "Readiness file and parameter package do not match."
        )

    return rows


def build_files(parameters, config_path, scenario, project_root=ROOT):
    project_root = project_root.resolve()
    allowed = (
        project_root / "data/processed/synthetic"
    ).resolve()

    parameters = (project_root / parameters).resolve()

    if not parameters.is_relative_to(allowed):
        raise ValueError(
            "Parameters must be under data/processed/synthetic."
        )

    config_path = (project_root / config_path).resolve()

    if not config_path.is_relative_to(project_root):
        raise ValueError("Config must be inside the project.")

    paths = {
        "simulator_parameters.json": (
            parameters / "simulator_parameters.json"
        ),
        "owner_readiness.csv": parameters / "owner_readiness.csv",
        "public_power_config.json": config_path,
    }

    before = {
        name: sha256(path)
        for name, path in paths.items()
    }

    package = load_json(paths["simulator_parameters.json"])
    config = load_json(config_path)

    output_package = apply_public_power_layer(
        package,
        config,
        scenario,
    )
    validate_readiness(paths["owner_readiness.csv"], package)

    output_package["public_power_layer"]["input_sha256"] = before
    output_package["public_power_layer"]["input_paths"] = {
        name: path.relative_to(project_root).as_posix()
        for name, path in paths.items()
    }

    readiness_bytes = paths["owner_readiness.csv"].read_bytes()

    if (
        hashlib.sha256(readiness_bytes).hexdigest()
        != before["owner_readiness.csv"]
    ):
        raise ValueError(
            "Readiness input changed during preparation."
        )

    if any(
        sha256(paths[name]) != digest
        for name, digest in before.items()
    ):
        raise ValueError(
            "An input changed during preparation; rerun."
        )

    stamp = datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%S_%fZ"
    )
    output = allowed / f"parameters_public_power_{stamp}"
    output.mkdir(parents=True, exist_ok=False)

    (output / "simulator_parameters.json").write_text(
        json.dumps(
            output_package,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        ) + "\n",
        encoding="utf-8",
    )

    (output / "owner_readiness.csv").write_bytes(readiness_bytes)

    report = {
        "schema_version": "public_power_report_v1",
        "data_origin": "SYNTHETIC",
        "power_evidence_status": "DEVELOPMENT_ASSUMPTIONS",
        "scenario": scenario,
        "owner_summary": package["summary"],
        "power_summary": output_package["public_power_layer"]["summary"],
        "config_rating_options": len(config["rating_options"]),
        "input_sha256": before,
        "output_sha256": {
            name: sha256(output / name)
            for name in (
                "simulator_parameters.json",
                "owner_readiness.csv",
            )
        },
        "owner_profiles_preserved": True,
        "readiness_file_copied_exactly": True,
        "parameters_fitted": False,
        "national_charger_mix_estimated": False,
        "vehicle_connector_matching_completed": False,
        "raw_data_changed": False,
    }

    (output / "public_power_report.json").write_text(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        ) + "\n",
        encoding="utf-8",
    )

    return output, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--parameters",
        required=True,
        help="Original parameter folder",
    )
    parser.add_argument(
        "--config",
        required=True,
        help="Configuration JSON from the previous step",
    )
    parser.add_argument("--scenario", default="reduced_test")
    args = parser.parse_args()

    output, report = build_files(
        Path(args.parameters),
        Path(args.config),
        args.scenario,
    )

    print("Public charging power layer complete.")

    for name in (
        "owners_retained_in_audit",
        "owners_ready_for_bounded_test",
        "owners_deferred",
    ):
        print(f"{name}: {report['owner_summary'][name]}")

    print(f"power_scenario: {report['scenario']}")
    print(f"power_summary: {report['power_summary']}")
    print(f"Output: {output.relative_to(ROOT)}")
    print(
        "Power assumptions recorded; home ranges and owner readiness "
        "preserved; no parameters fitted."
    )


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError, OverflowError) as exc:
        print(
            f"Public power preparation failed: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1)