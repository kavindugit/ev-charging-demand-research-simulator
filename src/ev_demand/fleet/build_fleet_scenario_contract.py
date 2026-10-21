"""Prepare a fleet input contract and unfilled scenario templates."""

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[3]

IDENTITY_SHA256 = (
    "da768930da9afe64f33d525b76e7252cf"
    "7cb7072f89d2016d1b04cfd38b2fd65"
)

YEARS = list(range(2026, 2036))
SCENARIOS = ["low", "medium", "high"]

COUNT_BASES = [
    "active_vehicle_stock",
    "registered_vehicle_population",
    "registration_cohort",
    "conditional_scenario_stock",
]

VALUE_STATUSES = [
    "official_projection",
    "model_derived_scenario",
    "engineering_assumption",
]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def project_path(value):
    path = (ROOT / value).resolve()
    require(
        path.is_relative_to(ROOT.resolve()),
        "Path outside the project.",
    )
    return path


def relative(path):
    return project_path(path).relative_to(ROOT.resolve()).as_posix()


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        require(
            reader.fieldnames
            and len(set(reader.fieldnames)) == len(reader.fieldnames),
            "Missing or duplicate CSV headers.",
        )
        return list(reader)


def write_csv(path, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_json(path, value):
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def verify_report(path, watched):
    report = json.loads(path.read_text(encoding="utf-8-sig"))
    require(
        report.get("input_sha256") and report.get("output_sha256"),
        "Incomplete source manifests.",
    )

    entries = [(path, digest(path))]
    entries += [
        (project_path(name), expected)
        for name, expected in report["input_sha256"].items()
    ]
    entries += [
        (project_path(path.parent / name), expected)
        for name, expected in report["output_sha256"].items()
    ]

    for item, expected in entries:
        require(
            item not in watched or watched[item] == expected,
            "Conflicting hashes.",
        )
        require(
            digest(item) == expected,
            "Changed source: " + relative(item),
        )
        watched[item] = expected

    return report


def reference_counts(folder):
    annual = read_csv(folder / "annual_vehicle_counts.csv")
    fuel = read_csv(folder / "fuel_registrations_2025.csv")

    selections = [
        (
            "DMT2025_ALL_POPULATION",
            "annual_vehicle_counts.csv",
            annual,
            dict(
                metric="vehicle_population",
                year="2025",
                vehicle_label="Total",
            ),
            "registered_population_all_fuels",
        ),
        (
            "DMT2025_CAR_POPULATION",
            "annual_vehicle_counts.csv",
            annual,
            dict(
                metric="vehicle_population",
                year="2025",
                vehicle_label="MOTOR CARS",
            ),
            "registered_population_all_fuels",
        ),
        (
            "DMT2025_ALL_NEW",
            "annual_vehicle_counts.csv",
            annual,
            dict(
                metric="new_registrations",
                year="2025",
                vehicle_label="Total",
            ),
            "annual_new_registrations",
        ),
        (
            "DMT2025_CAR_NEW",
            "annual_vehicle_counts.csv",
            annual,
            dict(
                metric="new_registrations",
                year="2025",
                vehicle_label="MOTOR CARS",
            ),
            "annual_new_registrations",
        ),
        (
            "DMT2025_CAR_E_NEW",
            "fuel_registrations_2025.csv",
            fuel,
            dict(
                vehicle_group="motor_cars",
                fuel_code="E",
                period="TOTAL",
            ),
            "annual_new_registrations",
        ),
        (
            "DMT2025_CAR_PE_NEW",
            "fuel_registrations_2025.csv",
            fuel,
            dict(
                vehicle_group="motor_cars",
                fuel_code="PE",
                period="TOTAL",
            ),
            "annual_new_registrations",
        ),
        (
            "DMT2025_MC_E_NEW",
            "fuel_registrations_2025.csv",
            fuel,
            dict(
                vehicle_group="motor_cycles",
                fuel_code="E",
                period="TOTAL",
            ),
            "annual_new_registrations",
        ),
    ]

    rows = []

    for evidence_id, filename, table, selector, basis in selections:
        found = [
            row for row in table
            if all(row[key] == value for key, value in selector.items())
        ]
        require(
            len(found) == 1
            and found[0]["value_state"] == "reported_number",
            "A public reference count is missing or unresolved.",
        )

        row = found[0]
        rows.append(
            dict(
                evidence_id=evidence_id,
                reference_year=2025,
                reported_value=int(row["reported_value"]),
                unit="vehicles",
                count_basis=basis,
                vehicle_label=row.get(
                    "vehicle_label", row.get("vehicle_group")
                ),
                reported_fuel_category=row.get(
                    "reported_fuel_category", "all_fuels"
                ),
                source_file=relative(folder / filename),
                source_file_sha256=digest(folder / filename),
                source_row_selector_json=json.dumps(
                    selector, sort_keys=True
                ),
                source_pdf_page=row["pdf_page"],
                source_table=row["source_table"],
                source_row=row["source_row"],
                source_column=row["source_column"],
                permitted_role=(
                    "historical_reference_only_"
                    "not_an_activated_EV_baseline"
                ),
                data_origin="PUBLIC_REFERENCE",
            )
        )

    return rows


def make_contract(districts):
    text = dict(type="string", minLength=1)
    nonnegative = dict(type="number", minimum=0)

    def obj(properties):
        return dict(
            type="object",
            additionalProperties=False,
            properties=properties,
            required=list(properties),
        )

    scope = obj(
        dict(
            scope_id=text,
            vehicle_classes=dict(
                type="array",
                minItems=1,
                uniqueItems=True,
                items=text,
            ),
            powertrain_scope=dict(
                enum=[
                    "BEV_only",
                    "BEV_and_PHEV",
                    "reported_Electric_category",
                ]
            ),
            count_basis=dict(enum=COUNT_BASES),
            national_method=dict(
                enum=[
                    "supplied_national_counts",
                    "conditional_penetration",
                ]
            ),
            district_fleet_basis=dict(
                enum=["owner_residence", "vehicle_registration"]
            ),
            count_representation=dict(
                enum=["expected_vehicles", "integer_vehicles"]
            ),
            integerization_reference=dict(type="string"),
            definition_evidence_id=text,
            behaviour_model_reference=text,
            behaviour_class_compatibility_reference=text,
        )
    )

    scope["allOf"] = [
        {
            "if": {
                "properties": {
                    "count_representation": {
                        "const": "integer_vehicles"
                    }
                }
            },
            "then": {
                "properties": {
                    "integerization_reference": text
                }
            },
        }
    ]

    national = obj(
        dict(
            year=dict(enum=YEARS),
            scenario=dict(enum=SCENARIOS),
            national_method=dict(
                enum=[
                    "supplied_national_counts",
                    "conditional_penetration",
                ]
            ),
            count_basis=dict(enum=COUNT_BASES),
            national_ev_count=dict(
                type=["number", "null"], minimum=0
            ),
            all_fuel_population=dict(
                type=["number", "null"], minimum=0
            ),
            ev_stock_share=dict(
                type=["number", "null"], minimum=0, maximum=1
            ),
            value_status=dict(enum=VALUE_STATUSES),
            evidence_id=text,
            assumption_id=dict(type="string"),
            rationale=text,
        )
    )

    national["oneOf"] = [
        {
            "properties": {
                "national_method": {
                    "const": "supplied_national_counts"
                },
                "national_ev_count": nonnegative,
                "all_fuel_population": {"type": "null"},
                "ev_stock_share": {"type": "null"},
            }
        },
        {
            "properties": {
                "national_method": {
                    "const": "conditional_penetration"
                },
                "count_basis": {
                    "const": "conditional_scenario_stock"
                },
                "national_ev_count": {"type": "null"},
                "all_fuel_population": nonnegative,
                "ev_stock_share": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                },
            }
        },
    ]

    national["allOf"] = [
        {
            "if": {
                "properties": {
                    "value_status": {
                        "enum": [
                            "model_derived_scenario",
                            "engineering_assumption",
                        ]
                    }
                }
            },
            "then": {
                "properties": {"assumption_id": text}
            },
        }
    ]

    district = obj(
        dict(
            district_id=dict(
                enum=[row["district_id"] for row in districts]
            ),
            district_name=text,
            province_name=text,
            year=dict(enum=YEARS),
            scenario=dict(enum=SCENARIOS),
            allocation_share=dict(
                type="number", minimum=0, maximum=1
            ),
            allocation_basis=dict(
                enum=["owner_residence", "vehicle_registration"]
            ),
            value_status=dict(enum=VALUE_STATUSES),
            evidence_id=text,
            assumption_id=dict(type="string"),
            rationale=text,
        )
    )

    district["allOf"] = national["allOf"]

    assumption = obj(
        dict(
            assumption_id=text,
            parameter_or_rule=text,
            applicable_scope=text,
            value_or_rule=text,
            rationale=text,
            evidence_ids=text,
            sensitivity_plan=text,
        )
    )

    contract = obj(
        dict(
            scope=scope,
            national_inputs=dict(
                type="array",
                minItems=30,
                maxItems=30,
                items=national,
            ),
            district_allocations=dict(
                type="array",
                minItems=750,
                maxItems=750,
                items=district,
            ),
            assumptions=dict(
                type="array",
                uniqueItems=True,
                items=assumption,
            ),
        )
    )

    contract.update(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "Fleet scenario input contract v1",
            "description": (
                "Schema for completed inputs, not for the "
                "deliberately unfilled templates."
            ),
            "x-contract-version": "fleet_scenario_input_contract_v1",
            "x-csv-conversion": (
                "Parse numbers explicitly; an unused numeric field "
                "becomes JSON null. A blank required numeric field "
                "is unresolved, never zero."
            ),
            "x-count-unit": "distinct_vehicles",
            "x-district-identities": districts,
            "x-semantic-rules": [
                "Require each (year,scenario) key exactly once in "
                "the national table; length alone does not "
                "establish coverage.",

                "Require each (district_id,year,scenario) key "
                "exactly once in the district table, with canonical "
                "district and province names.",

                "National method and count basis must agree with "
                "the scope declaration across every row.",

                "District allocation basis must agree with the "
                "scope declaration; fleet ownership/registration "
                "allocation is distinct from charging-location "
                "allocation.",

                "All 25 district shares must be supplied and sum "
                "to one for each year/scenario, with absolute "
                "tolerance 1e-9; do not fill, renormalize or drop "
                "missing districts.",

                "For conditional_penetration, compute "
                "national_ev_count = all_fuel_population * "
                "ev_stock_share in the same vehicle-class scope "
                "and target year.",

                "Compute expected district count = national count "
                "* district share; summed district counts must "
                "conserve the national count.",

                "Every evidence_id must resolve to a retained "
                "source or derivation with reference period, scope "
                "and file hashes; the seven historical references "
                "alone do not establish future counts or "
                "district shares.",

                "Every non-empty assumption_id must resolve to "
                "one unique assumption-register record; justify "
                "and sensitivity-test future assumptions.",

                "Record raw source counts as PUBLIC_REFERENCE "
                "and scenario values by value_status; simulated "
                "charging demand remains SIMULATED with "
                "behavioural input origin declared separately.",

                "Future national counts must be ordered low <= "
                "medium <= high within each year and common "
                "scope; no automatic monotonic growth over "
                "time is assumed.",

                "For integer_vehicles, require an explicit "
                "integerization rule and preserve national "
                "totals; do not silently round fractional "
                "expected counts.",

                "A registration_cohort is not the whole active "
                "or registered fleet; retain its identity in "
                "all downstream outputs.",

                "Scope-definition and behavioural-class "
                "compatibility references require substantive "
                "review; schema conformance does not establish "
                "empirical validity.",

                "Contract/schema conformance alone does not "
                "activate fleet inputs, spatial weights, "
                "charging-location mapping or district demand.",
            ],
        }
    )

    scope_template = {
        key: [] if key == "vehicle_classes" else ""
        for key in scope["properties"]
    }

    national_rows = [
        {
            key: (
                year if key == "year"
                else scenario if key == "scenario"
                else ""
            )
            for key in national["properties"]
        }
        for year in YEARS
        for scenario in SCENARIOS
    ]

    district_rows = []
    for year in YEARS:
        for scenario in SCENARIOS:
            for identity in districts:
                values = dict(identity, year=year, scenario=scenario)
                district_rows.append(
                    {
                        key: values.get(key, "")
                        for key in district["properties"]
                    }
                )

    assumption_rows = [
        {key: "" for key in assumption["properties"]}
    ]

    return (
        contract,
        scope_template,
        national_rows,
        district_rows,
        assumption_rows,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dmt", required=True)
    parser.add_argument("--district-identity", required=True)
    parser.add_argument(
        "--decision",
        default="docs/dmt_evidence_use_decisions.md",
    )
    args = parser.parse_args()

    folder = project_path(args.dmt)
    identity_path = project_path(args.district_identity)
    decision = project_path(args.decision)
    source_report = folder / "dmt_table_report.json"

    script = Path(__file__).resolve()
    watched = {script: digest(script)}
    report = verify_report(source_report, watched)

    require(
        report.get("schema") == "dmt_extracted_tables_v1"
        and report.get("status") in {
            "tables_extracted",
            "tables_extracted_with_review_flags",
        }
        and report.get("data_origin") == "PUBLIC_REFERENCE"
        and report.get("checks")
        and all(value is True for value in report["checks"].values()),
        "Unexpected DMT package.",
    )

    policy = decision.read_text(encoding="utf-8-sig")
    pins = re.findall(
        r"^Table-report SHA256:\s*([0-9a-f]{64})\s*$",
        policy,
        re.M,
    )
    require(
        pins == [digest(source_report)],
        "The decision document does not reference this DMT report.",
    )
    require(
        digest(identity_path) == IDENTITY_SHA256,
        "The district identity snapshot differs from the reviewed source.",
    )

    watched[identity_path] = digest(identity_path)
    watched[decision] = digest(decision)

    identities = read_csv(identity_path)
    require(
        len(identities) == 25
        and len({row["district_id"] for row in identities}) == 25
        and len({row["province_name"] for row in identities}) == 9,
        "Expected 25 unique districts and nine provinces.",
    )

    districts = sorted(
        [
            {
                key: row[key]
                for key in (
                    "district_id", "district_name", "province_name"
                )
            }
            for row in identities
        ],
        key=lambda row: row["district_id"],
    )

    references = reference_counts(folder)

    contract, scope, national, district, assumptions = make_contract(
        districts
    )

    require(
        len(national) == 30
        and len(district) == 750
        and len(
            {
                (row["district_id"], row["year"], row["scenario"])
                for row in district
            }
        ) == 750,
        "Incomplete planned configuration grid.",
    )

    require(
        all(
            digest(path) == expected
            for path, expected in watched.items()
        ),
        "An input changed during preparation.",
    )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    output = (
        ROOT
        / "data/processed/reference"
        / ("fleet_scenario_contract_" + stamp)
    )
    output.mkdir(parents=True, exist_ok=False)

    write_json(output / "fleet_input_contract.json", contract)
    write_json(output / "fleet_scope_template.json", scope)

    exports = {
        "national_fleet_inputs_template.csv": national,
        "district_fleet_allocation_inputs_template.csv": district,
        "assumption_register_template.csv": assumptions,
        "public_fleet_reference_counts.csv": references,
    }

    for name, rows in exports.items():
        write_csv(output / name, rows)

    guide = """# Fleet scenario input contract

The JSON Schema describes COMPLETED inputs. The CSV/JSON templates are
deliberately incomplete and will not pass full readiness validation.
Blank numbers are unresolved, not zero. No baseline, adoption rate,
district share, vehicle scope or integerization method is activated.

1. Define vehicle/powertrain scope and its compatibility with the behavioural model.
2. Choose supplied national counts OR conditional penetration; document count basis.
3. Supply 30 national year/scenario rows with evidence and any required assumptions.
4. Supply all 750 district shares; shares across 25 districts must sum to one per year/scenario.
5. Complete the assumption register and verify every evidence/assumption reference.
6. Implement/run both schema validation and the aggregate semantic checks before activation.

The seven public references retain historical DMT values and their source
selectors. They do not establish an active EV baseline or future values.
No example shares or growth rates are copied from the original plan.
District fleet shares are not within-district place-typology weights or
away-charging destination shares. Those mappings are separate downstream work.

Planned scope: 25 districts x 10 years x 3 scenarios = 750 configurations.
Four downstream place groups would produce 3,000 profile groups. These
are planned output groups, not simulations or independent populations;
replicates are an additional experimental dimension selected separately.
"""
    (output / "README.md").write_text(guide, encoding="utf-8")

    result = dict(
        schema="fleet_scenario_contract_package_v1",
        status="awaiting_fleet_inputs",
        created_utc=datetime.now(timezone.utc).isoformat(),
        artifact_role="input_contract_and_unfilled_templates",
        source_dmt_folder=relative(folder),
        district_identity_file=relative(identity_path),
        decision_document=relative(decision),
        districts=25,
        years=YEARS,
        scenarios=SCENARIOS,
        national_input_rows=30,
        district_input_rows=750,
        planned_configurations=750,
        planned_downstream_place_profile_groups=3000,
        public_reference_rows=len(references),
        retained_dmt_numerical_review_flags=(
            report["numerical_review_flags"]
        ),
        unresolved=dict(
            scope_fields=list(scope),
            national_count_rows=30,
            district_allocation_rows=750,
        ),
        checks=dict(
            source_hashes_verified=True,
            decision_report_binding=True,
            district_identity_pin_verified=True,
            complete_planned_grid=True,
            historical_reference_values_retained=True,
            input_files_unchanged=True,
        ),
        input_sha256={
            relative(path): expected
            for path, expected in watched.items()
        },
        output_sha256={
            path.name: digest(path)
            for path in sorted(output.iterdir())
        },
        schema_validation_of_completed_inputs_performed=False,
        semantic_validation_of_completed_inputs_performed=False,
        numerical_fleet_inputs_ready=False,
        behavioural_scope_compatibility_established=False,
        production_inputs_activated=False,
        district_demand_ready=False,
        national_active_electric_stock_established=False,
        new_demand_simulations=0,
    )

    write_json(output / "fleet_contract_report.json", result)

    print("Fleet scenario input contract prepared.")
    print("districts: 25 | years: 2026-2035 | scenarios: low, medium, high")
    print("planned_configurations: 750")
    print("planned_downstream_place_profile_groups: 3000")
    print("national_input_rows: 30 | district_allocation_rows: 750")
    print("public_reference_rows:", len(references))
    print("status: awaiting_fleet_inputs")
    print(
        "Source hashes, decision binding, district identities "
        "and planned-grid checks passed."
    )
    print("Output:", relative(output))
    print(
        "Contract preparation only; no fleet counts, adoption "
        "assumptions or demand simulations activated."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("Fleet contract preparation failed:", exc, file=sys.stderr)
        raise SystemExit(1)