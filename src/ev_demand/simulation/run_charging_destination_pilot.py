"""Allocate saved synthetic Car charging profiles under explicit destination assumptions."""

import argparse
import csv
import hashlib
import itertools
import json
import math
import statistics
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CONTEXTS = ("T1", "T2", "T3", "T4")
ALL_CONTEXTS = CONTEXTS + ("TOTAL",)
GENERATORS = ("poisson_control", "availability_aware")
ANSWERS = ("I do not charge away from home", "Mostly within my home district",
           "Both within and outside my home district", "Mostly outside my home district")
BASELINE = "residence_reference"
UNRESOLVED = "OUTSIDE_UNRESOLVED"
REL_TOL, ABS_TOL = 1e-8, 1e-7


def require(condition, message):
    if not condition:
        raise ValueError(message)


def number(value):
    require(not isinstance(value, bool), "Boolean used as a numerical value.")
    result = float(value)
    require(math.isfinite(result), f"Non-finite number: {value!r}")
    return result


def boolean(value, optional=False):
    if optional and (value is None or str(value).strip() == ""):
        return None
    require(str(value) in ("True", "False"), f"Invalid boolean: {value!r}")
    return str(value) == "True"


def close(actual, reference, label):
    require(math.isclose(actual, reference, rel_tol=REL_TOL, abs_tol=ABS_TOL),
            f"{label}: {actual:.12g} versus {reference:.12g}")


def path(value):
    result = (ROOT / value).resolve()
    require(result.is_relative_to(ROOT), f"Path outside project: {result}")
    return result


def relative(value):
    return value.relative_to(ROOT).as_posix()


def digest(value):
    result = hashlib.sha256()
    with value.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def read_json(value):
    return json.loads(value.read_text(encoding="utf-8-sig"))


def read_csv(value, required):
    with value.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        columns = reader.fieldnames or []
        require(len(columns) == len(set(columns)) and set(required) <= set(columns),
                f"Missing or duplicate CSV columns: {value.name}")
        for row in reader:
            require(None not in row and all(v is not None for v in row.values()), f"Malformed row: {value.name}")
            yield row


def mean_mcse(values):
    values = list(values)
    require(len(values) >= 2, "At least two saved replicates are required for MCSE.")
    return statistics.mean(values), statistics.stdev(values) / math.sqrt(len(values))


class Sources:
    def __init__(self):
        self.hashes = {}

    def watch(self, value, expected=None):
        require(value.is_file(), f"Required source not found: {relative(value)}")
        if value not in self.hashes:
            self.hashes[value] = digest(value)
        if expected is not None:
            require(isinstance(expected, str) and len(expected) == 64
                    and self.hashes[value] == expected.lower(), f"Source hash mismatch: {relative(value)}")

    def package(self, folder, filename):
        self.watch(folder / filename)
        report = read_json(folder / filename)
        for field in ("input_sha256", "output_sha256"):
            require(isinstance(report.get(field), dict) and report[field], f"Missing manifest: {filename}/{field}")
        for name, expected in report["input_sha256"].items():
            self.watch(path(name), expected)
        for name, expected in report["output_sha256"].items():
            require(Path(name).name == name, f"Expected bare output filename: {name}")
            self.watch(path(folder / name), expected)
        return report

    def unchanged(self):
        for value, expected in self.hashes.items():
            require(digest(value) == expected, f"Source changed during processing: {relative(value)}")


def policy_cases(policy):
    require(policy.get("schema_version") == "charging_destination_fraction_policy_v1"
            and policy.get("evidence_status") == "RESEARCH_SENSITIVITY_ASSUMPTION"
            and policy.get("allocation_method") == "deterministic_fractional_energy_allocation"
            and policy.get("baseline_case") == BASELINE
            and policy.get("unresolved_destination_id") == UNRESOLVED, "Unsupported routing policy.")
    require(isinstance(policy.get("rationale"), list) and policy["rationale"], "Routing rationale is missing.")
    cases = {BASELINE: dict.fromkeys(ANSWERS, 0.0)}
    for row in policy.get("cases", []):
        name, fractions = row["case_id"], row["outside_fractions_by_answer"]
        require(isinstance(name, str) and name and name not in cases and set(fractions) == set(ANSWERS),
                "Invalid or duplicate routing case or answer categories.")
        values = {answer: number(value) for answer, value in fractions.items()}
        require(all(0 <= v <= 1 for v in values.values()) and values[ANSWERS[0]] == 0
                and values[ANSWERS[1]] < 0.5 and values[ANSWERS[3]] > 0.5,
                f"Fractions contradict the declared qualitative categories: {name}")
        cases[name] = values
    require(len(cases) == 3, "This bounded pilot expects two sensitivity cases and one residence reference.")
    return cases


def allocation_rule(owner, context, fractions, districts):
    require(context in CONTEXTS and owner["home_district_id"] in districts, "Invalid charging context or home district.")
    require(owner["away_geography_answer"] in fractions, f"Unsupported geography answer: {owner['owner_id']}")
    outside = 0.0 if context == "T2" else fractions[owner["away_geography_answer"]]
    named = owner["named_outside_district_id"]
    require(not named or (named in districts and named != owner["home_district_id"]), "Invalid named outside district.")
    require((owner["outside_answer_status"] == "named_district") == bool(named), "Outside answer/identity mismatch.")
    if not named:
        require(owner["outside_answer_status"] == "reported_not_applicable", "Unresolved source answer needs review.")
    target = named or UNRESOLVED
    return [(owner["home_district_id"], 1.0 - outside), (target, outside)]


def session_profile(row, days):
    start, end, duration, energy, power = (number(row[k]) for k in (
        "start_hour_absolute", "end_hour_absolute", "active_duration_hours", "session_energy_kwh",
        "effective_average_power_kw"))
    require(end >= start and duration >= 0 and energy >= 0 and power > 0, "Invalid session support or power.")
    close(end - start, duration, "Session duration")
    close(duration * power, energy, "Session energy")
    limit = 24.0 * days
    low, high = max(start, 0.0), min(end, limit)
    partitions = (power * max(0, min(end, 0.0) - start), power * max(0, high - low),
                  power * max(0, end - max(start, limit)))
    for name, value in zip(("energy_before_study_kwh", "energy_in_study_kwh", "energy_after_study_kwh"), partitions):
        close(number(row[name]), value, name)
    close(sum(partitions), energy, "Session energy partition")
    profile = [0.0] * 24
    if high > low:
        for hour in range(math.floor(low), math.ceil(high)):
            overlap = max(0.0, min(high, hour + 1.0) - max(low, hour))
            profile[hour % 24] += power * overlap / days
    close(sum(profile), partitions[1] / days, "Session hourly integration")
    return profile, start, end


def make_figures(output, cases, districts, profiles, summary, replicates):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    colors = ("#1f77b4", "#e69f00", "#7b3294")
    generator = "availability_aware"
    fig, axes = plt.subplots(1, 2, figsize=(13, 5.2))
    for (case, _), color in zip(cases.items(), colors):
        curve = [statistics.mean(sum(profiles[case, r, generator, d, "TOTAL"][h]
                 for d in districts) for r in replicates) for h in range(24)]
        axes[0].plot(range(24), curve, color=color, label=case.replace("_", " "),
                     linestyle="-" if case == BASELINE else "--")
    axes[0].set(title="Cohort total is conserved (curves overlap)", xlabel="Hour", ylabel="Mean power (kW)")
    axes[0].set_xticks(range(0, 24, 3))
    axes[0].legend(fontsize=8)
    high = list(cases)[-1]
    top = sorted((d for d in districts if d != UNRESOLVED), key=lambda d: abs(
        summary[high, generator, d, "TOTAL"]["difference_vs_residence_kwh_day"]), reverse=True)[:5]
    top.append(UNRESOLVED)
    for i, (case, color) in enumerate(zip(cases, colors)):
        axes[1].barh([j + (i - 1) * 0.24 for j in range(len(top))],
                     [summary[case, generator, d, "TOTAL"]["mean_daily_energy_kwh"] for d in top],
                     height=0.23, color=color, label=case.replace("_", " "))
    axes[1].set_yticks(range(len(top)), [districts[d] for d in top])
    axes[1].set(title="Largest district changes and unresolved demand", xlabel="Mean daily energy (kWh/day)")
    axes[1].invert_yaxis()
    fig.suptitle("32-Car synthetic study cohort: conditional destination allocation", fontsize=13)
    fig.text(0.01, 0.01, "Saved availability-aware sessions; fixed routing assumptions. Outside unresolved is not a district.", fontsize=8)
    fig.tight_layout(rect=(0, 0.04, 1, 0.94))
    for extension in ("png", "svg"):
        fig.savefig(output / f"destination_allocation_overview.{extension}", dpi=160)
    plt.close(fig)


def run(args):
    started = time.perf_counter()
    watched = Sources()
    watched.watch(Path(__file__).resolve())
    geo_folder, pilot_folder, class_folder, policy_path = map(path, (args.geography, args.pilot, args.class_profiles, args.policy))
    print("Verifying geography, saved pilot and Car-profile source packages...", flush=True)
    geo = watched.package(geo_folder, "charging_geography_resolved_report.json")
    require(geo.get("schema_version") == "charging_geography_resolved_v1" and geo.get("status") == "resolved"
            and geo.get("data_origin") == "SYNTHETIC" and not any(geo["review_flags"].values()), "Geography review is incomplete.")
    pilot_report_path, class_report_path = pilot_folder / "availability_generator_report.json", class_folder / "vehicle_class_profile_report.json"
    require(all(relative(p) in geo["input_sha256"] for p in (pilot_report_path, class_report_path)),
            "Pilot and class export must be linked to the saved geography provenance.")
    pilot = watched.package(pilot_folder, pilot_report_path.name)
    exported = watched.package(class_folder, class_report_path.name)
    require(pilot.get("schema_version") == "availability_generator_comparison_v1" and pilot.get("status") == "complete"
            and pilot.get("behaviour_input_origin") == "SYNTHETIC" and pilot.get("output_origin") == "SIMULATED",
            "Unsupported saved availability pilot.")
    require(exported.get("schema_version") == "vehicle_class_profile_export_v1" and exported.get("status") == "complete"
            and exported.get("source_pilot") == relative(pilot_folder), "Class export belongs to a different pilot.")
    for report in (geo, pilot, exported):
        require(report.get("checks") and all(v is True for v in report["checks"].values()), "An upstream source check did not pass.")
    require(pilot["energy_case"] == "bounded_uniform_test" and pilot["power_scenario"] == "reduced_test"
            and pilot["cohort"] == "common_energy_cases" and pilot["replicates"] == 10
            and pilot["study_days"] == 28 and pilot["warmup_days"] == 7, "Use the frozen one-case PP1 pilot settings.")
    days, replicates = pilot["study_days"], tuple(str(i) for i in range(1, pilot["replicates"] + 1))
    require(all(exported[k] == pilot[k] for k in ("energy_case", "power_scenario", "cohort", "replicates", "study_days", "warmup_days")),
            "Pilot/class study settings differ.")
    watched.watch(policy_path)
    policy = read_json(policy_path)
    cases = policy_cases(policy)
    registry_paths = [path(name) for name in geo["input_sha256"] if Path(name).name == "district_registry.csv"]
    require(len(registry_paths) == 1, "Expected one hash-bound district registry.")
    registry = list(read_csv(registry_paths[0], ("district_id", "district_name")))
    districts = {row["district_id"]: row["district_name"] for row in registry}
    require(len(districts) == len(registry) == 25 and UNRESOLVED not in districts, "Expected 25 unique districts.")
    rows = list(read_csv(geo_folder / "owner_geography_resolved.csv", ("owner_id", "vehicle_class", "screened_cohort",
        "in_saved_Car_profiles", "data_origin", "home_district_id", "away_geography_answer", "outside_district_answer", "named_outside_district_id", "outside_answer_status")))
    require(len(rows) == geo["owners_retained"] and len({r["owner_id"] for r in rows}) == len(rows), "Owner denominator mismatch.")
    cars = {r["owner_id"]: r for r in rows if boolean(r["in_saved_Car_profiles"])}
    require(len(cars) == geo["profile_Car_owners"] == 32 and all(r["vehicle_class"] == "Car"
            and r["screened_cohort"] == "common_energy_cases" and r["data_origin"] == "SYNTHETIC" for r in cars.values()),
            "Expected the unchanged 32-owner common Car cohort.")
    links = list(read_csv(class_folder / "class_owner_links.csv", ("owner_id", "vehicle_class", "owner_replicate_selections")))
    require(len({r["owner_id"] for r in links}) == len(links) and {r["owner_id"] for r in links if r["vehicle_class"] == "Car"} == set(cars),
            "Saved class export and geography Car memberships differ.")
    require(exported["class_counts_in_selected_cohort"]["Car"] == 32, "Class export denominator differs.")
    selected = list(read_csv(pilot_folder / "selected_owner_parameters.csv", ("owner_id", "replicate", "energy_case", "screen_cohort")))
    selected_grid = {(r["replicate"], r["owner_id"]) for r in selected}
    require(len(selected_grid) == len(selected) and selected_grid == set(itertools.product(replicates, pilot["owner_ids"])),
            "Saved owner/replicate selection grid differs.")
    require(set(cars) <= set(pilot["owner_ids"]) and all(r["energy_case"] == pilot["energy_case"]
            and r["screen_cohort"] == pilot["cohort"] for r in selected), "Saved selection scope differs.")
    rules = {}
    for case, fractions in cases.items():
        for owner_id, owner in cars.items():
            for context in CONTEXTS:
                rule = allocation_rule(owner, context, fractions, districts)
                close(sum(f for _, f in rule), 1.0, "Allocation fractions")
                rules[case, owner_id, context] = rule
    owner_profiles = {(r, g, o, c): [0.0] * 24 for r, g, o, c in itertools.product(replicates, GENERATORS, cars, CONTEXTS)}
    intervals, session_ids = defaultdict(list), set()
    session_rows, car_session_rows = 0, 0
    columns = ("generator", "replicate", "owner_id", "session_id", "context", "output_origin", "behaviour_input_origin",
        "data_origin", "start_hour_absolute", "end_hour_absolute", "active_duration_hours", "session_energy_kwh",
        "effective_average_power_kw", "energy_before_study_kwh", "energy_in_study_kwh", "energy_after_study_kwh")
    print("Reconstructing the saved Car sessions, including study-boundary overlaps...", flush=True)
    for row in read_csv(pilot_folder / "sessions.csv", columns):
        session_rows += 1
        if row["owner_id"] not in cars:
            continue
        car_session_rows += 1
        r, g, o, c = (row[k] for k in ("replicate", "generator", "owner_id", "context"))
        require((r, g, o, c) in owner_profiles and (r, o) in selected_grid, "Unknown Car session selection or context.")
        require(row["output_origin"] == "SIMULATED" and row["behaviour_input_origin"] == row["data_origin"] == "SYNTHETIC",
                "Car session origin mismatch.")
        key = (r, g, row["session_id"])
        require(row["session_id"] and key not in session_ids, "Duplicate or empty Car session ID.")
        session_ids.add(key)
        curve, start, end = session_profile(row, days)
        if cars[o]["away_geography_answer"] == ANSWERS[0] and c != "T2":
            require(number(row["session_energy_kwh"]) == 0, "No-away report conflicts with a saved non-home session.")
        for h, value in enumerate(curve):
            owner_profiles[r, g, o, c][h] += value
        if end > start:
            intervals[r, g, o].append((start, end))
    require(car_session_rows > 0, "No saved Car sessions found.")
    for key, values in intervals.items():
        values.sort()
        require(all(a[1] <= b[0] + 1e-9 for a, b in zip(values, values[1:])), f"Saved owner session overlap: {key}")
    reference = {(r, g, c): [sum(owner_profiles[r, g, o, c][h] for o in cars) for h in range(24)]
                 for r, g, c in itertools.product(replicates, GENERATORS, CONTEXTS)}
    for r, g in itertools.product(replicates, GENERATORS):
        reference[r, g, "TOTAL"] = [sum(reference[r, g, c][h] for c in CONTEXTS) for h in range(24)]
    reconciliation = []

    def check(kind, case, r, g, c, h, actual, expected):
        close(actual, expected, f"{kind}/{case}/{r}/{g}/{c}/{h}")
        reconciliation.append({"check": kind, "allocation_case": case, "replicate": r, "generator": g,
            "context": c, "hour_of_day": h, "actual": actual, "reference": expected, "absolute_error": abs(actual - expected)})

    for filename, hourly in (("class_replicate_profiles.csv", True), ("class_replicate_daily_energy.csv", False)):
        required = ("replicate", "generator", "vehicle_class", "selected_owners", "context", "mean_power_kw", "hour_of_day") if hourly else (
            "replicate", "generator", "vehicle_class", "selected_owners", "context", "daily_energy_kwh")
        seen = set()
        for row in read_csv(class_folder / filename, required):
            if row["vehicle_class"] != "Car":
                continue
            r, g, c = row["replicate"], row["generator"], row["context"]
            h = int(row["hour_of_day"]) if hourly else ""
            key = (r, g, c, h)
            require(key not in seen and (r, g, c) in reference and int(row["selected_owners"]) == 32
                    and (not hourly or 0 <= h < 24), "Invalid saved Car reference grid.")
            seen.add(key)
            actual = reference[r, g, c][h] if hourly else sum(reference[r, g, c])
            check("source_car_hourly" if hourly else "source_car_daily", BASELINE, r, g, c, h, actual,
                  number(row["mean_power_kw"] if hourly else row["daily_energy_kwh"]))
        require(seen == {(r, g, c, h) for r, g, c in reference for h in (range(24) if hourly else ("",))},
                "Incomplete saved Car reference grid.")
    districts[UNRESOLVED] = "Outside district unresolved"
    profiles = {(case, r, g, d, c): [0.0] * 24 for case, r, g, d, c in
                itertools.product(cases, replicates, GENERATORS, districts, ALL_CONTEXTS)}
    outside_energy = defaultdict(float)
    for (r, g, o, c), curve in owner_profiles.items():
        for case in cases:
            for d, fraction in rules[case, o, c]:
                for h, value in enumerate(curve):
                    profiles[case, r, g, d, c][h] += fraction * value
                    profiles[case, r, g, d, "TOTAL"][h] += fraction * value
            outside_energy[case, r, g, c] += sum(curve) * rules[case, o, c][1][1]
            outside_energy[case, r, g, "TOTAL"] += sum(curve) * rules[case, o, c][1][1]
    for case, r, g, c in itertools.product(cases, replicates, GENERATORS, ALL_CONTEXTS):
        for h in range(24):
            check("all_destinations_hourly", case, r, g, c, h,
                  sum(profiles[case, r, g, d, c][h] for d in districts), reference[r, g, c][h])
        check("all_destinations_daily", case, r, g, c, "",
              sum(sum(profiles[case, r, g, d, c]) for d in districts), sum(reference[r, g, c]))
    for case, r, g, d in itertools.product(cases, replicates, GENERATORS, districts):
        for h in range(24):
            close(sum(profiles[case, r, g, d, c][h] for c in CONTEXTS), profiles[case, r, g, d, "TOTAL"][h], "Context sum")
            close(profiles[case, r, g, d, "T2"][h], profiles[BASELINE, r, g, d, "T2"][h], "Home context unchanged")
    mean_profiles, daily, routing = [], {}, []
    for case, g, d, c in itertools.product(cases, GENERATORS, districts, ALL_CONTEXTS):
        curves = [profiles[case, r, g, d, c] for r in replicates]
        mean_curve = [statistics.mean(curve[h] for curve in curves) for h in range(24)]
        average, mcse = mean_mcse(sum(curve) for curve in curves)
        difference, difference_mcse = mean_mcse(sum(profiles[case, r, g, d, c]) - sum(profiles[BASELINE, r, g, d, c]) for r in replicates)
        close(sum(mean_curve), average, "Mean profile integration")
        daily[case, g, d, c] = {"allocation_case": case, "generator": g, "district_id": d, "district_name": districts[d],
            "context": c, "mean_daily_energy_kwh": average, "daily_energy_mcse_kwh": mcse,
            "difference_vs_residence_kwh_day": difference, "paired_difference_mcse_kwh_day": difference_mcse,
            "peak_of_mean_profile_kw": max(mean_curve), "peak_hours_json": json.dumps([h for h, v in enumerate(mean_curve) if v == max(mean_curve) and v > 0])}
        for h in range(24):
            power, power_mcse = mean_mcse(curve[h] for curve in curves)
            mean_profiles.append({"allocation_case": case, "generator": g, "district_id": d, "district_name": districts[d],
                "context": c, "hour_of_day": h, "mean_power_kw": power, "power_mcse_kw": power_mcse})
    for case, g, c in itertools.product(cases, GENERATORS, ALL_CONTEXTS):
        total, mcse = mean_mcse(sum(reference[r, g, c]) for r in replicates)
        unresolved, unresolved_mcse = mean_mcse(sum(profiles[case, r, g, UNRESOLVED, c]) for r in replicates)
        located, located_mcse = mean_mcse(sum(sum(profiles[case, r, g, d, c]) for d in districts if d != UNRESOLVED) for r in replicates)
        outside, outside_mcse = mean_mcse(outside_energy[case, r, g, c] for r in replicates)
        close(located + unresolved, total, "Located plus unresolved energy")
        routing.append({"allocation_case": case, "generator": g, "context": c,
            "cohort_mean_daily_energy_kwh": total, "cohort_daily_energy_mcse_kwh": mcse,
            "located_mean_daily_energy_kwh": located, "located_daily_energy_mcse_kwh": located_mcse,
            "unresolved_outside_mean_daily_energy_kwh": unresolved, "unresolved_outside_daily_energy_mcse_kwh": unresolved_mcse,
            "outside_home_mean_daily_energy_kwh": outside, "outside_home_daily_energy_mcse_kwh": outside_mcse,
            "outside_share_of_context_energy": outside / total if total else ""})
    reviews = [r for r in read_csv(class_folder / "class_selection_review.csv", ("owner_id", "replicate", "vehicle_class",
        "exceeds_reported_battery_upper", "calibration_status", "verification_within_development_band")) if r["owner_id"] in cars]
    require(len(reviews) == len({(r["replicate"], r["owner_id"]) for r in reviews}) == len(cars) * len(replicates)
            and {(r["replicate"], r["owner_id"]) for r in reviews} == set(itertools.product(replicates, cars)), "Incomplete Car source review grid.")
    source_flags = {"owner_parameter_selections": len(reviews),
        "calibration_review_selections": sum(r["calibration_status"] != "proxy_target_matched" for r in reviews),
        "verification_outside_development_band": sum(not boolean(r["verification_within_development_band"]) for r in reviews),
        "missing_battery_upper_flags": sum(boolean(r["exceeds_reported_battery_upper"], optional=True) is None for r in reviews),
        "positive_battery_upper_flags": sum(boolean(r["exceeds_reported_battery_upper"], optional=True) is True for r in reviews)}
    if not args.skip_figures:
        try:
            import matplotlib
        except ImportError as error:
            raise ValueError("Matplotlib is unavailable. Use --skip-figures to export the checked tables without figures.") from error
    watched.unchanged()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    output = path(args.output_root) / f"charging_destination_pilot_{stamp}"
    output.mkdir(parents=True, exist_ok=False)
    common = {"study_car_owners": 32, "n_replicates": len(replicates), "behaviour_input_origin": "SYNTHETIC",
              "output_origin": "DERIVED_FROM_SAVED_SIMULATED_SESSIONS", "routing_input_origin": "RESEARCH_SENSITIVITY_ASSUMPTION"}
    row_counts = {}

    def write(name, fields, records):
        count = 0
        with (output / name).open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(dict.fromkeys([*fields, *common])))
            writer.writeheader()
            for row in records:
                writer.writerow({**row, **common})
                count += 1
        row_counts[name] = count

    write("district_mean_profiles_24h.csv", list(mean_profiles[0]), mean_profiles)
    write("district_daily_summary.csv", list(next(iter(daily.values()))), daily.values())
    write("routing_summary.csv", list(routing[0]), routing)
    write("energy_conservation_checks.csv", list(reconciliation[0]), reconciliation)
    write("district_replicate_profiles.csv", ("allocation_case", "replicate", "generator", "district_id", "context", "hour_of_day", "mean_power_kw"),
          ({"allocation_case": case, "replicate": r, "generator": g, "district_id": d, "context": c, "hour_of_day": h, "mean_power_kw": curve[h]}
           for (case, r, g, d, c), curve in profiles.items() for h in range(24)))
    write("owner_context_allocation_rules.csv", ("allocation_case", "owner_id", "context", "away_geography_answer", "home_district_id",
          "raw_named_outside_answer", "outside_district_id", "home_fraction", "outside_fraction"),
          ({"allocation_case": case, "owner_id": o, "context": c, "away_geography_answer": cars[o]["away_geography_answer"],
            "home_district_id": rule[0][0], "raw_named_outside_answer": cars[o]["outside_district_answer"],
            "outside_district_id": rule[1][0], "home_fraction": rule[0][1], "outside_fraction": rule[1][1]}
           for (case, o, c), rule in rules.items()))
    write("source_Car_selection_review.csv", list(reviews[0]), reviews)
    (output / "routing_policy_snapshot.json").write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
    if not args.skip_figures:
        print("Rendering the conditional allocation comparison...", flush=True)
        make_figures(output, cases, districts, profiles, daily, replicates)
    totals = [r for r in routing if r["context"] == "TOTAL"]
    lines = ["# PP1 conditional charging-destination pilot", "", "## Scope", "",
        f"32 original synthetic study Car owners; {days} study days; {pilot['warmup_days']} warm-up days; {len(replicates)} saved replicates.",
        "One energy/power case: bounded_uniform_test / reduced_test. Both saved generators are retained.",
        "This step allocates existing simulated energy. It creates no new demand simulations, owners, parameter draws or trained models.",
        "", "## Computed energy allocation", "",
        "| Generator | Allocation case | Cohort kWh/day | Located kWh/day | Outside unresolved kWh/day |",
        "|---|---|---:|---:|---:|"]
    lines += [f"| {r['generator']} | {r['allocation_case']} | {r['cohort_mean_daily_energy_kwh']:.3f} | "
              f"{r['located_mean_daily_energy_kwh']:.3f} | {r['unresolved_outside_mean_daily_energy_kwh']:.3f} |" for r in totals]
    lines += ["", "## Interpretation", "", *["- " + s for s in policy["rationale"]], "",
        "- Located district demand plus unresolved outside demand reproduces the original cohort at every hour and replicate.",
        "- The unresolved row is an accounting category, not a 26th district. Located districts alone can sum below the cohort total.",
        "- MCSE describes saved-replicate mean precision under a fixed policy. It excludes routing-policy uncertainty and population sampling uncertainty.",
        "- T1/T2/T3/T4 retain survey context meanings. No commercial/residential/tourist/highway geographic-typology crosswalk is activated.",
        "", "## Original model review flags retained", "", "```json", json.dumps(source_flags, indent=2), "```", "",
        "Original completed-frequency agreement: " + str(pilot.get("completed_frequency_agreement_established")),
        "Original warm-up stationarity established: " + str(pilot.get("warmup_stationarity_established")),
        "", "## Stopping point", "", "The destination sensitivity pilot is complete when the checks in the JSON report pass.",
        "Real-data collection, refitting, scheduling validation, geographic-typology mapping, production precision experiments,",
        "future-fleet destination allocation and N-BEATSx evaluation remain later research tasks.", "",
        "## Recorded sources", "", f"- Geography: {relative(geo_folder)}", f"- Saved pilot: {relative(pilot_folder)}",
        f"- Class export: {relative(class_folder)}", f"- Policy: {relative(policy_path)}", ""]
    (output / "PP1_RESULTS.md").write_text("\n".join(lines), encoding="utf-8")
    bundle = {"schema_version": "pp1_destination_demo_v1", **common, "allocation_scope": "32_original_study_cars",
        "national_forecast_ready": False, "empirical_validation": False, "policy": policy,
        "routing_summary": routing, "district_daily_summary": list(daily.values()), "mean_profiles": mean_profiles}
    (output / "demo_bundle.json").write_text(json.dumps(bundle, separators=(",", ":")) + "\n", encoding="utf-8")
    watched.unchanged()
    result = {"schema_version": "charging_destination_pilot_v1", "status": "complete", **common,
        "study_days": days, "warmup_days": pilot["warmup_days"], "energy_case": pilot["energy_case"], "power_scenario": pilot["power_scenario"],
        "allocation_cases": list(cases), "districts": 25, "unresolved_accounting_categories": 1,
        "saved_session_rows_scanned": session_rows, "saved_Car_session_rows": car_session_rows,
        "conservation_checks": len(reconciliation), "maximum_absolute_conservation_error": max(r["absolute_error"] for r in reconciliation),
        "source_Car_review_flags": source_flags, "source_completed_frequency_agreement_established": pilot.get("completed_frequency_agreement_established"),
        "source_warmup_stationarity_established": pilot.get("warmup_stationarity_established"),
        "checks": {"source_hashes_verified": True, "Car_owner_set_unchanged": True, "source_Car_profiles_reproduced": True,
            "saved_session_no_overlap": True, "session_energy_partitions_reproduced": True, "hourly_and_daily_energy_conserved": True,
            "home_charging_retained_at_residence": True, "context_sums_reconciled": True, "source_review_flags_retained": True, "inputs_unchanged": True},
        "complete_charging_destinations_identified": False, "geographic_place_mapping_activated": False,
        "national_forecast_ready": False, "empirical_validation": False, "eligibility_changed": False,
        "new_demand_simulations": 0, "new_parameter_draws": 0, "new_routing_random_draws": 0, "AI_models_trained": 0,
        "output_row_counts": row_counts, "elapsed_seconds": time.perf_counter() - started,
        "input_sha256": {relative(p): d for p, d in sorted(watched.hashes.items(), key=lambda item: str(item[0]))},
        "output_sha256": {p.name: digest(p) for p in sorted(output.iterdir()) if p.is_file()}}
    (output / "charging_destination_pilot_report.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print("Conditional charging-destination pilot complete.")
    for row in totals:
        print(f"{row['generator']} | {row['allocation_case']}: cohort={row['cohort_mean_daily_energy_kwh']:.3f}, "
              f"located={row['located_mean_daily_energy_kwh']:.3f}, outside_unresolved={row['unresolved_outside_mean_daily_energy_kwh']:.3f} kWh/day")
    print(f"Conservation checks passed: {len(reconciliation)}; maximum absolute error={result['maximum_absolute_conservation_error']:.3g}")
    print(f"Original Car review flags retained: {source_flags}")
    print(f"Output: {relative(output)}")
    print("PP1 stopping point: conditional allocation checked; real-data validation and production modelling remain pending.")
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--geography", required=True)
    parser.add_argument("--pilot", required=True)
    parser.add_argument("--class-profiles", required=True)
    parser.add_argument("--policy", default="config/synthetic/charging_destination_policy_v1.json")
    parser.add_argument("--output-root", default="data/processed/synthetic")
    parser.add_argument("--skip-figures", action="store_true")
    return run(parser.parse_args())


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError, csv.Error) as error:
        raise SystemExit(f"Charging-destination pilot failed: {error}") from error
