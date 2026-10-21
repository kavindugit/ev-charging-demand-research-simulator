"""Prepare a reviewable 2026-2035 car-scenario design; activate no fleet inputs."""

import argparse
import csv
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

try:
    from . import build_reference_car_cohort as source_tools
except ImportError:
    import build_reference_car_cohort as source_tools


ROOT = source_tools.ROOT
require = source_tools.require
resolve = source_tools.resolve
rel = source_tools.rel
sha256 = source_tools.sha256
read_csv = source_tools.read_csv

SCENARIOS = ("low", "medium", "high")
YEARS = tuple(range(2026, 2036))


def share_argument(value):
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise argparse.ArgumentTypeError(
            "Share must be a decimal fraction."
        ) from error

    if (
        not result.is_finite()
        or not Decimal(0) < result <= Decimal(1)
    ):
        raise argparse.ArgumentTypeError(
            "Share must be finite and in (0, 1]."
        )

    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument(
        "--reference-cohort",
        required=True,
    )
    parser.add_argument(
        "--fleet-contract",
        required=True,
    )
    parser.add_argument(
        "--start-share",
        type=share_argument,
        required=True,
    )
    parser.add_argument(
        "--end-shares",
        type=share_argument,
        nargs=3,
        required=True,
        metavar=("LOW", "MEDIUM", "HIGH"),
    )
    parser.add_argument(
        "--output-root",
        default="data/processed/synthetic",
    )

    args = parser.parse_args()

    low, medium, high = args.end_shares

    require(
        args.start_share <= low < medium < high,
        "Require start_share <= low < medium < high endpoint shares.",
    )

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

    def package(folder, filename):
        path = folder / filename
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

        for name, expected in report["input_sha256"].items():
            watch(
                resolve(name),
                expected,
            )

        for name, expected in report["output_sha256"].items():
            require(
                Path(name).name == name,
                "Expected bare output filename.",
            )

            watch(
                resolve(name, folder),
                expected,
            )

        return report

    def unchanged():
        for path, expected in watched.items():
            require(
                sha256(path) == expected,
                f"Source changed: {rel(path)}",
            )

    watch(Path(__file__).resolve())
    watch(Path(source_tools.__file__).resolve())

    cohort_folder = resolve(args.reference_cohort)
    contract_folder = resolve(args.fleet_contract)

    print(
        "Verifying reference-cohort and fleet-contract source hashes..."
    )

    cohort = package(
        cohort_folder,
        "reference_car_cohort_report.json",
    )

    require(
        cohort.get("schema_version")
        == "reference_car_cohort_transfer_v1"
        and cohort.get("status") == "complete"
        and cohort.get("checks")
        and all(
            value is True
            for value in cohort["checks"].values()
        ),
        "A complete, checked reference-cohort package is required.",
    )

    require(
        cohort.get("class_export_cases") == 6
        and cohort.get("behaviour_input_origin") == "SYNTHETIC",
        "Expected the six-case synthetic-behaviour reference package.",
    )

    require(
        contract_folder / "fleet_contract_report.json" in watched,
        "Fleet contract is not bound to the supplied "
        "reference-cohort package.",
    )

    contract = package(
        contract_folder,
        "fleet_contract_report.json",
    )

    reference_file = (
        contract_folder / "public_fleet_reference_counts.csv"
    )

    require(
        reference_file.name in contract["output_sha256"],
        "Reference CSV is not hash-bound.",
    )

    matches = [
        row
        for row in read_csv(
            reference_file,
            (
                "evidence_id",
                "reference_year",
                "reported_value",
                "unit",
                "count_basis",
                "vehicle_label",
                "reported_fuel_category",
                "data_origin",
            ),
        )
        if row["evidence_id"] == "DMT2025_CAR_POPULATION"
    ]

    require(
        len(matches) == 1,
        "Expected exactly one DMT2025_CAR_POPULATION reference.",
    )

    reference = matches[0]

    require(
        reference["reference_year"] == "2025"
        and reference["count_basis"]
        == "registered_population_all_fuels"
        and reference["vehicle_label"] == "MOTOR CARS"
        and reference["reported_fuel_category"] == "all_fuels"
        and reference["unit"] == "vehicles"
        and reference["data_origin"] == "PUBLIC_REFERENCE",
        "Reference must be the 2025 all-fuel registered "
        "MOTOR CARS population.",
    )

    population = Decimal(reference["reported_value"])

    require(
        population.is_finite()
        and population > 0
        and population == population.to_integral_value(),
        "Invalid reference car population.",
    )

    rows = []

    for year in YEARS:
        shares = []

        for scenario, endpoint in zip(
            SCENARIOS,
            args.end_shares,
        ):
            share = (
                args.start_share
                + (endpoint - args.start_share)
                * Decimal(year - 2026)
                / Decimal(9)
            )

            shares.append(share)

            rows.append(
                {
                    "year": year,
                    "scenario": scenario,
                    "reference_population_year": 2025,
                    "reference_all_fuel_car_population": (
                        str(population)
                    ),
                    "proposed_electric_share_of_fixed_reference": (
                        str(share)
                    ),
                    "conditional_car_count": str(
                        (population * share).quantize(
                            Decimal("0.000001")
                        )
                    ),
                    "count_representation": "expected_vehicles",
                    "count_basis": "conditional_scenario_stock",
                    "value_status": "draft_study_assumption",
                    "reference_evidence_id": reference["evidence_id"],
                    "share_assumption_id": (
                        f"CAR-SHARE-{scenario.upper()}-01"
                    ),
                    "denominator_assumption_id": (
                        "CAR-FIXED-REFERENCE-01"
                    ),
                    "path_assumption_id": "CAR-LINEAR-SHARE-01",
                    "candidate_origin": (
                        "RESEARCH_SCENARIO_ASSUMPTION"
                    ),
                }
            )

        require(
            shares[0] <= shares[1] <= shares[2],
            "Scenario ordering failed.",
        )

    require(
        len(rows) == 30
        and len(
            {
                (row["year"], row["scenario"])
                for row in rows
            }
        ) == 30,
        "Year/scenario grid mismatch.",
    )

    assumptions = [
        {
            "assumption_id": "CAR-FIXED-REFERENCE-01",
            "parameter_or_rule": "reference_denominator",
            "value_or_rule": (
                f"Use {population} registered all-fuel motor "
                "cars from 2025 as a fixed reference scale "
                "for all candidate years."
            ),
            "rationale": (
                "Hold the denominator constant to isolate "
                "proposed penetration-share paths. This is "
                "not a future all-fuel population forecast."
            ),
            "evidence_ids": reference["evidence_id"],
            "sensitivity_plan": (
                "Compare updated reference populations or "
                "explicitly model denominator growth separately."
            ),
        },
        {
            "assumption_id": "CAR-LINEAR-SHARE-01",
            "parameter_or_rule": "annual_share_path",
            "value_or_rule": (
                "start_share + (end_share - start_share) "
                "* (year - 2026) / 9"
            ),
            "rationale": (
                "A transparent interpolation rule for a "
                "controlled what-if experiment; not a "
                "fitted adoption model."
            ),
            "evidence_ids": "",
            "sensitivity_plan": (
                "Compare delayed and nonlinear paths "
                "with the same endpoints."
            ),
        },
    ]

    for scenario, endpoint in zip(
        SCENARIOS,
        args.end_shares,
    ):
        assumptions.append(
            {
                "assumption_id": (
                    f"CAR-SHARE-{scenario.upper()}-01"
                ),
                "parameter_or_rule": "electric_share_endpoints",
                "value_or_rule": (
                    f"2026={args.start_share}; "
                    f"2035={endpoint}"
                ),
                "rationale": (
                    "Researcher-selected proposed experimental "
                    "levels. Not official projections, fitted "
                    "rates, confidence bounds or estimated "
                    "2026 EV stock."
                ),
                "evidence_ids": "",
                "sensitivity_plan": (
                    "Review and vary endpoint shares before "
                    "using the candidate in demand calculations."
                ),
            }
        )

    design = {
        "schema_version": "conditional_car_scenario_design_v1",
        "status": "draft_for_review",
        "vehicle_class_scope": "Car",
        "public_population_reference": reference,
        "scope_reference": rel(
            cohort_folder / "reference_cohort_scope.json"
        ),
        "years": list(YEARS),
        "scenarios": list(SCENARIOS),
        "start_share_2026": str(args.start_share),
        "end_shares_2035": {
            name: str(value)
            for name, value in zip(
                SCENARIOS,
                args.end_shares,
            )
        },
        "interpolation": (
            "linear_share_between_2026_and_2035"
        ),
        "denominator_policy": (
            "fixed_2025_reference_not_projected_future_population"
        ),
        "count_representation": "expected_vehicles",
        "count_decimal_places": 6,
        "count_origin": "RESEARCH_SCENARIO_ASSUMPTION",
        "scope_note": (
            "Car-only candidate experiment. Survey-to-registration "
            "class and powertrain transfer assumptions remain "
            "unverified."
        ),
        "not_an_EV_stock_baseline": True,
        "official_adoption_projection": False,
        "scenario_probabilities_assigned": False,
        "fleet_contract_activated": False,
        "district_allocations_created": False,
        "new_demand_simulations": 0,
    }

    unchanged()

    stamp = datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%S_%fZ"
    )

    output = (
        resolve(args.output_root)
        / f"conditional_car_scenario_design_{stamp}"
    )

    output.mkdir(
        parents=True,
        exist_ok=False,
    )

    outputs = []

    for name, data in (
        ("national_car_scenario_candidates.csv", rows),
        ("scenario_assumption_register.csv", assumptions),
    ):
        path = output / name

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

    design_path = output / "scenario_design.json"

    design_path.write_text(
        json.dumps(
            design,
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    outputs.append(design_path)

    lookup = {
        (row["year"], row["scenario"]): row
        for row in rows
    }

    preview = [
        "# Proposed conditional Car scenario design",
        "",
        "The published all-fuel motor-car count supplies the "
        "reference denominator. Shares and interpolation are "
        "proposed study assumptions. This package does not "
        "estimate the active EV fleet or fill the fleet "
        "input contract.",
        "",
        "| Year | Low expected cars | Medium expected cars "
        "| High expected cars |",
        "|---|---:|---:|---:|",
    ]

    for year in (2026, 2030, 2035):
        values = [
            f"{Decimal(lookup[year, name]['conditional_car_count']):,.2f}"
            for name in SCENARIOS
        ]

        preview.append(
            f"| {year} | "
            + " | ".join(values)
            + " |"
        )

    preview.extend(
        [
            "",
            "Counts retain six decimal places in the CSV. "
            "Fractions are expected scenario quantities; "
            "no integer fleet was generated. Low/medium/high "
            "label experimental share paths, not likelihoods "
            "or confidence limits.",
            "",
            "The fixed denominator is a controlled reference "
            "scale, not a claim that the future national "
            "motor-car population stays constant. The 2025 "
            "newly registered Electric-car cohort is not "
            "used as EV stock.",
            "",
            "Next decisions: review share endpoints and "
            "denominator policy; define and assess district "
            "allocation evidence/proxies and the behaviour-context "
            "to charging-place allocation. No district fleet "
            "counts, place weights, demand profiles or AI "
            "training are produced.",
            "",
        ]
    )

    preview_path = output / "scenario_review.md"

    preview_path.write_text(
        "\n".join(preview),
        encoding="utf-8",
    )

    outputs.append(preview_path)

    unchanged()

    report = {
        "schema_version": (
            "conditional_car_scenario_design_report_v1"
        ),
        "status": "draft_for_review",
        "candidate_rows": len(rows),
        "assumption_rows": len(assumptions),
        "source_reference_year": 2025,
        "source_all_fuel_car_population": int(population),
        "source_behaviour_owners": cohort["study_car_owners"],
        "source_replicates_per_case": (
            cohort["source_replicates_per_case"]
        ),
        "checks": {
            "source_hashes_verified": True,
            "reference_scope_checked": True,
            "complete_30_cell_grid": True,
            "bounded_ordered_shares": True,
            "source_files_unchanged": True,
        },
        "share_levels_are_research_assumptions": True,
        "future_EV_stock_estimated": False,
        "fleet_contract_activated": False,
        "district_demand_ready": False,
        "new_demand_simulations": 0,
        "empirical_validation": False,
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

    report_path = output / "scenario_design_report.json"

    report_path.write_text(
        json.dumps(
            report,
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    print("Conditional Car scenario draft prepared.")

    print(
        f"reference_all_fuel_cars_2025: {population}"
    )

    print(
        f"year_scenario_rows: {len(rows)} | "
        f"assumption_rows: {len(assumptions)}"
    )

    for year in (2026, 2030, 2035):
        values = ", ".join(
            f"{name}="
            f"{Decimal(lookup[year, name]['conditional_car_count']):,.2f}"
            for name in SCENARIOS
        )

        print(f"{year}: {values}")

    print(
        "Source integrity, reference scope, share ordering "
        "and grid checks passed."
    )

    print(f"Output: {rel(output)}")

    print(
        "Draft study scenarios only; fleet contract, district "
        "inputs and demand runs unchanged."
    )


if __name__ == "__main__":
    try:
        main()

    except (
        ValueError,
        OSError,
        KeyError,
        InvalidOperation,
        csv.Error,
    ) as error:
        raise SystemExit(
            f"Conditional Car scenario preparation failed: {error}"
        ) from error