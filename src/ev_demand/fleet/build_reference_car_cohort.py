"""Scale saved Car profiles to the reported 2025 electric-car registration cohort.

This is a conditional transfer calculation, not an active-fleet estimate or
an estimate of measured charging demand in 2025. No new EVs are simulated.
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

GENERATORS = ("poisson_control", "availability_aware")
CONTEXTS = ("T1", "T2", "T3", "T4", "TOTAL")

ENERGIES = (
    "bounded_uniform_test",
    "conditional_gamma_test",
    "conditional_lognormal_test",
)

POWERS = ("reduced_test", "rated_ceiling_test")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def resolve(value, base=ROOT):
    path = Path(value)
    path = (path if path.is_absolute() else base / path).resolve()

    require(
        path.is_relative_to(ROOT),
        f"Path outside repository: {path}",
    )

    return path


def rel(path):
    return path.relative_to(ROOT).as_posix()


def sha256(path):
    digest = hashlib.sha256()

    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)

    return digest.hexdigest()


def read_csv(path, required):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        fields = reader.fieldnames or []

        require(
            len(fields) == len(set(fields)),
            f"Duplicate columns: {path}",
        )

        require(
            set(required) <= set(fields),
            f"Missing columns: {path}",
        )

        for row in reader:
            require(
                None not in row
                and all(value is not None for value in row.values()),
                f"Malformed CSV row: {path}",
            )

            yield row


def number(value):
    result = float(value)

    require(
        math.isfinite(result) and result >= 0,
        f"Invalid nonnegative value: {value}",
    )

    return result


def close(actual, expected, label):
    require(
        math.isclose(
            actual,
            expected,
            rel_tol=1e-8,
            abs_tol=1e-7,
        ),
        f"Reconciliation failed: {label}: {actual} versus {expected}",
    )


def mean_mcse(values):
    values = list(values)

    return (
        statistics.mean(values),
        statistics.stdev(values) / math.sqrt(len(values)),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument("--crosswalk", required=True)
    parser.add_argument("--fleet-contract", required=True)

    parser.add_argument(
        "--output-root",
        default="data/processed/synthetic",
    )

    args = parser.parse_args()
    watched = {}

    def watch(path, expected=None):
        require(
            path.is_file(),
            f"Source not found: {rel(path)}",
        )

        if path not in watched:
            watched[path] = sha256(path)

        if expected is not None:
            require(
                isinstance(expected, str)
                and len(expected) == 64
                and watched[path] == expected.lower(),
                f"Source hash mismatch: {rel(path)}",
            )

    def package(folder, name):
        path = folder / name
        watch(path)

        report = json.loads(
            path.read_text(encoding="utf-8-sig")
        )

        for field in ("input_sha256", "output_sha256"):
            require(
                isinstance(report.get(field), dict)
                and report[field],
                f"Missing {field}: {rel(path)}",
            )

        for source, expected in report["input_sha256"].items():
            watch(resolve(source), expected)

        for filename, expected in report["output_sha256"].items():
            require(
                Path(filename).name == filename,
                "Expected bare output filename.",
            )

            watch(
                resolve(filename, folder),
                expected,
            )

        return report

    def unchanged():
        for path, expected in watched.items():
            require(
                sha256(path) == expected,
                f"Input changed: {rel(path)}",
            )

    watch(Path(__file__).resolve())

    crosswalk_folder = resolve(args.crosswalk)
    contract_folder = resolve(args.fleet_contract)

    print(
        "Verifying crosswalk, fleet-contract "
        "and source-export hashes..."
    )

    crosswalk = package(
        crosswalk_folder,
        "vehicle_class_crosswalk_report.json",
    )

    contract = package(
        contract_folder,
        "fleet_contract_report.json",
    )

    require(
        crosswalk.get("schema_version")
        == "vehicle_class_crosswalk_review_v1"
        and crosswalk.get("status")
        == "proposed_crosswalk_prepared",
        "Unsupported crosswalk package.",
    )

    require(
        crosswalk.get("checks")
        and all(
            value is True
            for value in crosswalk["checks"].values()
        ),
        "A recorded crosswalk check did not pass.",
    )

    require(
        resolve(crosswalk["fleet_contract_reference"])
        == contract_folder,
        "Fleet contract differs from the crosswalk-bound contract.",
    )

    references = (
        contract_folder / "public_fleet_reference_counts.csv"
    )

    require(
        references.name in contract["output_sha256"],
        "Reference CSV is not hash-bound.",
    )

    selected_reference = [
        row
        for row in read_csv(
            references,
            (
                "evidence_id",
                "reference_year",
                "reported_value",
                "unit",
                "count_basis",
                "vehicle_label",
                "reported_fuel_category",
                "permitted_role",
                "data_origin",
            ),
        )
        if row["evidence_id"] == "DMT2025_CAR_E_NEW"
    ]

    require(
        len(selected_reference) == 1,
        "Expected one DMT2025_CAR_E_NEW reference.",
    )

    reference = selected_reference[0]

    require(
        reference["reference_year"] == "2025"
        and reference["count_basis"] == "annual_new_registrations"
        and reference["vehicle_label"] == "motor_cars"
        and reference["reported_fuel_category"] == "Electric"
        and reference["unit"] == "vehicles"
        and reference["data_origin"] == "PUBLIC_REFERENCE",
        "Reference scope is not 2025 Electric motor-car "
        "new registrations.",
    )

    count = number(reference["reported_value"])

    require(
        count > 0 and count.is_integer(),
        "Reference count must be a positive integer.",
    )

    count = int(count)

    index_file = crosswalk_folder / "class_profile_index.csv"

    require(
        index_file.name in crosswalk["output_sha256"],
        "Profile index is not hash-bound.",
    )

    index = [
        row
        for row in read_csv(
            index_file,
            (
                "vehicle_class",
                "energy_case",
                "power_scenario",
                "generator",
                "source_report",
            ),
        )
        if row["vehicle_class"] == "Car"
    ]

    index_cells = [
        (
            row["energy_case"],
            row["power_scenario"],
            row["generator"],
        )
        for row in index
    ]

    require(
        len(index_cells) == len(set(index_cells))
        and set(index_cells)
        == set(
            itertools.product(
                ENERGIES,
                POWERS,
                GENERATORS,
            )
        ),
        "Expected exactly twelve Car profile-index rows "
        "across six cases.",
    )

    source_reports = sorted(
        {
            resolve(row["source_report"])
            for row in index
        }
    )

    require(
        len(source_reports) == 6,
        "Expected six distinct class-export reports.",
    )

    profile_rows = []
    summary_rows = []
    replicate_rows = []
    review_rows = []

    baseline = None
    cases = set()

    origins = {
        "output_origin": "SIMULATED",
        "behaviour_input_origin": "SYNTHETIC",
        "count_input_origin": "PUBLIC_REFERENCE",
        "calculation_basis": (
            "conditional_registration_cohort_transfer"
        ),
        "count_reference_year": 2025,
        "reference_cohort_vehicles": count,
    }

    for report_path in source_reports:
        require(
            report_path in watched,
            "Class-export report is not crosswalk hash-bound.",
        )

        folder = report_path.parent
        report = package(folder, report_path.name)

        require(
            report.get("schema_version")
            == "vehicle_class_profile_export_v1"
            and report.get("status") == "complete"
            and report.get("cohort") == "common_energy_cases"
            and report.get("output_origin") == "SIMULATED"
            and report.get("behaviour_input_origin") == "SYNTHETIC",
            "Unsupported class-export scope.",
        )

        require(
            report.get("checks")
            and all(
                value is True
                for value in report["checks"].values()
            ),
            "A class-export check did not pass.",
        )

        case = (
            report["energy_case"],
            report["power_scenario"],
        )

        require(
            case not in cases
            and case in set(itertools.product(ENERGIES, POWERS)),
            "Duplicate or unexpected source case.",
        )

        cases.add(case)

        n = report["replicates"]

        owners = report[
            "class_counts_in_selected_cohort"
        ].get("Car", 0)

        require(
            type(n) is int
            and n >= 2
            and type(owners) is int
            and owners > 0,
            "At least two replicates and a positive Car "
            "denominator are required.",
        )

        car_ids = sorted(
            row["owner_id"]
            for row in read_csv(
                folder / "class_owner_links.csv",
                ("owner_id", "vehicle_class"),
            )
            if row["vehicle_class"] == "Car"
        )

        require(
            len(car_ids) == len(set(car_ids)) == owners,
            "Car owner-link count mismatch.",
        )

        settings = (
            car_ids,
            n,
            report["study_days"],
            report["warmup_days"],
            report["base_seed"],
        )

        if baseline is None:
            baseline = settings

        require(
            settings == baseline,
            "Car owners or run settings differ across cases.",
        )

        for filename in (
            "class_owner_links.csv",
            "class_replicate_profiles.csv",
            "class_daily_energy_summary.csv",
            "class_selection_review.csv",
        ):
            require(
                filename in report["output_sha256"],
                f"Unbound class output: {filename}",
            )

        factor = count / owners

        common = {
            "energy_case": case[0],
            "power_scenario": case[1],
            "vehicle_class": "Car",
            "study_car_owners": owners,
            "n_replicates": n,
            "scale_factor": factor,
            **origins,
        }

        grid = {}

        for row in read_csv(
            folder / "class_replicate_profiles.csv",
            (
                "vehicle_class",
                "replicate",
                "generator",
                "context",
                "hour_of_day",
                "selected_owners",
                "mean_power_kw",
                "mean_power_per_study_vehicle_kw",
            ),
        ):
            if row["vehicle_class"] != "Car":
                continue

            key = (
                int(row["replicate"]),
                row["generator"],
                row["context"],
                int(row["hour_of_day"]),
            )

            require(
                key not in grid
                and int(row["selected_owners"]) == owners,
                "Duplicate profile cell or incorrect Car denominator.",
            )

            value = number(row["mean_power_kw"])

            close(
                value / owners,
                number(row["mean_power_per_study_vehicle_kw"]),
                "per-Car profile",
            )

            grid[key] = value

        expected = set(
            itertools.product(
                range(1, n + 1),
                GENERATORS,
                CONTEXTS,
                range(24),
            )
        )

        require(
            set(grid) == expected,
            "Incomplete Car replicate-profile grid.",
        )

        for rep, generator, hour in itertools.product(
            range(1, n + 1),
            GENERATORS,
            range(24),
        ):
            close(
                sum(
                    grid[rep, generator, context, hour]
                    for context in CONTEXTS[:4]
                ),
                grid[rep, generator, "TOTAL", hour],
                "context-to-TOTAL power",
            )

        source_summary = {}

        for row in read_csv(
            folder / "class_daily_energy_summary.csv",
            (
                "vehicle_class",
                "generator",
                "context",
                "mean_daily_energy_kwh",
                "daily_energy_mcse_kwh",
            ),
        ):
            if row["vehicle_class"] == "Car":
                key = (
                    row["generator"],
                    row["context"],
                )

                require(
                    key not in source_summary,
                    "Duplicate source daily summary.",
                )

                source_summary[key] = row

        require(
            set(source_summary)
            == set(itertools.product(GENERATORS, CONTEXTS)),
            "Incomplete Car source daily summary.",
        )

        for generator, context in itertools.product(
            GENERATORS,
            CONTEXTS,
        ):
            daily = [
                sum(
                    grid[rep, generator, context, hour]
                    for hour in range(24)
                )
                for rep in range(1, n + 1)
            ]

            average, mcse = mean_mcse(daily)
            saved = source_summary[generator, context]

            close(
                average,
                number(saved["mean_daily_energy_kwh"]),
                "source daily energy",
            )

            close(
                mcse,
                number(saved["daily_energy_mcse_kwh"]),
                "source daily MCSE",
            )

            mean_profile = []

            for hour in range(24):
                power, power_mcse = mean_mcse(
                    grid[rep, generator, context, hour]
                    for rep in range(1, n + 1)
                )

                mean_profile.append(power * factor)

                profile_rows.append(
                    {
                        **common,
                        "generator": generator,
                        "context": context,
                        "hour_of_day": hour,
                        "conditional_mean_power_kw": power * factor,
                        "scaled_source_mcse_kw": power_mcse * factor,
                    }
                )

            close(
                sum(mean_profile),
                average * factor,
                "scaled mean-profile energy",
            )

            peak = max(mean_profile)

            hours = [
                hour
                for hour, power in enumerate(mean_profile)
                if math.isclose(
                    power,
                    peak,
                    rel_tol=1e-10,
                    abs_tol=1e-10,
                )
            ]

            summary_rows.append(
                {
                    **common,
                    "generator": generator,
                    "context": context,
                    "mean_daily_energy_per_study_car_kwh": (
                        average / owners
                    ),
                    "conditional_mean_daily_energy_kwh": (
                        average * factor
                    ),
                    "scaled_source_daily_mcse_kwh": (
                        mcse * factor
                    ),
                    "peak_of_conditional_mean_profile_kw": peak,
                    "mean_profile_peak_hours_json": json.dumps(hours),
                }
            )

        for (
            rep,
            generator,
            context,
            hour,
        ), power in sorted(grid.items()):
            replicate_rows.append(
                {
                    **common,
                    "replicate": rep,
                    "generator": generator,
                    "context": context,
                    "hour_of_day": hour,
                    "conditional_mean_power_kw": power * factor,
                }
            )

        selections = set()

        for row in read_csv(
            folder / "class_selection_review.csv",
            (
                "vehicle_class",
                "owner_id",
                "replicate",
                "exceeds_reported_battery_upper",
                "calibration_status",
                "verification_within_development_band",
            ),
        ):
            if row["vehicle_class"] != "Car":
                continue

            key = (
                row["owner_id"],
                int(row["replicate"]),
            )

            require(
                key not in selections,
                "Duplicate Car review selection.",
            )

            selections.add(key)

            state = (
                row["exceeds_reported_battery_upper"]
                .strip()
                .lower()
            )

            require(
                state in {"true", "false", ""},
                "Invalid nullable battery flag.",
            )

            require(
                row["verification_within_development_band"]
                .strip()
                .lower()
                in {"true", "false"},
                "Invalid required verification boolean.",
            )

            review_rows.append(
                {
                    **common,
                    **row,
                    "source_export": rel(folder),
                }
            )

        require(
            selections
            == set(
                itertools.product(
                    car_ids,
                    range(1, n + 1),
                )
            ),
            "Incomplete Car review-selection grid.",
        )

        print(
            f"Checked {case[0]} / {case[1]}: "
            f"{owners} study Car owners"
        )

    require(
        cases == set(itertools.product(ENERGIES, POWERS)),
        "Incomplete six-case grid.",
    )

    unchanged()

    stamp = datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%S_%fZ"
    )

    output = (
        resolve(args.output_root)
        / f"reference_car_cohort_{stamp}"
    )

    output.mkdir(
        parents=True,
        exist_ok=False,
    )

    outputs = []

    for filename, data in (
        ("cohort_mean_profiles_24h.csv", profile_rows),
        ("cohort_daily_summary.csv", summary_rows),
        ("cohort_replicate_profiles.csv", replicate_rows),
        ("source_car_selection_review.csv", review_rows),
    ):
        path = output / filename

        with path.open(
            "w",
            encoding="utf-8",
            newline="",
        ) as stream:
            writer = csv.DictWriter(
                stream,
                fieldnames=list(data[0]),
            )

            writer.writeheader()
            writer.writerows(data)

        outputs.append(path)

    scope = {
        "scope_id": (
            "conditional_2025_E_motor_car_registration_cohort"
        ),
        "count_basis": "registration_cohort",
        "source_reference": reference,
        "count_is_active_fleet_stock": False,
        "conditional_transfer_assumption_id": (
            "TRANSFER-CAR-REGCOHORT-01"
        ),
        "conditional_transfer_rule": (
            "Apply the saved synthetic survey-Car per-study-vehicle "
            "means to each vehicle in the reported Electric "
            "motor-car registration cohort."
        ),
        "assumption_is_empirically_verified": False,
        "study_car_owners": len(baseline[0]),
        "source_study_days": baseline[2],
        "source_warmup_days": baseline[3],
        "source_replicates_per_case": baseline[1],
        "calculation_time_basis": (
            "Source study-period average, not a reconstruction "
            "of measured 2025 charging."
        ),
        "uncertainty_basis": (
            "Deterministically scaled source MCSE conditional on "
            "the saved model and parameter bank; not national "
            "population uncertainty or a new fleet simulation."
        ),
        "district_allocation_performed": False,
        "future_fleet_inputs_filled": False,
    }

    scope_path = output / "reference_cohort_scope.json"

    scope_path.write_text(
        json.dumps(
            scope,
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    outputs.append(scope_path)

    readme = output / "README.md"

    readme.write_text(
        "# Conditional electric-car registration-cohort calculation\n\n"
        "The public input counts newly registered Electric motor cars in 2025. "
        "It does not count the active national electric fleet. The saved Car "
        "profiles are transferred under assumption TRANSFER-CAR-REGCOHORT-01. "
        "Survey-to-registration class and powertrain compatibility remain "
        "unverified.\n\n"
        "Calculation: source Car profile / all selected Car owners * reference "
        "cohort count. Every selected Car owner remains in the denominator, "
        "including those with no charging during a study replicate. Contexts "
        "remain T1-T4 behavioural contexts. TOTAL is their sum.\n\n"
        "Mean-profile peaks are maxima of ensemble mean curves. Their hours "
        "are not estimates of the distribution of individual daily peaks. "
        "MCSE is recomputed across each case's saved replicate vectors and "
        "scaled by the same constant. Context MCSEs are not added. This "
        "does not simulate independent vehicles in a new large fleet or "
        "establish fleet-level predictive intervals. Cases are not pooled.\n\n"
        "The count reference year labels the registration cohort, not a "
        "historically reconstructed charging year. Battery missing states, "
        "calibration flags and verification flags are copied for the Car "
        "selections. Vehicle limits and connector compatibility are not "
        "established by this calculation. The separate extension cohort, "
        "SUV/crossover and other survey classes are not added.\n\n"
        "No new random draws, simulations, fitted parameters, exclusions, "
        "2026-2035 fleet counts, district shares or place allocations are "
        "created. This is a reference scaling check before future scenario "
        "inputs are chosen; empirical validation remains outstanding.\n",
        encoding="utf-8",
    )

    outputs.append(readme)

    unchanged()

    report = {
        "schema_version": "reference_car_cohort_transfer_v1",
        "status": "complete",
        **origins,
        "source_evidence_id": reference["evidence_id"],
        "class_export_cases": len(cases),
        "study_car_owners": len(baseline[0]),
        "source_replicates_per_case": baseline[1],
        "source_study_days": baseline[2],
        "mean_profile_rows": len(profile_rows),
        "daily_summary_rows": len(summary_rows),
        "replicate_profile_rows": len(replicate_rows),
        "source_review_rows": len(review_rows),
        "battery_flag_states_retained": dict(
            Counter(
                row["exceeds_reported_battery_upper"]
                .strip()
                .lower()
                or "missing"
                for row in review_rows
            )
        ),
        "checks": {
            "source_hashes_verified": True,
            "complete_six_case_grid": True,
            "same_Car_owner_ids_and_settings": True,
            "source_energy_and_mcse_reproduced": True,
            "context_TOTAL_reconciliation": True,
            "scaled_profile_energy_conserved": True,
            "source_review_grid_complete": True,
            "source_files_unchanged": True,
        },
        "behaviour_transfer_is_declared_assumption": True,
        "owner_registration_class_verified": False,
        "powertrain_compatibility_verified": False,
        "active_EV_stock_established": False,
        "national_forecast_ready": False,
        "fleet_contract_activated": False,
        "district_demand_ready": False,
        "empirical_validation": False,
        "new_demand_simulations": 0,
        "new_parameter_draws": 0,
        "input_sha256": {
            rel(path): value
            for path, value in sorted(
                watched.items(),
                key=lambda item: str(item[0]),
            )
        },
        "output_sha256": {
            path.name: sha256(path)
            for path in outputs
        },
    }

    report_path = (
        output / "reference_car_cohort_report.json"
    )

    report_path.write_text(
        json.dumps(
            report,
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    print("Conditional reference-car cohort calculation complete.")

    print(
        f"public_registration_cohort_vehicles: {count}"
    )

    print(
        f"study_car_owners: {len(baseline[0])}"
    )

    print(
        f"cases: {len(cases)} | "
        f"source_replicates_per_case: {baseline[1]}"
    )

    print(
        f"mean_profile_rows: {len(profile_rows)} | "
        f"daily_summary_rows: {len(summary_rows)}"
    )

    print(
        "Source hashes, cohort scope, Car denominators, "
        "MCSE and energy checks passed."
    )

    print(f"Output: {rel(output)}")

    print(
        "Conditional registration-cohort transfer only; "
        "future fleet inputs remain unfilled."
    )


if __name__ == "__main__":
    try:
        main()

    except (ValueError, OSError, KeyError, csv.Error) as error:
        raise SystemExit(
            f"Reference-car cohort calculation failed: {error}"
        ) from error