"""Extract pinned DMT tables by ruled cells; retain source discrepancies."""

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import re
import sys

import pdfplumber

ROOT = Path(__file__).resolve().parents[3]

PINS = {
    "vehicle_population_2014_2025":
        "bed6a4b57ff05b8bb299d45e5fd5b260df54b773c672fd19d5e7b8d31714e757",
    "monthly_registrations_2025":
        "c17a23465c1499269827391fbae58fd5e5500797dff537a746195d42a3f0370d",
    "fuel_registrations_2025":
        "06529f2e5acf96fceeb0a4449a70b88965e0a12d1a01ad673022be026d9e212d",
}

# Expected table count, rows, columns, header rows and header fingerprint.
HEADERS = {
    "vehicle_population_2014_2025": (
        2, 19, 13, 1,
        "1babe31d717984ef1e5a25afdef05a02abcf716ebd9b74252516b903ac739019",
    ),
    "monthly_registrations_2025": (
        1, 25, 14, 2,
        "53e1416807e5021398ad4d01401620159112920af27305fc64fdd8298b776012",
    ),
    "fuel_registrations_2025": (
        1, 17, 43, 3,
        "005b2e710b98e24ba4339691f2b709aba8946362eed945ef544e2b26327352f2",
    ),
}

MONTHS = (
    "January February March April May June July August "
    "September October November December"
).split()

MONTH_CODES = (
    "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC TOTAL"
).split()

ANNUAL_LABELS = [
    "MOTOR CARS",
    "MOTOR TRICYCLES",
    "MOTOR CYCLES",
    "BUSES",
    "DUAL PURPOSE VEHICLES",
    "MOTOR LORRIES",
    "PRIME MOVERS",
    "LORRY TRAILER/BOWSER",
    "SPECIAL PURPOSE VEHICLE",
    "AMBULANCES",
    "HEARSES",
    "LAND VEHICLES",
    "N.A. TRACTORS",
    "TRACTOR TRAILER/BOWSER",
    "QUADRICYCLE",
    "MOTOR HOME",
    "OTHER SPECIAL PURPOSE VEHICLE",
    "Total",
]

MONTHLY_LABELS = [
    "CAR",
    "TRACTOR TRAILER/BOWSER",
    "DUAL PURPOSE VEHICLE",
    "PRIME MOVER",
    "LAND VEHICLE (Tractor)",
    "LAND VEHICLE SMALL(Hand Tractor)",
    "SPECIAL PURPOSE VEHICLE",
    "LORRY TRAILER/BOWSER",
    "THREE WHEELER",
    "QUADRICYCLE",
    "MOTOR HOME",
    "MOTOR CYCLE",
    "MOTOR LORRY (Commercial)",
    "SINGLE CAB",
    "MOTOR TRICYCLE VAN",
    "COMMERCIAL (Bus)",
    "AMBULANCE",
    "HEARSE",
    "NON AGRICULTURAL TRACTOR",
    "OTHER SPECIAL PURPOSE VEHICLE",
    "TOTAL",
    "Total No.of Cars less than 1000 CC & Electric",
]

# Reported column groups.
# These do not establish BEV/PHEV definitions or a vehicle-model mix.
FUEL_GROUPS = [
    ("motor_cars", "D P E DE PE"),
    ("three_wheelers", "D P PE E"),
    ("motor_tricycle_van", "D P E"),
    ("quadricycle", "P"),
    ("motor_home", "D"),
    ("buses", "D P"),
    ("motor_cycles", "P E PE"),
    ("dual_purpose", "D P E PE"),
    ("single_cab", "D P E"),
    ("special_purpose", "D P E"),
    ("lorry", "D P DE LPG"),
    ("prime_mover", "D"),
    ("ambulance_and_hearse", "D P PE"),
    ("lorry_trailer", "NF"),
    ("hand_tractor", "D"),
    ("tractor", "D"),
    ("tractor_trailer", "NF"),
    ("TOTAL", "ALL"),
]

FUEL_NAMES = dict(
    D="Diesel",
    P="Petrol",
    E="Electric",
    DE="Diesel/Electric",
    PE="Petrol/Electric",
    LPG="LP Gas",
    NF="Non Fuel",
    ALL="All reported fuels",
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def json_hash(value):
    serialized = json.dumps(
        value, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(serialized.encode()).hexdigest()


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
        return list(csv.DictReader(handle))


def write_csv(path, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def parse_cell(raw):
    token = "" if raw is None else re.sub(r"\s+", "", raw)

    if token in {"", "-"}:
        state = "blank" if token == "" else "reported_dash"
        return None, state

    require(
        bool(
            re.fullmatch(
                r"(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)",
                token,
            )
        ),
        "Unrecognized numeric cell: " + repr(raw),
    )
    return int(token.replace(",", "")), "reported_number"


def read_grids(paths):
    grids, audit = {}, []

    for source_id, path in paths.items():
        count, nrows, ncols, header_rows, header_hash = HEADERS[source_id]

        with pdfplumber.open(path) as pdf:
            require(len(pdf.pages) == 1, "Expected one PDF page.")

            tables = sorted(
                pdf.pages[0].find_tables(),
                key=lambda table: table.bbox[1],
            )
            matrices = [table.extract() for table in tables]

            require(
                len(matrices) == count
                and all(
                    len(table) == nrows
                    and all(len(row) == ncols for row in table)
                    for table in matrices
                ),
                "Table dimensions changed: " + source_id,
            )
            require(
                json_hash(
                    [table[:header_rows] for table in matrices]
                ) == header_hash,
                "Table headers or column positions changed: " + source_id,
            )

            grids[source_id] = matrices

            for index, (table, cells) in enumerate(
                zip(tables, matrices), 1
            ):
                audit.append(
                    dict(
                        source_id=source_id,
                        pdf_page=1,
                        source_table=index,
                        bbox=list(table.bbox),
                        cells=cells,
                        source_sha256=PINS[source_id],
                        data_origin="PUBLIC_REFERENCE",
                    )
                )

    return grids, audit


def extract(grids):
    annual, monthly, fuel, columns = [], [], [], []

    def cell(source_id, table, row, column, raw, **fields):
        value, state = parse_cell(raw)
        return dict(
            source_id=source_id,
            pdf_page=1,
            source_table=table,
            source_row=row,
            source_column=column,
            **fields,
            raw_cell=raw,
            reported_value=value,
            value_state=state,
            unit="vehicles",
            geography="Sri Lanka national",
            data_origin="PUBLIC_REFERENCE",
        )

    source_id = "vehicle_population_2014_2025"

    for index, table in enumerate(grids[source_id], 1):
        labels = ANNUAL_LABELS.copy()
        if index == 2:
            labels[7] = "LORRY TRAILERS"
            labels[13] = "TRAILERS & BOWSERS"

        require(
            [row[0] for row in table[1:]] == labels,
            "Annual row labels changed.",
        )

        for row_number, row in enumerate(table[1:], 2):
            for column, raw in enumerate(row[1:], 2):
                annual.append(
                    cell(
                        source_id, index, row_number, column, raw,
                        metric=(
                            "new_registrations"
                            if index == 1
                            else "vehicle_population"
                        ),
                        vehicle_label=row[0],
                        year=2012 + column,
                        row_role=(
                            "reported_total"
                            if row[0] == "Total"
                            else "vehicle_class"
                        ),
                    )
                )

    source_id = "monthly_registrations_2025"
    table = grids[source_id][0]

    require(
        [row[0] for row in table[2:24]] == MONTHLY_LABELS,
        "Monthly row labels changed.",
    )

    for row_number, row in enumerate(table[2:24], 3):
        role = (
            "reported_total" if row[0] == "TOTAL"
            else "overlapping_breakout" if row_number == 24
            else "vehicle_class"
        )

        for column, raw in enumerate(row[1:], 2):
            monthly.append(
                cell(
                    source_id, 1, row_number, column, raw,
                    metric="new_registrations",
                    vehicle_label=row[0],
                    year=2025,
                    period=MONTH_CODES[column - 2],
                    row_role=role,
                )
            )

    source_id = "fuel_registrations_2025"
    table = grids[source_id][0]

    require(
        [row[0] for row in table[3:16]] == MONTHS + ["Total"],
        "Fuel row labels changed.",
    )

    mapping = [
        (group, code)
        for group, codes in FUEL_GROUPS
        for code in codes.split()
    ]
    require(len(mapping) == 42, "Fuel column mapping is incomplete.")

    for column, (group, code) in enumerate(mapping, 2):
        columns.append(
            dict(
                source_column=column,
                vehicle_group=group,
                fuel_code=code,
                reported_fuel_category=FUEL_NAMES[code],
                raw_header_cells_json=json.dumps(
                    [row[column - 1] for row in table[:3]]
                ),
                classification_status="reported_label_only",
                data_origin="PUBLIC_REFERENCE",
            )
        )

        for row_number, row in enumerate(table[3:16], 4):
            fuel.append(
                cell(
                    source_id, 1, row_number, column, row[column - 1],
                    metric="new_registrations",
                    vehicle_group=group,
                    fuel_code=code,
                    reported_fuel_category=FUEL_NAMES[code],
                    year=2025,
                    period=(
                        "TOTAL" if row[0] == "Total" else row[0]
                    ),
                    column_role=(
                        "reported_total"
                        if group == "TOTAL"
                        else "class_and_fuel"
                    ),
                )
            )

    require(
        (len(annual), len(monthly), len(fuel)) == (432, 286, 546),
        "Unexpected extracted-cell accounting.",
    )
    return annual, monthly, fuel, columns


def reconcile(annual, monthly, fuel):
    checks = []

    def check(name, key, left, right):
        def subtotal(rows):
            values = [
                row["reported_value"]
                for row in rows
                if row["reported_value"] is not None
            ]
            return sum(values) if values else None

        a, b = subtotal(left), subtotal(right)
        gap = a - b if a is not None and b is not None else None

        status = (
            "numeric_subtotals_match" if gap == 0
            else "review" if (
                gap is not None
                or (a is None and b not in (None, 0))
                or (b is None and a not in (None, 0))
            )
            else "not_evaluable"
        )

        def references(rows):
            return [
                [
                    row["source_id"],
                    row["source_table"],
                    row["source_row"],
                    row["source_column"],
                ]
                for row in rows
            ]

        checks.append(
            dict(
                check=name,
                key=str(key),
                left_numeric_subtotal=a,
                right_numeric_subtotal=b,
                difference=gap,
                status=status,
                left_non_numeric_cells=sum(
                    row["reported_value"] is None for row in left
                ),
                right_non_numeric_cells=sum(
                    row["reported_value"] is None for row in right
                ),
                left_cells_json=json.dumps(references(left)),
                right_cells_json=json.dumps(references(right)),
                interpretation=(
                    "Observed numeric subtotals only; "
                    "blanks and dashes are not imputed."
                ),
                data_origin="PUBLIC_REFERENCE",
            )
        )

    for metric in ("new_registrations", "vehicle_population"):
        for year in range(2014, 2026):
            selected = [
                row for row in annual
                if row["metric"] == metric and row["year"] == year
            ]
            check(
                "annual_class_sum",
                f"{metric}/{year}",
                [
                    row for row in selected
                    if row["row_role"] == "reported_total"
                ],
                [
                    row for row in selected
                    if row["row_role"] == "vehicle_class"
                ],
            )

    for label in MONTHLY_LABELS:
        selected = [
            row for row in monthly if row["vehicle_label"] == label
        ]
        check(
            "monthly_row_sum",
            label,
            [row for row in selected if row["period"] == "TOTAL"],
            [row for row in selected if row["period"] != "TOTAL"],
        )

    for period in MONTH_CODES:
        selected = [
            row for row in monthly if row["period"] == period
        ]
        check(
            "monthly_class_sum",
            period,
            [
                row for row in selected
                if row["row_role"] == "reported_total"
            ],
            [
                row for row in selected
                if row["row_role"] == "vehicle_class"
            ],
        )

    for group, codes in FUEL_GROUPS:
        for code in codes.split():
            selected = [
                row for row in fuel
                if row["vehicle_group"] == group
                and row["fuel_code"] == code
            ]
            check(
                "fuel_column_sum",
                f"{group}/{code}",
                [row for row in selected if row["period"] == "TOTAL"],
                [row for row in selected if row["period"] != "TOTAL"],
            )

    for period in MONTHS + ["TOTAL"]:
        selected = [
            row for row in fuel if row["period"] == period
        ]
        check(
            "fuel_class_sum",
            period,
            [
                row for row in selected
                if row["column_role"] == "reported_total"
            ],
            [
                row for row in selected
                if row["column_role"] == "class_and_fuel"
            ],
        )

        short = (
            "TOTAL" if period == "TOTAL"
            else MONTH_CODES[MONTHS.index(period)]
        )

        check(
            "monthly_vs_fuel_total",
            period,
            [
                row for row in monthly
                if row["period"] == short
                and row["row_role"] == "reported_total"
            ],
            [
                row for row in selected
                if row["column_role"] == "reported_total"
            ],
        )

    check(
        "annual_vs_monthly_total",
        2025,
        [
            row for row in annual
            if row["metric"] == "new_registrations"
            and row["year"] == 2025
            and row["row_role"] == "reported_total"
        ],
        [
            row for row in monthly
            if row["period"] == "TOTAL"
            and row["row_role"] == "reported_total"
        ],
    )
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", required=True)
    args = parser.parse_args()

    require(
        version("pdfplumber") == "0.11.9",
        "Install the tested pdfplumber==0.11.9 version.",
    )

    folder = project_path(args.sources)
    source_report = folder / "dmt_source_report.json"
    report = json.loads(
        source_report.read_text(encoding="utf-8-sig")
    )

    require(
        report.get("schema") == "dmt_source_snapshot_v1"
        and report.get("status") == "source_snapshots_inspected"
        and report.get("data_origin") == "PUBLIC_REFERENCE"
        and report.get("source_documents") == 3
        and report.get("checks")
        and all(value is True for value in report["checks"].values()),
        "Unexpected DMT source package.",
    )

    script = Path(__file__).resolve()
    watched = {
        source_report: digest(source_report),
        script: digest(script),
    }

    entries = [
        (project_path(name), expected)
        for name, expected in report["input_sha256"].items()
    ]
    entries += [
        (project_path(folder / name), expected)
        for name, expected in report["output_sha256"].items()
    ]

    for path, expected in entries:
        require(
            path not in watched or watched[path] == expected,
            "Conflicting source hashes.",
        )
        require(
            digest(path) == expected,
            "Changed source: " + relative(path),
        )
        watched[path] = expected

    manifest = read_csv(folder / "dmt_source_manifest.csv")
    require(
        len(manifest) == 3
        and {row["source_id"] for row in manifest} == set(PINS),
        "Incomplete manifest.",
    )

    paths = {}
    for row in manifest:
        source_id = row["source_id"]
        path = project_path(row["raw_file"])
        require(
            row["sha256"] == PINS[source_id] == digest(path)
            and watched.get(path) == PINS[source_id],
            "Unpinned PDF source.",
        )
        paths[source_id] = path

    print(
        "Reading ruled PDF tables and preserving non-numeric cells...",
        flush=True,
    )

    grids, audit = read_grids(paths)
    annual, monthly, fuel, columns = extract(grids)
    checks = reconcile(annual, monthly, fuel)

    require(
        all(
            digest(path) == expected
            for path, expected in watched.items()
        ),
        "An input changed during extraction.",
    )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    output = (
        ROOT / "data/processed/reference" / ("dmt_tables_" + stamp)
    )
    output.mkdir(parents=True, exist_ok=False)

    exports = {
        "annual_vehicle_counts.csv": annual,
        "monthly_registrations_2025.csv": monthly,
        "fuel_registrations_2025.csv": fuel,
        "fuel_column_dictionary.csv": columns,
        "dmt_table_checks.csv": checks,
    }

    for name, rows in exports.items():
        write_csv(output / name, rows)

    with (
        output / "dmt_source_cell_grids.jsonl"
    ).open("w", encoding="utf-8") as handle:
        for row in audit:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    flags = [
        row for row in checks if row["status"] == "review"
    ]

    result = dict(
        schema="dmt_extracted_tables_v1",
        status=(
            "tables_extracted_with_review_flags"
            if flags else "tables_extracted"
        ),
        created_utc=datetime.now(timezone.utc).isoformat(),
        data_origin="PUBLIC_REFERENCE",
        source_folder=relative(folder),
        rows={
            name: len(rows) for name, rows in exports.items()
        },
        source_table_grids=len(audit),
        check_status_counts=dict(
            Counter(row["status"] for row in checks)
        ),
        numerical_review_flags=flags,
        value_states=dict(
            Counter(
                row["value_state"]
                for row in annual + monthly + fuel
            )
        ),
        software={
            name: version(name)
            for name in ("pdfplumber", "pdfminer.six")
        },
        checks=dict(
            source_hashes_verified=True,
            pinned_pdfs_verified=True,
            table_headers_and_dimensions_verified=True,
            source_labels_retained=True,
            complete_cell_grids=True,
            inputs_unchanged=True,
        ),
        input_sha256={
            relative(path): expected
            for path, expected in watched.items()
        },
        output_sha256={
            path.name: digest(path)
            for path in sorted(output.iterdir())
        },
        blank_or_dash_values_imputed=False,
        numerical_consistency_review_complete=False,
        fuel_category_definitions_review_complete=False,
        national_active_electric_stock_established=False,
        district_electric_stock_established=False,
        adoption_scenarios_created=False,
        district_demand_ready=False,
        new_demand_simulations=0,
        interpretation_limits=[
            "Numeric subtotals exclude non-numeric cells without "
            "converting them to zero; matching subtotals do not "
            "resolve dash or blank semantics.",
            "Whitespace inside a single ruled numeric cell is "
            "removed for parsing; the original cell text is retained.",
            "The cars-under-1000-cc-and-electric row is an overlapping "
            "breakout, not an electric-only count or an additional "
            "vehicle class.",
            "Population, registration flow and mixed-fuel categories "
            "remain distinct; no active BEV/PHEV fleet is inferred.",
            "Vehicle classes differ between documents; only national "
            "totals are compared across documents in this step.",
            "These tables do not establish district EV counts, "
            "battery sizes, vehicle models or future adoption rates.",
            "Raw PDF and source-package files remain unchanged; "
            "unresolved published values are retained.",
        ],
    )

    (output / "dmt_table_report.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print("DMT table extraction and reconciliation complete.")
    print("annual_vehicle_count_rows:", len(annual))
    print("monthly_registration_rows:", len(monthly))
    print("fuel_registration_rows:", len(fuel))
    print(
        "non_numeric_cells_retained:",
        sum(
            row["reported_value"] is None
            for row in annual + monthly + fuel
        ),
    )
    print("numerical_review_flags:", len(flags))

    for row in flags:
        print(
            f"REVIEW {row['check']} | {row['key']}: "
            f"{row['left_numeric_subtotal']} versus "
            f"{row['right_numeric_subtotal']}"
        )

    print(
        "Source hashes, table layouts, cell accounting and "
        "input-integrity checks passed; review flags retained."
    )
    print("Output:", relative(output))
    print(
        "Public table extraction only; active electric fleet, "
        "district counts and adoption scenarios remain unestablished."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("DMT table extraction failed:", exc, file=sys.stderr)
        raise SystemExit(1)