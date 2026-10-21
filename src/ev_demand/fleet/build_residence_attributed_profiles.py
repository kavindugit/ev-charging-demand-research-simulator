"""Apply saved per-Car profiles to conditional residence-allocation candidates."""

import argparse
import csv
import itertools
import json
import math
import statistics
import sys
from datetime import datetime, timezone
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
from ev_demand.geography import build_district_population_allocation as inputs

require, resolve, rel, sha = inputs.require, inputs.resolve, inputs.rel, inputs.sha
GENERATORS = ("poisson_control", "availability_aware")
CONTEXTS = ("T1", "T2", "T3", "T4", "TOTAL")
ENERGIES = ("bounded_uniform_test", "conditional_gamma_test", "conditional_lognormal_test")
POWERS = ("reduced_test", "rated_ceiling_test")
CASES = tuple(itertools.product(ENERGIES, POWERS))
ALLOCATIONS = ("equal_district_test", "population_2024_test")


def read_csv(path, required):
    rows = inputs.read_csv(path, required)
    require(all(None not in row and all(v is not None for v in row.values()) for row in rows),
            f"Malformed CSV row: {rel(path)}")
    return rows


def number(value):
    result = float(value)
    require(math.isfinite(result) and result >= 0, f"Invalid nonnegative number: {value}")
    return result


def close(actual, expected, label):
    require(math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-7),
            f"Reconciliation failed: {label}: {actual} versus {expected}")


def mean_mcse(values):
    return statistics.mean(values), statistics.stdev(values) / math.sqrt(len(values))


def checked_package(sources, folder, filename, schema, status):
    report = sources.package(folder, filename)
    require(report.get("schema_version") == schema and report.get("status") == status
            and report.get("checks") and all(v is True for v in report["checks"].values()),
            f"Unsupported or unchecked source package: {rel(folder)}")
    return report


def load_basis(folder, report):
    n, owners, reference_count = (report[k] for k in
        ("source_replicates_per_case", "study_car_owners", "reference_cohort_vehicles"))
    require(type(n) is int and n >= 2 and type(owners) is int and owners > 0
            and type(reference_count) is int and reference_count > 0,
            "Invalid source replicate count or Car denominators.")
    require(report.get("behaviour_input_origin") == "SYNTHETIC"
            and report.get("output_origin") == "SIMULATED" and report.get("class_export_cases") == 6
            and report.get("source_evidence_id") == "DMT2025_CAR_E_NEW",
            "Expected the six-case synthetic-behaviour Car reference package.")
    for name in ("cohort_replicate_profiles.csv", "cohort_mean_profiles_24h.csv", "cohort_daily_summary.csv",
                 "reference_cohort_scope.json", "source_car_selection_review.csv"):
        require(name in report["output_sha256"], f"Unbound source output: {name}")
    scope = inputs.read_json(folder / "reference_cohort_scope.json")
    require(scope.get("count_basis") == "registration_cohort"
            and scope.get("study_car_owners") == owners
            and scope.get("source_replicates_per_case") == n,
            "Source reference-cohort scope mismatch.")
    grid = {}
    for row in read_csv(folder / "cohort_replicate_profiles.csv", (
        "energy_case", "power_scenario", "replicate", "generator", "context", "hour_of_day",
        "conditional_mean_power_kw", "study_car_owners", "reference_cohort_vehicles", "n_replicates",
        "vehicle_class", "output_origin", "behaviour_input_origin")):
        key = (row["energy_case"], row["power_scenario"], int(row["replicate"]),
               row["generator"], row["context"], int(row["hour_of_day"]))
        require(key not in grid and int(row["study_car_owners"]) == owners
                and int(row["reference_cohort_vehicles"]) == reference_count and int(row["n_replicates"]) == n
                and row["vehicle_class"] == "Car" and row["output_origin"] == "SIMULATED"
                and row["behaviour_input_origin"] == "SYNTHETIC", "Duplicate source cell or scope mismatch.")
        grid[key] = number(row["conditional_mean_power_kw"]) / reference_count
    expected = set(itertools.product(ENERGIES, POWERS, range(1, n + 1), GENERATORS, CONTEXTS, range(24)))
    require(set(grid) == expected, "Incomplete per-Car source replicate grid.")
    for energy, power, rep, generator, hour in itertools.product(ENERGIES, POWERS, range(1, n + 1), GENERATORS, range(24)):
        close(sum(grid[energy, power, rep, generator, context, hour] for context in CONTEXTS[:4]),
              grid[energy, power, rep, generator, "TOTAL", hour], "source context-to-TOTAL power")
    profiles, daily, vectors = {}, {}, {}
    for energy, power, generator, context in itertools.product(ENERGIES, POWERS, GENERATORS, CONTEXTS):
        key = (energy, power, generator, context)
        profiles[key] = [mean_mcse([grid[energy, power, rep, generator, context, hour]
                                   for rep in range(1, n + 1)]) for hour in range(24)]
        vectors[key] = [sum(grid[energy, power, rep, generator, context, hour] for hour in range(24))
                        for rep in range(1, n + 1)]
        daily[key] = mean_mcse(vectors[key])
        close(sum(v[0] for v in profiles[key]), daily[key][0], "per-Car profile energy")
    seen = set()
    for row in read_csv(folder / "cohort_mean_profiles_24h.csv", (
        "energy_case", "power_scenario", "generator", "context", "hour_of_day",
        "conditional_mean_power_kw", "scaled_source_mcse_kw")):
        key = (row["energy_case"], row["power_scenario"], row["generator"], row["context"])
        cell = (*key, int(row["hour_of_day"]))
        require(key in profiles and 0 <= cell[-1] < 24 and cell not in seen, "Unexpected mean-profile cell.")
        seen.add(cell)
        mean, mcse = profiles[key][cell[-1]]
        close(number(row["conditional_mean_power_kw"]), mean * reference_count, "saved mean profile")
        close(number(row["scaled_source_mcse_kw"]), mcse * reference_count, "saved hourly MCSE")
    require(len(seen) == 1440, "Incomplete source mean profiles.")
    seen = set()
    for row in read_csv(folder / "cohort_daily_summary.csv", (
        "energy_case", "power_scenario", "generator", "context", "mean_daily_energy_per_study_car_kwh",
        "conditional_mean_daily_energy_kwh", "scaled_source_daily_mcse_kwh")):
        key = (row["energy_case"], row["power_scenario"], row["generator"], row["context"])
        require(key in daily and key not in seen, "Unexpected daily-summary cell.")
        seen.add(key)
        mean, mcse = daily[key]
        close(number(row["mean_daily_energy_per_study_car_kwh"]), mean, "saved per-Car daily energy")
        close(number(row["conditional_mean_daily_energy_kwh"]), mean * reference_count, "saved daily energy")
        close(number(row["scaled_source_daily_mcse_kwh"]), mcse * reference_count, "saved daily MCSE")
    require(set(seen) == set(daily), "Incomplete source daily summaries.")
    return grid, profiles, daily, vectors, n, owners


def load_allocations(folder, report):
    require(report.get("vehicle_class_scope") == "Car" and report.get("fleet_contract_activated") is False,
            "Expected unactivated Car allocation comparisons.")
    for name in ("district_allocation_candidates.csv", "district_car_count_candidates.csv"):
        require(name in report["output_sha256"], f"Unbound allocation file: {name}")
    weights, identities = {}, {}
    for row in read_csv(folder / "district_allocation_candidates.csv", (
        "allocation_case", "district_id", "district_name", "province_name", "share_numerator", "share_denominator")):
        key = (row["allocation_case"], row["district_id"])
        identity = (row["district_name"], row["province_name"])
        require(key[0] in ALLOCATIONS and key not in weights
                and (key[1] not in identities or identities[key[1]] == identity), "Allocation identity mismatch.")
        identities[key[1]] = identity
        share = Fraction(int(row["share_numerator"]), int(row["share_denominator"]))
        require(0 <= share <= 1, "Invalid allocation share.")
        weights[key] = share
    require(len(identities) == 25 and set(weights) == set(itertools.product(ALLOCATIONS, identities)),
            "Incomplete district allocation-weight grid.")
    for case in ALLOCATIONS:
        require(sum(weights[case, district] for district in identities) == 1, "Allocation shares must sum exactly to one.")
    rows, national, seen = [], {}, set()
    for row in read_csv(folder / "district_car_count_candidates.csv", (
        "allocation_case", "district_id", "district_name", "province_name", "year", "scenario", "vehicle_class_scope",
        "national_conditional_car_count", "district_conditional_car_count", "share_numerator", "share_denominator",
        "count_representation", "count_basis", "allocation_basis", "candidate_origin", "value_status")):
        case, district, year, scenario = row["allocation_case"], row["district_id"], int(row["year"]), row["scenario"]
        cell = (case, district, year, scenario)
        require(cell not in seen and (case, district) in weights and year in inputs.YEARS and scenario in inputs.SCENARIOS
                and identities[district] == (row["district_name"], row["province_name"]), "Unexpected district-count cell.")
        seen.add(cell)
        require(row["vehicle_class_scope"] == "Car" and row["count_representation"] == "expected_vehicles"
                and row["count_basis"] == "conditional_scenario_stock"
                and row["allocation_basis"] == "conditional_owner_residence_proxy"
                and row["candidate_origin"] == "RESEARCH_SCENARIO_ASSUMPTION"
                and row["value_status"] == "draft_study_assumption", "District-count scope mismatch.")
        value = inputs.finite_decimal(row["national_conditional_car_count"])
        require(value >= 0 and ((year, scenario) not in national or national[year, scenario] == value),
                "Inconsistent national candidate count.")
        national[year, scenario] = value
        share = Fraction(int(row["share_numerator"]), int(row["share_denominator"]))
        exact = Fraction(value) * share
        require(share == weights[case, district] and inputs.decimal_text(exact, 6) == row["district_conditional_car_count"],
                "District candidate count does not reproduce its exact allocation rule.")
        rows.append((row, exact))
    require(seen == set(itertools.product(ALLOCATIONS, identities, inputs.YEARS, inputs.SCENARIOS)),
            "Expected 1500 district candidate cells across two allocation cases.")
    return rows, national


def profile_record(count, energy, power, generator, profiles, daily):
    factor = float(count)
    key = (energy, power, generator, "TOTAL")
    values = [factor * mean for mean, _ in profiles[key]]
    peak = max(values)
    result = {"energy_case": energy, "power_scenario": power, "generator": generator,
              "mean_daily_energy_kwh": factor * daily[key][0], "scaled_source_daily_mcse_kwh": factor * daily[key][1],
              "peak_of_mean_profile_kw": peak, "mean_profile_peak_hours_json": json.dumps(
                  [hour for hour, value in enumerate(values) if math.isclose(value, peak, rel_tol=1e-10, abs_tol=1e-10)])}
    for context in CONTEXTS[:4]:
        result[f"{context}_mean_daily_kwh"] = factor * daily[energy, power, generator, context][0]
        result[f"{context}_scaled_source_daily_mcse_kwh"] = factor * daily[energy, power, generator, context][1]
    result.update({f"mean_H{hour:02d}_kw": value for hour, value in enumerate(values)})
    result.update({f"source_mcse_H{hour:02d}_kw": factor * value[1] for hour, value in enumerate(profiles[key])})
    close(sum(values), result["mean_daily_energy_kwh"], "exported TOTAL profile energy")
    close(sum(result[f"{context}_mean_daily_kwh"] for context in CONTEXTS[:4]),
          result["mean_daily_energy_kwh"], "exported context daily energy")
    return result


def write_table(output, name, rows):
    with (output / name).open("w", encoding="utf-8", newline="") as stream:
        iterator = iter(rows)
        first = next(iterator)
        writer = csv.DictWriter(stream, fieldnames=list(first))
        writer.writeheader()
        writer.writerow(first)
        writer.writerows(iterator)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allocation", required=True)
    parser.add_argument("--reference-cohort", required=True)
    parser.add_argument("--output-root", default="data/processed/synthetic")
    args = parser.parse_args()
    sources = inputs.Sources()
    sources.watch(Path(__file__).resolve())
    sources.watch(Path(inputs.__file__).resolve())
    allocation_folder, cohort_folder = resolve(args.allocation), resolve(args.reference_cohort)
    print("Verifying allocation, reference-cohort and recorded upstream hashes...")
    allocation = checked_package(sources, allocation_folder, "district_allocation_report.json",
                                 "district_population_allocation_candidates_v1", "draft_comparisons_prepared")
    require(rel(cohort_folder / "reference_car_cohort_report.json") in allocation["input_sha256"],
            "The allocation scenarios are not bound to the supplied reference-cohort package.")
    cohort = checked_package(sources, cohort_folder, "reference_car_cohort_report.json", "reference_car_cohort_transfer_v1", "complete")
    grid, profiles, daily, vectors, n, owners = load_basis(cohort_folder, cohort)
    rows, national_counts = load_allocations(allocation_folder, allocation)
    print(f"Source basis checked: {owners} study Car owners, {n} replicates per case, six energy/power cases.")
    national_profiles, national_lookup = [], {}
    for (year, scenario), count in national_counts.items():
        for (energy, power), generator in itertools.product(CASES, GENERATORS):
            record = {"year": year, "scenario": scenario, "national_conditional_cars": str(count),
                      **profile_record(Fraction(count), energy, power, generator, profiles, daily)}
            national_profiles.append(record)
            national_lookup[year, scenario, energy, power, generator] = record
    reconciliation = []
    for case, year, scenario, (energy, power), generator in itertools.product(
            ALLOCATIONS, inputs.YEARS, inputs.SCENARIOS, CASES, GENERATORS):
        district_counts = [count for row, count in rows if row["allocation_case"] == case
                           and int(row["year"]) == year and row["scenario"] == scenario]
        national_count = national_counts[year, scenario]
        require(sum(district_counts) == Fraction(national_count), "Exact national count conservation failed.")
        key = (energy, power, generator, "TOTAL")
        target = national_lookup[year, scenario, energy, power, generator]
        errors = []
        for hour, (mean, _) in enumerate(profiles[key]):
            total = math.fsum(float(count) * mean for count in district_counts)
            close(total, target[f"mean_H{hour:02d}_kw"], "district-to-national hourly energy")
            errors.append(abs(total - target[f"mean_H{hour:02d}_kw"]))
        aggregated = [math.fsum(float(count) * value for count in district_counts) for value in vectors[key]]
        mean, mcse = mean_mcse(aggregated)
        close(mean, target["mean_daily_energy_kwh"], "national daily mean from shared replicate vectors")
        close(mcse, target["scaled_source_daily_mcse_kwh"], "national MCSE from shared replicate vectors")
        reconciliation.append({"allocation_case": case, "year": year, "scenario": scenario,
            "energy_case": energy, "power_scenario": power, "generator": generator,
            "maximum_hourly_balance_error_kw": max(errors),
            "daily_mean_balance_error_kwh": mean - target["mean_daily_energy_kwh"],
            "daily_mcse_balance_error_kwh": mcse - target["scaled_source_daily_mcse_kwh"], "passed": True})
    sources.unchanged()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    output = resolve(args.output_root) / f"residence_attributed_car_profiles_{stamp}"
    output.mkdir(parents=True, exist_ok=False)
    write_table(output, "per_car_mean_profiles_24h.csv", (
        {"energy_case": energy, "power_scenario": power, "generator": generator, "context": context,
         "hour_of_day": hour, "mean_kw_per_study_car": mean, "source_mcse_kw_per_study_car": mcse}
        for (energy, power, generator, context), values in profiles.items() for hour, (mean, mcse) in enumerate(values)))
    write_table(output, "per_car_replicate_profiles.csv", (
        {"energy_case": energy, "power_scenario": power, "replicate": rep, "generator": generator,
         "context": context, "hour_of_day": hour, "mean_kw_per_study_car": value}
        for (energy, power, rep, generator, context, hour), value in sorted(grid.items())))
    write_table(output, "district_profile_index.csv", (
        {**row, "exact_district_count_numerator": count.numerator, "exact_district_count_denominator": count.denominator}
        for row, count in rows))
    write_table(output, "district_total_profiles_24h.csv", (
        {"allocation_case": row["allocation_case"], "district_id": row["district_id"], "district_name": row["district_name"],
         "province_name": row["province_name"], "year": row["year"], "scenario": row["scenario"],
         "district_conditional_cars_display": row["district_conditional_car_count"],
         **profile_record(count, energy, power, generator, profiles, daily)}
        for row, count in rows for (energy, power), generator in itertools.product(CASES, GENERATORS)))
    write_table(output, "national_total_profiles_24h.csv", national_profiles)
    write_table(output, "allocation_profile_reconciliation.csv", reconciliation)
    (output / "README.md").write_text(
        "# Residence-attributed conditional Car profiles\n\n"
        "Calculation: district expected Car count times saved mean power per study Car. The Car denominator includes all selected Car owners. "
        "Source registration-cohort profiles are divided by their reference count to recover the per-Car basis; the 2025 registration count is not used as future stock.\n\n"
        "Two allocation comparisons cover 750 district/year/scenario configurations each. Each is evaluated with three energy cases, two power cases and two generators. "
        "The 18000 wide TOTAL profile rows are deterministic transfers of the existing simulations, not additional Monte Carlo replicates. "
        "National profiles occur once per year/scenario/energy/power/generator because the two allocation cases conserve the same national totals.\n\n"
        "Exact rational count weights are used for calculation. Six-decimal district counts are display values; the index retains exact numerator/denominator pairs. "
        "Each mean_H column is average kW over that one-hour bin; summing the 24 columns gives mean daily kWh. "
        "Peaks are peaks of mean profiles, not distributions of daily peak demand.\n\n"
        "TOTAL includes T1-T4 behavioural contexts. The four context daily energies are exported alongside TOTAL profiles. "
        "For any context/hour, multiply its per_car_mean_profiles_24h.csv value by the exact district count in district_profile_index.csv. "
        "Geographic P1-P4 place types are not assigned by this calculation. Residence attribution does not locate charging: owners can charge outside their home district.\n\n"
        "Both scheduling methods and all energy cases remain comparisons. Calibration and battery-review flags remain in the hash-bound source_car_selection_review.csv. "
        "No new eligibility decisions are made. Study-class and powertrain transfer assumptions remain unverified.\n\n"
        "Scaled source MCSE describes finite-replicate uncertainty conditional on the saved synthetic cohort, model and parameter bank. "
        "It excludes survey representativeness, fitted-parameter estimation, fleet-count, allocation-rule and real-world prediction uncertainty. "
        "Districts share the same replicate basis. National MCSE is checked using aggregated replicate vectors; districts are not treated as independent. "
        "Context MCSEs must not be added. No fleet prediction intervals or P05/P95 day-level bands are created.\n\n"
        "Within each energy/power/generator case, normalized hourly shape is fixed across district, year and adoption scenario by construction. "
        "This is a scaling baseline. Its copies do not provide independent training examples or evidence that a neural model learns changing behaviour. "
        "No new sessions, owners, parameter draws, demand simulations or AI training occur; fleet inputs and geographic production weights remain unactivated.\n",
        encoding="utf-8")
    sources.unchanged()
    report = {"schema_version": "residence_attributed_car_profiles_v1", "status": "conditional_profiles_prepared",
        "output_origin": "SIMULATED_PROFILE_TRANSFER", "behaviour_input_origin": "SYNTHETIC",
        "fleet_count_origin": "RESEARCH_SCENARIO_ASSUMPTION", "vehicle_class_scope": "Car",
        "attribution_basis": "conditional_owner_residence_proxy", "source_study_car_owners": owners,
        "source_replicates_per_case": n, "energy_power_cases": 6, "generators": list(GENERATORS),
        "allocation_cases": list(ALLOCATIONS), "districts": 25, "district_configuration_rows": len(rows),
        "district_total_profile_rows": len(rows) * 12, "national_total_profile_rows": len(national_profiles),
        "per_car_mean_profile_rows": 1440, "per_car_replicate_profile_rows": len(grid),
        "national_reconciliation_rows": len(reconciliation),
        "source_review_reference": rel(cohort_folder / "source_car_selection_review.csv"),
        "checks": {"source_hashes_verified": True, "source_means_and_mcse_reproduced": True,
            "complete_source_and_district_grids": True, "profile_energy_conserved": True,
            "exact_district_counts_reconstructed": True, "district_national_profiles_reconciled": True,
            "shared_replicate_national_mcse_checked": True, "source_files_unchanged": True},
        "static_normalized_shape_within_case": True, "district_charging_location_profiles_established": False,
        "geographic_place_typologies_allocated": False, "fleet_prediction_intervals_established": False,
        "independent_large_fleet_simulated": False, "fleet_contract_activated": False, "district_demand_ready": False,
        "empirical_validation": False, "new_demand_simulations": 0, "new_parameter_draws": 0, "new_virtual_owners": 0,
        "input_sha256": {rel(p): value for p, value in sorted(sources.hashes.items(), key=lambda item: str(item[0]))},
        "output_sha256": {p.name: sha(p) for p in sorted(output.iterdir()) if p.is_file()}}
    (output / "residence_attributed_profile_report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("Conditional residence-attributed Car profiles complete.")
    print(f"study_car_owners: {owners} | source_replicates_per_case: {n}")
    print(f"district_configurations: {len(rows)} | energy_power_cases: 6 | generators: 2")
    print(f"district_TOTAL_profiles: {len(rows) * 12} | national_TOTAL_profiles: {len(national_profiles)}")
    print("Source means, MCSE, hourly energy and district-to-national balances passed.")
    print(f"Output: {rel(output)}")
    print("Scaling baseline only; charging destinations, predictive intervals and empirical validation remain unestablished.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError, ArithmeticError, csv.Error) as error:
        raise SystemExit(f"Residence-attributed profile calculation failed: {error}") from error
