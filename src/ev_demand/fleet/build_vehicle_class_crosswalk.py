"""Bind six class-profile exports to a proposed, non-activated DMT crosswalk."""

import argparse
import csv
import hashlib
import itertools
import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]

ENERGY_CASES = (
    "bounded_uniform_test",
    "conditional_gamma_test",
    "conditional_lognormal_test",
)

POWER_CASES = (
    "reduced_test",
    "rated_ceiling_test",
)

GENERATORS = (
    "poisson_control",
    "availability_aware",
)

CONTEXTS = (
    "T1",
    "T2",
    "T3",
    "T4",
    "TOTAL",
)

REFERENCES = (
    "https://dmt.gov.lk/images/2026/Vehicle_Population_2014-2025.pdf",
    "https://dmt.gov.lk/images/2026/"
    "New_Registrations_of_Motor_Vehicles_by_Type_of_Fuel_-_2025.pdf",
)

POLICY = {
    "Car": (
        "motor_cars",
        "Motor cars",
        "candidate_requires_declared_assumption",
        "CLASS-CAR-PROXY-01",
        "Proposed correspondence from the survey label. Confirm registration "
        "class or declare this proxy before using a motor-car fleet count.",
    ),
    "Motorcycle / scooter": (
        "motor_cycles",
        "Motor Cycles",
        "candidate_requires_declared_assumption",
        "CLASS-MOTORCYCLE-PROXY-01",
        "Confirm that the reported vehicle belongs to the published registered "
        "motorcycle category; the survey label alone does not verify this.",
    ),
    "Three-wheeler": (
        "three_wheelers",
        "3 Wheelers",
        "candidate_requires_declared_assumption",
        "CLASS-THREEWHEELER-PROXY-01",
        "Confirm the tricycle subtype. The fuel table separately identifies "
        "Motor Tricycle Van; do not combine those columns automatically.",
    ),
    "SUV / crossover": (
        "",
        "",
        "registration_class_unresolved",
        "",
        "Keep this survey body label separate. It does not establish whether "
        "a vehicle belongs to Motor cars, Dual Purpose Vehicles or another "
        "published registration group.",
    ),
    "Van": (
        "",
        "",
        "registration_class_unresolved",
        "",
        "Obtain registration-class and vehicle-use evidence. Do not interpret "
        "Van as Motor Tricycle Van or Dual Purpose Vehicles automatically.",
    ),
    "Other": (
        "",
        "",
        "registration_class_unresolved",
        "",
        "Obtain a more specific vehicle description and registration class.",
    ),
}


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


def digest(path):
    value = hashlib.sha256()

    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)

    return value.hexdigest()


def rows(path, columns):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        names = reader.fieldnames or []

        require(
            len(names) == len(set(names)),
            f"Duplicate CSV columns: {path}",
        )

        require(
            set(columns) <= set(names),
            f"Missing required CSV columns: {path}",
        )

        for row in reader:
            require(
                None not in row
                and all(value is not None for value in row.values()),
                f"Malformed CSV row: {path}",
            )

            yield row


def flag(value):
    value = value.strip().lower()

    require(
        value in {"true", "false", ""},
        f"Invalid battery flag: {value!r}",
    )

    return value or "missing"


def numeric(value):
    result = float(value)

    require(
        math.isfinite(result),
        f"Non-finite number: {value!r}",
    )

    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument(
        "--class-export",
        action="append",
        required=True,
    )

    parser.add_argument(
        "--dmt",
        required=True,
    )

    parser.add_argument(
        "--fleet-contract",
        required=True,
    )

    parser.add_argument(
        "--output-root",
        default="data/processed/synthetic",
    )

    args = parser.parse_args()

    require(
        len(args.class_export) == 6,
        "Supply exactly six class-export folders.",
    )

    inputs = {}

    def watch(path, expected=None):
        require(
            path.is_file(),
            f"Source not found: {path}",
        )

        if path not in inputs:
            inputs[path] = digest(path)

        if expected is not None:
            require(
                isinstance(expected, str)
                and len(expected) == 64
                and inputs[path] == expected.lower(),
                f"Source hash mismatch: {rel(path)}",
            )

        return inputs[path]

    def package(folder, report_name):
        path = folder / report_name
        watch(path)

        report = json.loads(
            path.read_text(encoding="utf-8-sig")
        )

        for field in ("input_sha256", "output_sha256"):
            require(
                isinstance(report.get(field), dict)
                and report[field],
                f"Missing source hash manifest {field}: {rel(path)}",
            )

        for name, expected in report["input_sha256"].items():
            watch(
                resolve(name),
                expected,
            )

        for name, expected in report["output_sha256"].items():
            require(
                Path(name).name == name,
                f"Expected output filename: {name}",
            )

            watch(
                resolve(name, folder),
                expected,
            )

        return report

    watch(Path(__file__).resolve())

    dmt_folder = resolve(args.dmt)
    contract_folder = resolve(args.fleet_contract)

    print("Verifying DMT and fleet-contract source packages...")

    dmt_report = package(
        dmt_folder,
        "dmt_table_report.json",
    )

    contract = package(
        contract_folder,
        "fleet_contract_report.json",
    )

    fuel_path = dmt_folder / "fuel_registrations_2025.csv"

    require(
        fuel_path.name in dmt_report["output_sha256"],
        "Fuel table is absent from the DMT output manifest.",
    )

    catalog = set()

    for row in rows(
        fuel_path,
        (
            "vehicle_group",
            "fuel_code",
            "reported_fuel_category",
        ),
    ):
        require(
            row["vehicle_group"] and row["fuel_code"],
            "Empty DMT group/fuel key.",
        )

        catalog.add(
            (
                row["vehicle_group"],
                row["fuel_code"],
                row["reported_fuel_category"],
            )
        )

    groups = {item[0] for item in catalog}

    require(
        {
            item[0]
            for item in POLICY.values()
            if item[0]
        } <= groups,
        "A proposed group is absent from the extracted DMT fuel table.",
    )

    cases = {}

    baseline_owners = None
    baseline_coverage = None
    baseline_settings = None

    combined = []
    index = []
    evidence = []

    source_check_count = 0

    energy_fields = (
        "generator",
        "vehicle_class",
        "selected_owners",
        "context",
        "n_replicates",
        "mean_daily_energy_kwh",
        "daily_energy_mcse_kwh",
        "mean_daily_energy_per_study_vehicle_kwh",
        "mcse_per_study_vehicle_kwh",
    )

    for value in args.class_export:
        folder = resolve(value)

        print(f"Verifying class export: {folder.name}")

        report = package(
            folder,
            "vehicle_class_profile_report.json",
        )

        require(
            report.get("schema_version")
            == "vehicle_class_profile_export_v1"
            and report.get("status") == "complete",
            "Unsupported/incomplete class export.",
        )

        require(
            report.get("cohort") == "common_energy_cases"
            and report.get("output_origin") == "SIMULATED"
            and report.get("behaviour_input_origin") == "SYNTHETIC",
            "This review requires the synthetic common-cohort exports.",
        )

        require(
            report.get("checks")
            and all(
                value is True
                for value in report["checks"].values()
            ),
            "A class-export source check did not pass.",
        )

        key = (
            report["energy_case"],
            report["power_scenario"],
        )

        require(
            key not in cases,
            f"Duplicate energy/power case: {key}",
        )

        needed_files = {
            "class_owner_links.csv",
            "class_coverage.csv",
            "class_selection_review.csv",
            "class_daily_energy_summary.csv",
            "class_mean_profile_24h.csv",
            "class_replicate_profiles.csv",
        }

        require(
            needed_files <= set(report["output_sha256"]),
            "Required class-export output missing from manifest.",
        )

        owner_links = {}

        for row in rows(
            folder / "class_owner_links.csv",
            (
                "owner_id",
                "vehicle_class",
            ),
        ):
            require(
                row["owner_id"] not in owner_links
                and row["owner_id"],
                "Duplicate/empty class-linked owner ID.",
            )

            owner_links[row["owner_id"]] = row["vehicle_class"]

        require(
            len(owner_links) == report["owners_simulated"],
            "Owner count mismatch.",
        )

        counts = Counter(owner_links.values())

        require(
            dict(counts)
            == report["class_counts_in_selected_cohort"],
            "Class counts do not match owner links.",
        )

        coverage = {}

        for row in rows(
            folder / "class_coverage.csv",
            (
                "vehicle_class",
                "retained_feature_owners",
                "selected_owners",
                "profile_status",
            ),
        ):
            label = row["vehicle_class"]

            require(
                label not in coverage
                and label in POLICY,
                f"Unreviewed class label: {label}",
            )

            retained = int(row["retained_feature_owners"])
            selected = int(row["selected_owners"])

            require(
                0 <= selected <= retained
                and selected == counts[label],
                f"Class coverage mismatch: {label}",
            )

            expected_profile_status = (
                "profile_exported"
                if selected
                else "no_selected_owners"
            )

            require(
                row["profile_status"] == expected_profile_status,
                f"Class profile-status mismatch: {label}",
            )

            coverage[label] = (
                retained,
                selected,
            )

        require(
            set(counts) <= set(coverage),
            "Profiled class lacks a coverage row.",
        )

        require(
            sum(value[0] for value in coverage.values())
            == report["owners_retained_in_features"],
            "Retained-feature coverage does not balance.",
        )

        features = resolve(report["features_file"])

        require(
            features in inputs,
            "Feature file absent from source manifests.",
        )

        settings = (
            report["study_days"],
            report["warmup_days"],
            report["replicates"],
            report["base_seed"],
            inputs[features],
        )

        if baseline_owners is None:
            baseline_owners = owner_links
            baseline_coverage = coverage
            baseline_settings = settings

        else:
            require(
                owner_links == baseline_owners,
                "Owner IDs/classes differ across cases.",
            )

            require(
                coverage == baseline_coverage,
                "Class coverage differs across cases.",
            )

            require(
                settings == baseline_settings,
                "Source features or experiment settings differ.",
            )

        reps = report["replicates"]

        require(
            type(reps) is int and reps > 0,
            "Invalid replicate count.",
        )

        expected_selections = set(
            itertools.product(
                (str(rep) for rep in range(1, reps + 1)),
                owner_links,
            )
        )

        selections = set()

        battery_counts = Counter(
            {
                "true": 0,
                "false": 0,
                "missing": 0,
            }
        )

        missing_owners = set()

        for row in rows(
            folder / "class_selection_review.csv",
            (
                "replicate",
                "owner_id",
                "vehicle_class",
                "exceeds_reported_battery_upper",
            ),
        ):
            selection = (
                row["replicate"],
                row["owner_id"],
            )

            require(
                selection in expected_selections
                and selection not in selections,
                "Missing/unexpected/duplicate owner selection.",
            )

            require(
                row["vehicle_class"]
                == owner_links[row["owner_id"]],
                "Selection/owner class mismatch.",
            )

            selections.add(selection)

            state = flag(
                row["exceeds_reported_battery_upper"]
            )

            battery_counts[state] += 1

            if state == "missing":
                missing_owners.add(row["owner_id"])

        require(
            selections == expected_selections,
            "Incomplete owner-selection grid.",
        )

        require(
            dict(battery_counts)
            == report["battery_upper_flag_counts"],
            "Battery flag counts do not reproduce the source report.",
        )

        energy_grid = set()

        for row in rows(
            folder / "class_daily_energy_summary.csv",
            energy_fields,
        ):
            cell = (
                row["generator"],
                row["vehicle_class"],
                row["context"],
            )

            require(
                cell not in energy_grid,
                "Duplicate class-energy summary cell.",
            )

            energy_grid.add(cell)

            label = row["vehicle_class"]

            require(
                label in counts
                and int(row["selected_owners"]) == counts[label]
                and int(row["n_replicates"]) == reps,
                "Energy-summary denominator mismatch.",
            )

            require(
                math.isclose(
                    numeric(row["mean_daily_energy_kwh"])
                    / counts[label],
                    numeric(
                        row["mean_daily_energy_per_study_vehicle_kwh"]
                    ),
                    rel_tol=1e-8,
                    abs_tol=1e-7,
                ),
                "Per-study-vehicle energy does not reproduce "
                "its class denominator.",
            )

            if reps > 1:
                require(
                    math.isclose(
                        numeric(row["daily_energy_mcse_kwh"])
                        / counts[label],
                        numeric(row["mcse_per_study_vehicle_kwh"]),
                        rel_tol=1e-8,
                        abs_tol=1e-7,
                    ),
                    "Per-study-vehicle MCSE denominator mismatch.",
                )

            else:
                require(
                    row["daily_energy_mcse_kwh"]
                    == row["mcse_per_study_vehicle_kwh"]
                    == "",
                    "Single-replicate MCSE must remain unavailable.",
                )

            combined.append(
                {
                    "energy_case": key[0],
                    "power_scenario": key[1],
                    **{
                        field: row[field]
                        for field in energy_fields
                    },
                    "source_export": rel(folder),
                    "cohort": report["cohort"],
                    "output_origin": "SIMULATED",
                    "behaviour_input_origin": "SYNTHETIC",
                }
            )

        require(
            energy_grid
            == set(
                itertools.product(
                    GENERATORS,
                    counts,
                    CONTEXTS,
                )
            ),
            "Incomplete/unexpected generator/class/context grid.",
        )

        for generator, label in itertools.product(
            GENERATORS,
            sorted(counts),
        ):
            source_report = (
                folder / "vehicle_class_profile_report.json"
            )

            index.append(
                {
                    "energy_case": key[0],
                    "power_scenario": key[1],
                    "generator": generator,
                    "vehicle_class": label,
                    "selected_owners": counts[label],
                    "replicates": reps,
                    "study_days": report["study_days"],
                    "warmup_days": report["warmup_days"],
                    "cohort": report["cohort"],
                    "mean_profile_file": rel(
                        folder / "class_mean_profile_24h.csv"
                    ),
                    "replicate_profile_file": rel(
                        folder / "class_replicate_profiles.csv"
                    ),
                    "source_report": rel(source_report),
                    "source_report_sha256": inputs[source_report],
                    "automatic_fleet_mapping_permitted": False,
                }
            )

        source_check_count += (
            report["aggregate_reconciliation_checks"]
        )

        evidence.append(
            {
                "energy_case": key[0],
                "power_scenario": key[1],
                "source_export": rel(folder),
                "battery_upper_flag_counts": dict(battery_counts),
                "owners_with_missing_battery_flags": sorted(
                    missing_owners
                ),
                "calibration_status_counts": (
                    report["calibration_status_counts"]
                ),
                "verification_selections_outside_band": (
                    report["verification_selections_outside_band"]
                ),
            }
        )

        cases[key] = report

    require(
        set(cases)
        == set(
            itertools.product(
                ENERGY_CASES,
                POWER_CASES,
            )
        ),
        "The required three-energy by two-power case grid is incomplete.",
    )

    crosswalk = []

    for label, policy in POLICY.items():
        group, heading, status, assumption, rationale = policy

        retained, selected = baseline_coverage.get(
            label,
            (0, 0),
        )

        crosswalk.append(
            {
                "survey_vehicle_class": label,
                "retained_feature_owners": retained,
                "common_cohort_owners": selected,
                "candidate_dmt_extractor_group": group,
                "candidate_published_heading": heading,
                "mapping_status": status,
                "proposed_assumption_id": assumption,
                "automatic_fleet_mapping_permitted": False,
                "registration_class_verified": False,
                "powertrain_scope_verified": False,
                "rationale": rationale,
            }
        )

    for path, expected in inputs.items():
        require(
            digest(path) == expected,
            f"Input changed during preparation: {rel(path)}",
        )

    stamp = datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%S_%fZ"
    )

    out = (
        resolve(args.output_root)
        / f"vehicle_class_crosswalk_{stamp}"
    )

    out.mkdir(
        parents=True,
        exist_ok=False,
    )

    outputs = []

    def write(name, data):
        path = out / name

        with path.open(
            "w",
            newline="",
            encoding="utf-8",
        ) as stream:
            writer = csv.DictWriter(
                stream,
                fieldnames=list(data[0]),
            )

            writer.writeheader()
            writer.writerows(data)

        outputs.append(path)

    write(
        "vehicle_class_crosswalk.csv",
        crosswalk,
    )

    write(
        "class_case_summary.csv",
        combined,
    )

    write(
        "class_profile_index.csv",
        index,
    )

    write(
        "dmt_fuel_group_catalog.csv",
        [
            {
                "dmt_extractor_group": group,
                "fuel_code": code,
                "reported_fuel_category": heading,
                "source_table": rel(fuel_path),
                "data_origin": "PUBLIC_REFERENCE",
            }
            for group, code, heading in sorted(catalog)
        ],
    )

    decision = out / "vehicle_scope_review.md"

    table = "\n".join(
        f"| {row['survey_vehicle_class']} | "
        f"{row['common_cohort_owners']} | "
        f"{row['candidate_published_heading'] or 'Unresolved'} | "
        f"{row['mapping_status']} |"
        for row in crosswalk
    )

    decision.write_text(
        "# Vehicle-class scope and crosswalk review\n\n"
        "## Working decision\n\n"
        "Retain the survey vehicle classes as separate behavioural groups. "
        "Do not substitute the mixed-cohort mean for a motor-car-only profile. "
        "Use the matching class row and its complete selected-owner denominator "
        "when preparing a conditional scenario. This package does not fill or "
        "activate the fleet scenario contract.\n\n"
        "| Survey label | Common-cohort owners | "
        "Proposed DMT fuel-table group | Status |\n"
        "|---|---:|---|---|\n"
        + table
        + "\n\n"
        "The proposed correspondences are research inferences from category "
        "labels, not verified owner-level registration classifications. The "
        "proposed assumption IDs are references for future declarations; this "
        "package does not declare them accepted or activate them.\n\n"
        "## Evidence and scope\n\n"
        "The DMT statistics distinguish Motor cars, Dual Purpose Vehicles, "
        "Motor Cycles and tricycle groupings. The fuel table separates 3 Wheelers "
        "from Motor Tricycle Van. Its parser group names are internal extraction "
        "keys, not statutory vehicle-class codes. Published category presence "
        "does not by itself verify a survey-to-registration-class assignment.\n\n"
        + "\n".join(
            f"- {url}"
            for url in REFERENCES
        )
        + "\n\n"
        "The local DMT snapshot and fleet-contract manifests are hash-bound "
        "inputs. DMT numerical cells and their existing review flags are not "
        "modified. No DMT vehicle count is used in a demand calculation here.\n\n"
        "## Rules before fleet use\n\n"
        "- Confirm class membership or explicitly declare a proxy mapping "
        "assumption, its scope and sensitivity treatment.\n"
        "- Keep SUV/crossover, Van and Other separate until supported mapping "
        "evidence is available. A Van label is not a Motor Tricycle Van code.\n"
        "- Confirm the powertrain scope separately. Survey class labels and "
        "DMT Electric, Pet/Ele and Die/Ele columns do not independently establish "
        "a verified BEV/PHEV behavioural cohort.\n"
        "- Supply an explicit stock/count basis, year/scenario counts and "
        "district allocation evidence or declared assumptions. Annual new "
        "registrations are not an established active electric fleet stock.\n"
        "- Retain calibration, verification, battery, vehicle-limit and connector "
        "limitations. Class decomposition does not resolve them.\n"
        "- Keep behavioural contexts T1-T4 distinct from geographic place "
        "groups P1-P4, and owner residence distinct from charging destination.\n\n"
        "## Interpretation\n\n"
        "The six-case tables retain their original replicate counts. They are "
        "not pooled as additional independent replicates or expanded owner "
        "samples. Per-study-vehicle values remain conditional on the synthetic "
        "cohort and source model. Shared replicate profiles are linked for "
        "future covariance-aware aggregation; class MCSEs must not be added. "
        "Classes without selected owners receive no invented demand profile.\n\n"
        "No new simulations, random draws, fitting, imputation, owner exclusions, "
        "fleet counts or geographic demand scaling are performed. The separate "
        "open-upper extension cohort is not merged into this common-cohort package.\n",
        encoding="utf-8",
    )

    outputs.append(decision)

    for path, expected in inputs.items():
        require(
            digest(path) == expected,
            f"Input changed: {rel(path)}",
        )

    result = {
        "schema_version": "vehicle_class_crosswalk_review_v1",
        "status": "proposed_crosswalk_prepared",
        "class_export_cases": len(cases),
        "common_cohort_owners": len(baseline_owners),
        "class_counts": dict(
            sorted(
                Counter(baseline_owners.values()).items()
            )
        ),
        "replicates_per_case": baseline_settings[2],
        "class_profile_index_rows": len(index),
        "class_case_summary_rows": len(combined),
        "source_export_reconciliation_checks_retained": (
            source_check_count
        ),
        "proposed_mapping_rows": sum(
            bool(policy[0])
            for policy in POLICY.values()
        ),
        "unresolved_registration_class_rows": sum(
            not policy[0]
            for policy in POLICY.values()
        ),
        "dmt_table_report": rel(
            dmt_folder / "dmt_table_report.json"
        ),
        "dmt_source_status": dmt_report.get("status"),
        "dmt_numerical_review_flags_retained": (
            dmt_report.get("numerical_review_flags")
        ),
        "fleet_contract_reference": rel(contract_folder),
        "fleet_contract_status": contract.get("status"),
        "class_case_review_flags": evidence,
        "checks": {
            "source_hashes_verified": True,
            "complete_six_case_grid": True,
            "same_owner_ids_and_class_links": True,
            "same_features_and_settings": True,
            "class_energy_denominators_reproduced": True,
            "nullable_battery_flag_counts_reproduced": True,
            "proposed_groups_present_in_dmt_table": True,
            "source_files_unchanged": True,
        },
        "output_origin": "SIMULATED",
        "behaviour_input_origin": "SYNTHETIC",
        "public_reference_origin": "PUBLIC_REFERENCE",
        "owner_registration_class_mapping_verified": False,
        "powertrain_scope_verified": False,
        "mapping_assumptions_activated": False,
        "national_electric_stock_established": False,
        "fleet_counts_activated": False,
        "district_demand_ready": False,
        "empirical_validation": False,
        "new_demand_simulations": 0,
        "new_parameter_draws": 0,
        "input_sha256": {
            rel(path): value
            for path, value in sorted(
                inputs.items(),
                key=lambda item: str(item[0]),
            )
        },
        "output_sha256": {
            path.name: digest(path)
            for path in outputs
        },
    }

    report_path = (
        out / "vehicle_class_crosswalk_report.json"
    )

    report_path.write_text(
        json.dumps(
            result,
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    print("Vehicle-class crosswalk review package complete.")
    print(f"class_export_cases: {len(cases)}")
    print(f"common_cohort_owners: {len(baseline_owners)}")
    print(f"class_profile_index_rows: {len(index)}")
    print(f"class_case_summary_rows: {len(combined)}")
    print("proposed_mapping_rows: 3")
    print("unresolved_registration_class_rows: 3")

    print(
        "Source integrity, six-case grid, owner links "
        "and denominator checks passed."
    )

    print(f"Output: {rel(out)}")

    print(
        "Proposed crosswalk only; fleet mappings and "
        "numerical fleet inputs are not activated."
    )


if __name__ == "__main__":
    try:
        main()

    except (ValueError, OSError, csv.Error) as error:
        raise SystemExit(
            f"Vehicle-class crosswalk preparation failed: {error}"
        ) from error