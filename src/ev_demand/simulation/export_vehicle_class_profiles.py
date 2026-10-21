"""Export vehicle-class profiles from a completed availability pilot.

Uses saved sessions and parameter selections. No simulations, fitting,
resampling, eligibility changes or fleet scaling are performed.
"""

import argparse
import csv
import hashlib
import itertools
import json
import math
import statistics
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
CONTEXTS = ("T1", "T2", "T3", "T4")
ALL_CONTEXTS = CONTEXTS + ("TOTAL",)
GENERATORS = ("poisson_control", "availability_aware")
REL_TOL = 1e-8
ABS_TOL = 1e-7


def require(condition, message):
    if not condition:
        raise ValueError(message)


def number(value):
    result = float(value)
    require(math.isfinite(result), f"Non-finite number: {value!r}")
    return result


def boolean(value):
    text = str(value).strip().lower()
    require(text in {"true", "false"}, f"Invalid boolean: {value!r}")
    return text == "true"


def optional_boolean(value):
    if value is None or str(value).strip() == "":
        return None
    return boolean(value)


def close(actual, expected, label):
    require(
        math.isclose(actual, expected, rel_tol=REL_TOL, abs_tol=ABS_TOL),
        f"{label}: reconstructed={actual:.12g}, reference={expected:.12g}",
    )


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_path(value, base=ROOT):
    path = Path(value)
    if not path.is_absolute():
        path = base / path
    path = path.resolve()
    require(path.is_relative_to(ROOT), f"Path is outside the repository: {path}")
    return path


def relative(path):
    return path.relative_to(ROOT).as_posix()


def csv_rows(path, required_columns):
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        fields = reader.fieldnames or []
        require(len(fields) == len(set(fields)), f"Duplicate columns: {path}")
        missing = set(required_columns) - set(fields)
        require(not missing, f"{path.name}: missing columns {sorted(missing)}")
        for line, row in enumerate(reader, start=2):
            require(
                None not in row and all(v is not None for v in row.values()),
                f"{path.name}: malformed CSV row at line {line}",
            )
            yield row


def keyed_csv(path, key_columns, required_columns):
    result = {}
    for row in csv_rows(path, set(key_columns) | set(required_columns)):
        key = tuple(row[column] for column in key_columns)
        require(key not in result, f"{path.name}: duplicate key {key}")
        result[key] = row
    return result


def check_grid(table, expected, label):
    observed = set(table)
    require(
        observed == expected,
        f"{label}: missing={len(expected - observed)}, "
        f"unexpected={len(observed - expected)}",
    )


def mean_mcse(values):
    values = list(values)
    average = statistics.mean(values)
    mcse = (
        statistics.stdev(values) / math.sqrt(len(values))
        if len(values) > 1 else None
    )
    return average, mcse


def divide_optional(value, denominator):
    return None if value is None else value / denominator


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot", required=True)
    parser.add_argument(
        "--features",
        default=(
            "data/processed/synthetic/"
            "features_20260930T105224_893907Z/owners_features.csv"
        ),
    )
    parser.add_argument("--output-root", default="data/processed/synthetic")
    args = parser.parse_args()

    folder = resolve_path(args.pilot)
    features_path = resolve_path(args.features)
    report_path = folder / "availability_generator_report.json"
    watched = {}

    def watch(path, expected=None):
        path = path.resolve()
        require(path.is_file(), f"Input file not found: {path}")
        actual = watched.get(path)
        if actual is None:
            actual = sha256(path)
            watched[path] = actual
        if expected is not None:
            require(
                isinstance(expected, str)
                and len(expected) == 64
                and actual == expected.lower(),
                f"Source hash mismatch: {relative(path)}",
            )
        return actual

    def verify_unchanged():
        for path, expected in watched.items():
            require(
                sha256(path) == expected,
                f"Input changed during export: {relative(path)}",
            )

    watch(Path(__file__).resolve())
    watch(features_path)
    watch(report_path)
    report = json.loads(report_path.read_text(encoding="utf-8-sig"))
    require(
        report.get("schema_version") == "availability_generator_comparison_v1",
        "Unsupported pilot report schema.",
    )
    require(report.get("status") == "complete", "Pilot is incomplete.")
    require(
        report.get("output_origin") == "SIMULATED"
        and report.get("behaviour_input_origin") == "SYNTHETIC",
        "This exporter version expects the synthetic availability pilot.",
    )

    required_source_checks = (
        "source_hashes_verified",
        "selected_owner_parameters_paired",
        "selected_time_and_window_screens_rechecked",
        "independent_calibration_verification_study_streams",
        "no_owner_overlaps",
        "session_and_hourly_energy_conserved",
        "replicate_and_mean_profile_energy_checked",
        "complete_owner_grid",
    )
    for name in required_source_checks:
        require(
            report.get("checks", {}).get(name) is True,
            f"Required source check did not pass: {name}",
        )
    for field in ("input_sha256", "output_sha256"):
        require(
            isinstance(report.get(field), dict) and report[field],
            f"Missing source manifest: {field}",
        )

    print("Verifying recorded pilot source and output hashes...")
    for name, expected in report["input_sha256"].items():
        watch(resolve_path(name), expected)
    for name, expected in report["output_sha256"].items():
        require(
            Path(name).name == name,
            f"Expected a pilot output filename, received: {name}",
        )
        watch(resolve_path(name, folder), expected)

    needed_outputs = {
        "sessions.csv", "selected_owner_parameters.csv", "owner_calibration.csv",
        "replicate_comparison.csv", "replicate_profiles.csv",
        "mean_profile_24h.csv", "generator_summary.csv",
    }
    require(
        needed_outputs <= set(report["output_sha256"]),
        "A required pilot output is absent from the hash manifest.",
    )
    days = report["study_days"]
    n = report["replicates"]
    require(type(days) is int and days > 0, "study_days must be positive integer.")
    require(type(n) is int and n > 0, "replicates must be a positive integer.")
    end_of_study = 24.0 * days
    replicates = tuple(str(i) for i in range(1, n + 1))

    owner_ids = report["owner_ids"]
    require(
        isinstance(owner_ids, list)
        and all(isinstance(x, str) and x for x in owner_ids),
        "Invalid source owner list.",
    )
    owners = set(owner_ids)
    require(
        len(owners) == len(owner_ids) == report["owners_simulated"],
        "Source owner list/count mismatch.",
    )
    features = {}
    for row in csv_rows(
        features_path, ("owner_id", "vehicle_class_answer", "data_origin")
    ):
        owner = row["owner_id"]
        require(owner and owner not in features, "Duplicate/empty feature owner.")
        require(row["vehicle_class_answer"].strip(), f"Missing class: {owner}")
        features[owner] = row
    require(owners <= set(features), "A simulated owner has no feature record.")
    for owner in owners:
        require(
            features[owner]["data_origin"] == "SYNTHETIC",
            f"Unexpected feature origin: {owner}",
        )

    owner_class = {
        owner: features[owner]["vehicle_class_answer"] for owner in owners
    }
    retained_counts = Counter(
        row["vehicle_class_answer"] for row in features.values()
    )
    class_counts = Counter(owner_class.values())
    classes = tuple(sorted(class_counts))
    selected = keyed_csv(
        folder / "selected_owner_parameters.csv",
        ("replicate", "owner_id"),
        ("energy_case", "parameter_draw", "screen_cohort",
         "exceeds_reported_battery_upper", "total_weekly_rate"),
    )
    owner_grid = set(itertools.product(replicates, owners))
    check_grid(selected, owner_grid, "Selected-owner grid")
    for (replicate, owner), row in selected.items():
        require(
            row["energy_case"] == report["energy_case"]
            and row["screen_cohort"] == report["cohort"],
            f"Selection case/cohort mismatch: {replicate}, {owner}",
        )
        require(int(row["parameter_draw"]) > 0, "Invalid parameter draw.")
        optional_boolean(row["exceeds_reported_battery_upper"])

    calibration = keyed_csv(
        folder / "owner_calibration.csv", ("replicate", "owner_id"),
        ("target_total_weekly_rate", "calibration_status",
         "verification_relative_error_percent",
         "verification_within_development_band"),
    )
    check_grid(calibration, owner_grid, "Calibration-owner grid")
    selection_review = []
    for replicate, owner in sorted(owner_grid):
        parameter = selected[(replicate, owner)]
        diagnostic = calibration[(replicate, owner)]
        close(
            number(parameter["total_weekly_rate"]),
            number(diagnostic["target_total_weekly_rate"]),
            f"Calibration target link: {replicate}, {owner}",
        )
        inside_band = boolean(diagnostic["verification_within_development_band"])
        selection_review.append({
            "replicate": replicate,
            "owner_id": owner,
            "vehicle_class": owner_class[owner],
            "parameter_draw": parameter["parameter_draw"],
            "exceeds_reported_battery_upper": optional_boolean(
                parameter["exceeds_reported_battery_upper"]
            ),
            "calibration_status": diagnostic["calibration_status"],
            "verification_relative_error_percent": number(
                diagnostic["verification_relative_error_percent"]
            ),
            "verification_within_development_band": inside_band,
        })

    status_counts = Counter(row["calibration_status"] for row in selection_review)
    outside_band = sum(
        not row["verification_within_development_band"] for row in selection_review
    )
    battery_flag_counts = {
        "true": sum(row["exceeds_reported_battery_upper"] is True
                    for row in selection_review),
        "false": sum(row["exceeds_reported_battery_upper"] is False
                     for row in selection_review),
        "missing": sum(row["exceeds_reported_battery_upper"] is None
                       for row in selection_review),
    }
    require(
        dict(status_counts) == report["calibration_status_counts"],
        "Calibration status totals do not reproduce the pilot report.",
    )
    require(
        outside_band == report["verification_selections_outside_band"],
        "Verification flag total does not reproduce the pilot report.",
    )

    keys = tuple(itertools.product(replicates, GENERATORS, classes, ALL_CONTEXTS))
    daily = {key: 0.0 for key in keys}
    profiles = {key: [0.0] * 24 for key in keys}
    power_evidence = Counter()
    session_ids = set()
    observed_generators = set()
    session_count = 0
    session_columns = (
        "generator", "replicate", "owner_id", "session_id", "context",
        "output_origin", "behaviour_input_origin", "data_origin",
        "start_hour_absolute", "end_hour_absolute", "active_duration_hours",
        "session_energy_kwh", "effective_average_power_kw",
        "energy_before_study_kwh", "energy_in_study_kwh", "energy_after_study_kwh",
        "vehicle_limit_status", "connector_compatibility_checked",
        "power_evidence_status",
    )
    print("Reconstructing class profiles from saved accepted sessions...")
    for row in csv_rows(folder / "sessions.csv", session_columns):
        session_count += 1
        generator = row["generator"]
        replicate = row["replicate"]
        owner = row["owner_id"]
        context = row["context"]
        require(generator in GENERATORS, f"Unknown generator: {generator}")
        require(
            (replicate, owner) in selected,
            f"Unselected session owner/replicate: {replicate}, {owner}",
        )
        require(context in CONTEXTS, f"Unknown session context: {context}")
        require(
            row["output_origin"] == "SIMULATED"
            and row["behaviour_input_origin"] == "SYNTHETIC"
            and row["data_origin"] == "SYNTHETIC",
            "Session origin mismatch.",
        )
        session_key = (generator, replicate, row["session_id"])
        require(
            row["session_id"] and session_key not in session_ids,
            f"Duplicate/empty session ID: {session_key}",
        )
        session_ids.add(session_key)
        observed_generators.add(generator)

        start = number(row["start_hour_absolute"])
        end = number(row["end_hour_absolute"])
        duration = number(row["active_duration_hours"])
        energy = number(row["session_energy_kwh"])
        power = number(row["effective_average_power_kw"])
        require(
            end >= start and duration >= 0 and energy >= 0 and power > 0,
            f"Invalid session time, energy or power: {session_key}",
        )
        close(end - start, duration, f"Session duration: {session_key}")
        close(power * duration, energy, f"Session energy: {session_key}")
        low = max(0.0, start)
        high = min(end_of_study, end)
        reconstructed = {
            "energy_before_study_kwh": power * max(0.0, min(end, 0.0) - start),
            "energy_in_study_kwh": power * max(0.0, high - low),
            "energy_after_study_kwh":
                power * max(0.0, end - max(start, end_of_study)),
        }
        saved = {}
        for column, expected in reconstructed.items():
            saved[column] = number(row[column])
            require(saved[column] >= 0, f"Negative partition: {session_key}")
            close(saved[column], expected, f"{column}: {session_key}")
        close(sum(saved.values()), energy, f"Energy partition: {session_key}")

        vehicle_class = owner_class[owner]
        target_keys = [
            (replicate, generator, vehicle_class, context),
            (replicate, generator, vehicle_class, "TOTAL"),
        ]
        for key in target_keys:
            daily[key] += saved["energy_in_study_kwh"] / days
        if high > low:
            for hour_absolute in range(math.floor(low), math.ceil(high)):
                overlap = min(high, hour_absolute + 1.0) - max(low, hour_absolute)
                if overlap > 0:
                    contribution = power * overlap / days
                    for key in target_keys:
                        profiles[key][hour_absolute % 24] += contribution
        if saved["energy_in_study_kwh"] > 0:
            connector_checked = boolean(row["connector_compatibility_checked"])
            power_evidence[(
                vehicle_class, generator, row["vehicle_limit_status"],
                connector_checked, row["power_evidence_status"],
            )] += 1

    require(observed_generators == set(GENERATORS), "Expected both generators.")
    for key in keys:
        close(sum(profiles[key]), daily[key], f"Class profile/energy: {key}")
    for replicate, generator, vehicle_class in itertools.product(
        replicates, GENERATORS, classes
    ):
        total_key = (replicate, generator, vehicle_class, "TOTAL")
        context_keys = [
            (replicate, generator, vehicle_class, context) for context in CONTEXTS
        ]
        close(
            sum(daily[key] for key in context_keys), daily[total_key],
            f"Class context energy sum: {total_key}",
        )
        for hour in range(24):
            close(
                sum(profiles[key][hour] for key in context_keys),
                profiles[total_key][hour],
                f"Class context profile sum: {total_key}, hour={hour}",
            )

    def aggregate_daily(replicate, generator, context):
        return sum(daily[(replicate, generator, c, context)] for c in classes)

    def aggregate_power(replicate, generator, context, hour):
        return sum(profiles[(replicate, generator, c, context)][hour]
                   for c in classes)

    reconciliation = []

    def reconcile(check, unit, replicate, context, hour, generator,
                  actual, reference):
        reference = number(reference)
        close(actual, reference,
              f"{check}: {replicate}, {generator}, {context}, {hour}")
        reconciliation.append({
            "check": check, "unit": unit, "replicate": replicate,
            "context": context, "hour_of_day": hour, "generator": generator,
            "reconstructed_value": actual, "saved_reference_value": reference,
            "absolute_error": abs(actual - reference),
        })

    print("Checking class sums against saved pilot results...")
    reference_daily = keyed_csv(
        folder / "replicate_comparison.csv", ("replicate", "context"),
        ("control_daily_energy_kwh", "availability_daily_energy_kwh",
         "difference_daily_energy_kwh"),
    )
    check_grid(reference_daily, set(itertools.product(replicates, ALL_CONTEXTS)),
               "Published replicate energy grid")
    for (replicate, context), row in reference_daily.items():
        values = [aggregate_daily(replicate, g, context) for g in GENERATORS]
        for generator, value, column in zip(
            GENERATORS, values,
            ("control_daily_energy_kwh", "availability_daily_energy_kwh"),
        ):
            reconcile("replicate_daily_energy", "kWh/day", replicate, context,
                      "", generator, value, row[column])
        reconcile("replicate_daily_difference", "kWh/day", replicate, context,
                  "", "availability_minus_control", values[1] - values[0],
                  row["difference_daily_energy_kwh"])

    reference_profiles = keyed_csv(
        folder / "replicate_profiles.csv", ("replicate", "context", "hour_of_day"),
        ("control_mean_power_kw", "availability_mean_power_kw", "difference_kw"),
    )
    profile_grid = set(itertools.product(
        replicates, ALL_CONTEXTS, tuple(str(h) for h in range(24))
    ))
    check_grid(reference_profiles, profile_grid, "Published replicate profiles")
    for (replicate, context, hour_text), row in reference_profiles.items():
        hour = int(hour_text)
        values = [aggregate_power(replicate, g, context, hour) for g in GENERATORS]
        for generator, value, column in zip(
            GENERATORS, values,
            ("control_mean_power_kw", "availability_mean_power_kw"),
        ):
            reconcile("replicate_hourly_profile", "kW", replicate, context,
                      hour, generator, value, row[column])
        reconcile("replicate_hourly_difference", "kW", replicate, context,
                  hour, "availability_minus_control", values[1] - values[0],
                  row["difference_kw"])

    reference_mean = keyed_csv(
        folder / "mean_profile_24h.csv", ("context", "hour_of_day"),
        ("n_replicates", "control_mean_power_kw", "availability_mean_power_kw",
         "mean_difference_kw", "paired_difference_mcse_kw"),
    )
    check_grid(reference_mean, set(itertools.product(
        ALL_CONTEXTS, tuple(str(h) for h in range(24))
    )), "Published mean-profile grid")
    for (context, hour_text), row in reference_mean.items():
        hour = int(hour_text)
        require(int(row["n_replicates"]) == n, "Mean-profile replicate mismatch.")
        values = {
            generator: [aggregate_power(r, generator, context, hour)
                        for r in replicates]
            for generator in GENERATORS
        }
        for generator, column in zip(
            GENERATORS, ("control_mean_power_kw", "availability_mean_power_kw")
        ):
            reconcile("ensemble_hourly_profile", "kW", "", context, hour,
                      generator, statistics.mean(values[generator]), row[column])
        differences = [b - a for a, b in zip(
            values["poisson_control"], values["availability_aware"]
        )]
        difference_mean, difference_mcse = mean_mcse(differences)
        reconcile("ensemble_hourly_difference", "kW", "", context, hour,
                  "availability_minus_control", difference_mean,
                  row["mean_difference_kw"])
        if difference_mcse is not None:
            reconcile("ensemble_hourly_difference_mcse", "kW", "", context, hour,
                      "availability_minus_control", difference_mcse,
                      row["paired_difference_mcse_kw"])

    reference_summary = keyed_csv(
        folder / "generator_summary.csv", ("context",),
        ("control_mean_daily_energy_kwh", "availability_mean_daily_energy_kwh",
         "mean_daily_energy_difference_kwh", "paired_daily_energy_mcse_kwh"),
    )
    check_grid(reference_summary, {(c,) for c in ALL_CONTEXTS},
               "Published generator-summary grid")
    for (context,), row in reference_summary.items():
        values = {
            generator: [aggregate_daily(r, generator, context) for r in replicates]
            for generator in GENERATORS
        }
        for generator, column in zip(
            GENERATORS,
            ("control_mean_daily_energy_kwh", "availability_mean_daily_energy_kwh"),
        ):
            reconcile("ensemble_daily_energy", "kWh/day", "", context, "",
                      generator, statistics.mean(values[generator]), row[column])
        differences = [b - a for a, b in zip(
            values["poisson_control"], values["availability_aware"]
        )]
        difference_mean, difference_mcse = mean_mcse(differences)
        reconcile("ensemble_daily_difference", "kWh/day", "", context, "",
                  "availability_minus_control", difference_mean,
                  row["mean_daily_energy_difference_kwh"])
        if difference_mcse is not None:
            reconcile("ensemble_daily_difference_mcse", "kWh/day", "", context,
                      "", "availability_minus_control", difference_mcse,
                      row["paired_daily_energy_mcse_kwh"])

    verify_unchanged()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    output = resolve_path(args.output_root) / f"vehicle_class_profiles_{stamp}"
    output.mkdir(parents=True, exist_ok=False)
    common = {
        "cohort": report["cohort"], "energy_case": report["energy_case"],
        "power_scenario": report["power_scenario"], "output_origin": "SIMULATED",
        "behaviour_input_origin": "SYNTHETIC",
    }
    output_files = []
    output_row_counts = {}

    def write_csv(name, columns, rows):
        path = output / name
        count = 0
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(columns) + list(common))
            writer.writeheader()
            for row in rows:
                writer.writerow({**row, **common})
                count += 1
        output_files.append(path)
        output_row_counts[name] = count

    write_csv("class_coverage.csv", (
        "vehicle_class", "retained_feature_owners", "selected_owners", "profile_status"
    ), ({
        "vehicle_class": c, "retained_feature_owners": retained_counts[c],
        "selected_owners": class_counts[c],
        "profile_status": "profile_exported" if class_counts[c] else "no_selected_owners",
    } for c in sorted(retained_counts)))
    write_csv("class_owner_links.csv", (
        "owner_id", "vehicle_class", "owner_replicate_selections"
    ), ({
        "owner_id": owner, "vehicle_class": owner_class[owner],
        "owner_replicate_selections": n,
    } for owner in sorted(owners)))
    write_csv("class_selection_review.csv", (
        "replicate", "owner_id", "vehicle_class", "parameter_draw",
        "exceeds_reported_battery_upper", "calibration_status",
        "verification_relative_error_percent", "verification_within_development_band",
    ), selection_review)
    write_csv("class_power_evidence.csv", (
        "vehicle_class", "generator", "vehicle_limit_status",
        "connector_compatibility_checked", "power_evidence_status",
        "sessions_with_positive_study_energy",
    ), ({
        "vehicle_class": key[0], "generator": key[1], "vehicle_limit_status": key[2],
        "connector_compatibility_checked": key[3], "power_evidence_status": key[4],
        "sessions_with_positive_study_energy": count,
    } for key, count in sorted(power_evidence.items())))
    write_csv("class_replicate_daily_energy.csv", (
        "replicate", "generator", "vehicle_class", "selected_owners", "context",
        "daily_energy_kwh", "daily_energy_per_study_vehicle_kwh",
    ), ({
        "replicate": r, "generator": g, "vehicle_class": c,
        "selected_owners": class_counts[c], "context": context,
        "daily_energy_kwh": daily[(r, g, c, context)],
        "daily_energy_per_study_vehicle_kwh":
            daily[(r, g, c, context)] / class_counts[c],
    } for r, g, c, context in keys))

    energy_summary = []
    mean_profiles = []
    comparisons = []
    for generator, vehicle_class, context in itertools.product(
        GENERATORS, classes, ALL_CONTEXTS
    ):
        count = class_counts[vehicle_class]
        average, mcse = mean_mcse(
            daily[(r, generator, vehicle_class, context)] for r in replicates
        )
        energy_summary.append({
            "generator": generator, "vehicle_class": vehicle_class,
            "selected_owners": count, "context": context, "n_replicates": n,
            "mean_daily_energy_kwh": average, "daily_energy_mcse_kwh": mcse,
            "mean_daily_energy_per_study_vehicle_kwh": average / count,
            "mcse_per_study_vehicle_kwh": divide_optional(mcse, count),
        })
        for hour in range(24):
            average_power, power_mcse = mean_mcse(
                profiles[(r, generator, vehicle_class, context)][hour]
                for r in replicates
            )
            mean_profiles.append({
                "generator": generator, "vehicle_class": vehicle_class,
                "selected_owners": count, "context": context,
                "hour_of_day": hour, "n_replicates": n,
                "mean_power_kw": average_power, "power_mcse_kw": power_mcse,
                "mean_power_per_study_vehicle_kw": average_power / count,
                "mcse_per_study_vehicle_kw": divide_optional(power_mcse, count),
            })
    for vehicle_class, context in itertools.product(classes, ALL_CONTEXTS):
        control = [daily[(r, "poisson_control", vehicle_class, context)]
                   for r in replicates]
        availability = [daily[(r, "availability_aware", vehicle_class, context)]
                        for r in replicates]
        average, mcse = mean_mcse(b - a for a, b in zip(control, availability))
        comparisons.append({
            "vehicle_class": vehicle_class, "selected_owners": class_counts[vehicle_class],
            "context": context, "n_replicates": n,
            "control_mean_daily_energy_kwh": statistics.mean(control),
            "availability_mean_daily_energy_kwh": statistics.mean(availability),
            "availability_minus_control_kwh_per_day": average,
            "paired_difference_mcse_kwh_per_day": mcse,
        })
    write_csv("class_daily_energy_summary.csv", (
        "generator", "vehicle_class", "selected_owners", "context", "n_replicates",
        "mean_daily_energy_kwh", "daily_energy_mcse_kwh",
        "mean_daily_energy_per_study_vehicle_kwh", "mcse_per_study_vehicle_kwh",
    ), energy_summary)
    write_csv("class_replicate_profiles.csv", (
        "replicate", "generator", "vehicle_class", "selected_owners", "context",
        "hour_of_day", "mean_power_kw", "mean_power_per_study_vehicle_kw",
    ), ({
        "replicate": r, "generator": g, "vehicle_class": c,
        "selected_owners": class_counts[c], "context": context, "hour_of_day": hour,
        "mean_power_kw": profiles[(r, g, c, context)][hour],
        "mean_power_per_study_vehicle_kw":
            profiles[(r, g, c, context)][hour] / class_counts[c],
    } for r, g, c, context in keys for hour in range(24)))
    write_csv("class_mean_profile_24h.csv", (
        "generator", "vehicle_class", "selected_owners", "context", "hour_of_day",
        "n_replicates", "mean_power_kw", "power_mcse_kw",
        "mean_power_per_study_vehicle_kw", "mcse_per_study_vehicle_kw",
    ), mean_profiles)
    write_csv("class_generator_comparison.csv", (
        "vehicle_class", "selected_owners", "context", "n_replicates",
        "control_mean_daily_energy_kwh", "availability_mean_daily_energy_kwh",
        "availability_minus_control_kwh_per_day", "paired_difference_mcse_kwh_per_day",
    ), comparisons)
    write_csv("aggregate_reconciliation.csv", (
        "check", "unit", "replicate", "context", "hour_of_day", "generator",
        "reconstructed_value", "saved_reference_value", "absolute_error",
    ), reconciliation)

    readme = output / "README.md"
    readme.write_text(
        "# Vehicle-class profile export\n\n"
        f"Source pilot: {relative(folder)}\n\n"
        f"Study: {days} days; warm-up: {report['warmup_days']} days; "
        f"replicates: {n}; selected owners: {len(owners)}.\n\n"
        "Vehicle classes are the original survey labels. They have not been "
        "verified as DMT administrative classes or powertrain groups.\n\n"
        "Profiles use saved accepted sessions and their effective average power "
        "as the existing constant-power test proxy. Energy is clipped to study "
        "hours [0, 24 * study_days). Warm-up carry-in and study-end carry-out "
        "are accounted for. Rejected candidates contribute no load.\n\n"
        "Each class denominator includes every selected owner in that class, "
        "including owners with no study-period charging. Classes with no selected "
        "owners receive coverage records without demand estimates.\n\n"
        "The 24 profile values are average kW in each one-hour time-of-day bin "
        "across the study days. Their sum, multiplied by one hour, equals mean "
        "daily energy in kWh.\n\n"
        "MCSE is computed across saved replicate results, conditional on the "
        "source model and screened parameter bank. It is not an empirical "
        "population confidence interval. MCSE is blank when only one replicate "
        "is available. Class means add; class MCSEs must not be added. Joint "
        "replicate outputs are retained for covariance-aware calculations.\n\n"
        "Generator differences pair saved replicate/owner selections. They do "
        "not establish common candidate-event pairing.\n\n"
        "Battery-upper flags retain True, False and missing as separate states. "
        "A missing flag does not establish a passed comparison. Missing flags "
        "remain blank in the exported CSV and are counted in the report.\n\n"
        "Calibration, verification, battery-review and power-evidence flags "
        "are retained. Exporting a class does not establish vehicle-limit or "
        "connector compatibility. Per-study-vehicle profiles do not establish "
        "nationally representative class behaviour.\n\n"
        "No simulations, random draws, fitting, source corrections, owner "
        "exclusions, fleet scaling or empirical validation were performed. "
        "Original sources and results were not changed.\n",
        encoding="utf-8",
    )
    output_files.append(readme)
    verify_unchanged()
    result_report = {
        "schema_version": "vehicle_class_profile_export_v1",
        "status": "complete", **common,
        "source_pilot": relative(folder), "features_file": relative(features_path),
        "study_days": days, "warmup_days": report["warmup_days"], "replicates": n,
        "base_seed": report["base_seed"], "owners_retained_in_features": len(features),
        "owners_simulated": len(owners),
        "class_counts_in_selected_cohort": dict(sorted(class_counts.items())),
        "classes_without_selected_owners": sorted(set(retained_counts) - set(classes)),
        "saved_accepted_session_rows": session_count,
        "owner_replicate_selections": len(selected),
        "calibration_status_counts": dict(status_counts),
        "verification_selections_outside_band": outside_band,
        "battery_upper_flag_counts": battery_flag_counts,
        "aggregate_reconciliation_checks": len(reconciliation),
        "numerical_check_tolerances": {"relative": REL_TOL, "absolute": ABS_TOL},
        "checks": {
            "recorded_source_hashes_verified": True,
            "owner_feature_links_unique_and_complete": True,
            "complete_selected_owner_grid": True,
            "calibration_flags_reproduced": True,
            "nullable_battery_flags_preserved": True,
            "saved_session_energy_partitions_reproduced": True,
            "class_profiles_integrate_to_class_daily_energy": True,
            "class_contexts_sum_to_TOTAL": True,
            "class_sums_reproduce_saved_aggregate_results": True,
            "inputs_unchanged_during_export": True,
        },
        "source_candidate_pairing": report.get("candidate_pairing"),
        "source_completed_frequency_agreement_established":
            report.get("completed_frequency_agreement_established"),
        "source_warmup_stationarity_established":
            report.get("warmup_stationarity_established"),
        "vehicle_class_fleet_mapping_verified": False,
        "vehicle_limit_and_connector_compatibility_established": False,
        "national_fleet_class_shares_established": False,
        "district_demand_ready": False, "empirical_validation": False,
        "new_demand_simulations": 0, "new_parameter_draws": 0,
        "output_row_counts": output_row_counts,
        "input_sha256": {
            relative(path): digest for path, digest in sorted(
                watched.items(), key=lambda item: str(item[0])
            )
        },
        "output_sha256": {path.name: sha256(path) for path in output_files},
    }
    (output / "vehicle_class_profile_report.json").write_text(
        json.dumps(result_report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print("Vehicle-class profile export complete.")
    print(f"owners_simulated: {len(owners)}")
    print(f"vehicle_classes_with_profiles: {len(classes)}")
    print(f"replicates: {n}")
    for vehicle_class, count in sorted(class_counts.items()):
        print(f"  {vehicle_class}: {count} owners")
    print(f"battery_upper_flag_counts: {battery_flag_counts}")
    for generator in GENERATORS:
        average = statistics.mean(
            aggregate_daily(r, generator, "TOTAL") for r in replicates
        )
        print(f"{generator}: mean_daily_energy_kwh={average:.6f}")
    print(f"aggregate_reconciliation_checks: {len(reconciliation)}")
    print("Source, owner-link, class-sum and profile-energy checks passed.")
    print(f"Output: {relative(output)}")
    print("Class decomposition only; fleet compatibility and empirical "
          "validation remain unestablished.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, csv.Error) as exc:
        raise SystemExit(f"Vehicle-class export failed: {exc}") from exc
