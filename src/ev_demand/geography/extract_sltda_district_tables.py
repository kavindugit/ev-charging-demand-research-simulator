"""Extract pinned SLTDA district tables, retaining published values and review flags."""
import argparse
import csv
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
import math
from pathlib import Path
import re
import sys

from pypdf import PdfReader


ROOT = Path(__file__).resolve().parents[3]

PINS = {
    2024: "65fde930d94619d2ffdf5df02a64bb2102a5dd43676fb0c73a77e03f6026c0bf",
    2025: "4a0f77c2824a150b353d7f070defa905e09efb1f277d98358ba8eced3cce3727",
}

MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

NUMBER = re.compile(r"[0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def norm(text):
    return " ".join(text.split()).casefold()


def project_path(value):
    path = (ROOT / value).resolve()
    require(path.is_relative_to(ROOT.resolve()), "Path is outside the project.")
    return path


def relative(path):
    return path.resolve().relative_to(ROOT.resolve()).as_posix()


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def verify_report(path, watched):
    report = json.loads(path.read_text(encoding="utf-8-sig"))

    require(
        report.get("input_sha256") and report.get("output_sha256"),
        "Incomplete upstream manifests: " + str(path),
    )

    entries = [(path, digest(path))]

    entries += [
        (project_path(name), expected)
        for name, expected in report["input_sha256"].items()
    ]

    entries += [
        (path.parent / name, expected)
        for name, expected in report["output_sha256"].items()
    ]

    for item, expected in entries:
        item = project_path(item)

        require(
            item not in watched or watched[item] == expected,
            "Conflicting upstream hashes.",
        )

        require(
            digest(item) == expected,
            "Changed or missing input: " + relative(item),
        )

        watched[item] = expected

    return report


def name_lookup(identity):
    require(
        len(identity) == 25
        and len({row["district_id"] for row in identity}) == 25,
        "Expected 25 unique DCS-linked districts.",
    )

    require(
        len({row["province_name"] for row in identity}) == 9,
        "Expected nine provinces.",
    )

    lookup = {
        norm(row["district_name"]): row["district_id"]
        for row in identity
    }

    require(len(lookup) == 25, "Duplicate canonical district names.")

    for alias, canonical in {
        "monaragala": "moneragala",
        "rathnapura": "ratnapura",
    }.items():
        require(canonical in lookup, "Missing alias target: " + canonical)
        lookup[alias] = lookup[canonical]

    return lookup


def numbers(tokens):
    require(
        all(NUMBER.fullmatch(token) for token in tokens),
        "Unexpected numeric table token.",
    )

    return [float(token.replace(",", "")) for token in tokens]


def named_table(text, lookup, fields, total_fields):
    """Read the district section without mixing in a province table."""
    lines = [
        " ".join(line.split())
        for line in text.splitlines()
        if line.strip()
    ]

    starts = [
        index
        for index, line in enumerate(lines)
        if line.startswith("District")
    ]

    require(len(starts) == 1, "District table header is not unique.")

    rows = {}

    for index in range(starts[0] + 1, len(lines)):
        line = lines[index]

        if norm(line) in {"total", "grand total"}:
            require(index + 1 < len(lines), "Missing table total.")
            line += " " + lines[index + 1]

        match = re.fullmatch(
            r"([A-Za-z][A-Za-z ]*?)\s+([0-9][0-9,. ]*)",
            line,
        )

        if not match:
            continue

        name, tail = match.groups()

        key = (
            "TOTAL"
            if norm(name) in {"total", "grand total"}
            else lookup.get(norm(name))
        )

        require(key is not None, "Unmapped district label: " + name)
        require(key not in rows, "Duplicate source district: " + name)

        tokens = tail.split()

        require(
            len(tokens) == (total_fields if key == "TOTAL" else fields),
            "Unexpected column count for " + name,
        )

        rows[key] = dict(
            source_name=name,
            tokens=tokens,
            values=numbers(tokens),
            raw_line=line,
        )

        if key == "TOTAL":
            return rows

    raise ValueError("No reported total found in the selected table.")


def total_line(text, prefix, fields):
    lines = [
        " ".join(line.split())
        for line in text.splitlines()
        if line.strip()
    ]

    candidates = [
        line for line in lines if line.startswith(prefix + " ")
    ]

    require(
        len(candidates) == 1,
        "Auxiliary table total is not unique: " + prefix,
    )

    tokens = candidates[0][len(prefix):].split()

    require(
        len(tokens) == fields,
        "Unexpected auxiliary total columns.",
    )

    return dict(
        source_name=prefix,
        tokens=tokens,
        values=numbers(tokens),
        raw_line=candidates[0],
    )


def integer(value):
    require(
        math.isfinite(value) and value >= 0 and value.is_integer(),
        "Invalid reported count.",
    )
    return int(value)


def extract(readers, identity):
    lookup = name_lookup(identity)
    tables, audits, checks, totals = {}, [], [], []

    definitions = {
        "rooms_2024": (
            2024, 20, "Table 09: Total rooms by districts", 2, 1
        ),
        "rooms_2025": (
            2025, 36, "Table 12: Total rooms by districts, 2025", 1, 1
        ),
        "occupancy_months": (
            2024, 42,
            "Table 19: Occupancy of graded establishments",
            14, 14,
        ),
        "occupancy_annual": (
            2024, 43, "Table 20 : Occupancy by districts", 1, 1
        ),
    }

    for key, definition in definitions.items():
        year, page, caption, fields, total_fields = definition
        text = readers[year].pages[page - 1].extract_text() or ""

        require(
            norm(caption) in norm(text),
            "Selected PDF table caption changed: " + key,
        )

        tables[key] = named_table(
            text, lookup, fields, total_fields
        )

        for district_id, row in tables[key].items():
            audits.append(dict(
                table_key=key,
                report_year=year,
                pdf_page=page,
                source_name=row["source_name"],
                district_id="" if district_id == "TOTAL" else district_id,
                source_row_kind=(
                    "reported_total"
                    if district_id == "TOTAL"
                    else "district"
                ),
                raw_line=row["raw_line"],
                raw_tokens_json=json.dumps(row["tokens"]),
                page_text_sha256=hashlib.sha256(
                    text.encode("utf-8")
                ).hexdigest(),
                data_origin="PUBLIC_REFERENCE",
            ))

    def check(name, left, right, tolerance, detail, district_id=""):
        gap = left - right

        checks.append(dict(
            check=name,
            district_id=district_id,
            left_value=left,
            right_value=right,
            difference=gap,
            tolerance=tolerance,
            status=(
                "review"
                if abs(gap) > tolerance
                else "within_declared_tolerance"
            ),
            detail=detail,
            data_origin="PUBLIC_REFERENCE",
        ))

    def published(
        table, report_year, reference_year, page,
        metric, unit, value, month="",
    ):
        totals.append(dict(
            table_key=table,
            report_year=report_year,
            reference_year=reference_year,
            pdf_page=page,
            metric=metric,
            month=month,
            reported_value=value,
            unit=unit,
            data_origin="PUBLIC_REFERENCE",
        ))

    ids = {row["district_id"] for row in identity}
    capacity = []

    for year in (2024, 2025):
        table = tables[f"rooms_{year}"]

        require(
            set(table) - {"TOTAL"} == ids,
            "Room table does not cover all 25 districts.",
        )

        for row in identity:
            source = table[row["district_id"]]
            value = integer(source["values"][0])

            capacity.append({
                **{
                    key: row[key]
                    for key in (
                        "district_id", "district_name", "province_name"
                    )
                },
                "reference_year": year,
                "source_district_name": source["source_name"],
                "registered_room_count": value,
                "published_room_share_percent": (
                    source["values"][1] if year == 2024 else None
                ),
                "report_year": year,
                "pdf_page": definitions[f"rooms_{year}"][1],
                "table_key": f"rooms_{year}",
                "unit": "rooms",
                "data_origin": "PUBLIC_REFERENCE",
            })

            if year == 2024:
                decimals = len(
                    source["tokens"][1].partition(".")[2]
                )
                tolerance = 0.5 * 10 ** (-decimals) + 1e-10

                check(
                    "published_room_share_2024",
                    source["values"][1],
                    100 * value / table["TOTAL"]["values"][0],
                    tolerance,
                    "Printed share versus room-count share; "
                    "rounding-method review only.",
                    row["district_id"],
                )

        total = integer(table["TOTAL"]["values"][0])

        check(
            f"district_room_sum_{year}",
            sum(
                integer(value["values"][0])
                for key, value in table.items()
                if key != "TOTAL"
            ),
            total,
            0,
            "Sum of district rows versus the published room total.",
        )

        published(
            f"rooms_{year}",
            year,
            year,
            definitions[f"rooms_{year}"][1],
            "rooms",
            "rooms",
            total,
        )

    # Retain comparative totals without replacing either district table.
    auxiliary_tables = (
        (
            "national_categories_2025",
            31,
            "Total",
            4,
            "Table 10: SLTDA registered accommodation establishments",
        ),
        (
            "national_provinces_2025",
            33,
            "Total number of rooms",
            2,
            "Table 11: Total rooms by province",
        ),
    )

    for key, page, prefix, count, caption in auxiliary_tables:
        text = readers[2025].pages[page - 1].extract_text() or ""

        require(
            norm(caption) in norm(text),
            "Auxiliary caption changed.",
        )

        source = total_line(text, prefix, count)

        audits.append(dict(
            table_key=key,
            report_year=2025,
            pdf_page=page,
            source_name=prefix,
            district_id="",
            source_row_kind="reported_total",
            raw_line=source["raw_line"],
            raw_tokens_json=json.dumps(source["tokens"]),
            page_text_sha256=hashlib.sha256(
                text.encode("utf-8")
            ).hexdigest(),
            data_origin="PUBLIC_REFERENCE",
        ))

        room_indices = (1, 3) if count == 4 else (0, 1)

        for year, index in zip((2025, 2024), room_indices):
            value = integer(source["values"][index])

            published(
                key, 2025, year, page, "rooms", "rooms", value
            )

            check(
                f"{key}_room_total_for_{year}",
                value,
                tables[f"rooms_{year}"]["TOTAL"]["values"][0],
                0,
                "Comparative national total versus same-year "
                "district-table total; source scope or revision may differ.",
            )

        if count == 4:
            for year, index in ((2025, 0), (2024, 2)):
                published(
                    key, 2025, year, page,
                    "establishments", "establishments",
                    integer(source["values"][index]),
                )

    monthly = tables["occupancy_months"]
    annual = tables["occupancy_annual"]

    require(
        set(monthly) == set(annual) and len(monthly) == 23,
        "Occupancy source grids differ.",
    )

    require(
        all(
            0 <= value <= 100
            for row in monthly.values()
            for value in row["values"][1:]
        )
        and all(
            0 <= row["values"][0] <= 100
            for row in annual.values()
        ),
        "Occupancy outside reported 0-100 scale.",
    )

    responding = integer(monthly["TOTAL"]["values"][0])

    check(
        "occupancy_reporting_establishments",
        sum(
            integer(row["values"][0])
            for key, row in monthly.items()
            if key != "TOTAL"
        ),
        responding,
        0,
        "Sum of reported district hotel counts versus source total.",
    )

    for district_id, row in monthly.items():
        values = row["values"]
        output_id = "" if district_id == "TOTAL" else district_id

        check(
            "annual_occupancy_tables_agree",
            values[-1],
            annual[district_id]["values"][0],
            0,
            "Table 19 annual column versus Table 20 annual value.",
            output_id,
        )

        check(
            "annual_vs_simple_monthly_mean",
            values[-1],
            math.fsum(values[1:13]) / 12,
            0.1000001,
            "Diagnostic only: source annual weighting and denominator "
            "are not specified in these tables.",
            output_id,
        )

    published(
        "occupancy_months", 2024, 2024, 42,
        "responding_establishments", "establishments", responding,
    )

    for month, value in enumerate(
        monthly["TOTAL"]["values"][1:13], 1
    ):
        published(
            "occupancy_months", 2024, 2024, 42,
            "occupancy", "reported_0_to_100_scale", value, month,
        )

    published(
        "occupancy_months", 2024, 2024, 42,
        "annual_occupancy", "reported_0_to_100_scale",
        monthly["TOTAL"]["values"][-1],
    )

    published(
        "occupancy_annual", 2024, 2024, 43,
        "annual_occupancy", "reported_0_to_100_scale",
        annual["TOTAL"]["values"][0],
    )

    occupancy, months, missing = [], [], []

    for row in identity:
        district_id = row["district_id"]
        source = monthly.get(district_id)

        if source is None:
            missing.append(row["district_name"])

        base = {
            key: row[key]
            for key in (
                "district_id", "district_name", "province_name"
            )
        }

        status = (
            "reported"
            if source
            else "not_listed_in_selected_occupancy_tables"
        )

        occupancy.append({
            **base,
            "reference_year": 2024,
            "coverage_status": status,
            "responding_establishments": (
                integer(source["values"][0]) if source else None
            ),
            "annual_occupancy_table19": (
                source["values"][-1] if source else None
            ),
            "annual_occupancy_table20": (
                annual[district_id]["values"][0] if source else None
            ),
            "unit": "reported_0_to_100_scale",
            "denominator_status": "not_defined_in_selected_tables",
            "pdf_pages": "42;43",
            "data_origin": "PUBLIC_REFERENCE",
        })

        for month, name in enumerate(MONTHS, 1):
            months.append({
                **base,
                "reference_year": 2024,
                "month": month,
                "month_name": name,
                "coverage_status": status,
                "occupancy_reported": (
                    source["values"][month] if source else None
                ),
                "unit": "reported_0_to_100_scale",
                "pdf_page": 42,
                "table_key": "occupancy_months",
                "data_origin": "PUBLIC_REFERENCE",
            })

    return dict(
        capacity=capacity,
        occupancy=occupancy,
        months=months,
        totals=totals,
        audits=audits,
        checks=checks,
        missing=missing,
        responding=responding,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument("--sources", required=True)

    parser.add_argument(
        "--identity",
        required=True,
        help="DCS-linked district_boundary_identity_audit.csv",
    )

    args = parser.parse_args()

    source_folder = project_path(args.sources)
    identity_path = project_path(args.identity)

    script = Path(__file__).resolve()
    watched = {script: digest(script)}

    source_report = verify_report(
        source_folder / "sltda_source_report.json",
        watched,
    )

    require(
        source_report.get("schema") == "sltda_source_snapshot_v1"
        and source_report.get("status") == "source_snapshots_inspected",
        "Unexpected source report.",
    )

    boundary_report = verify_report(
        identity_path.parent / "district_boundary_report.json",
        watched,
    )

    require(
        boundary_report.get("schema_version")
        == "district_boundary_identity_v1"
        and identity_path.name in boundary_report["output_sha256"],
        "Unverified district identity source.",
    )

    manifest = read_csv(
        source_folder / "sltda_source_manifest.csv"
    )

    by_year = {
        int(row["report_year"]): row
        for row in manifest
    }

    require(
        len(manifest) == 2 and set(by_year) == {2024, 2025},
        "Both pinned report years are required.",
    )

    for year, row in by_year.items():
        require(
            row["sha256"] == PINS[year],
            "Unsupported source PDF revision.",
        )

        path = project_path(row["raw_file"])

        require(
            path in watched and watched[path] == PINS[year],
            "PDF is absent from the verified source manifest.",
        )

    readers = {
        year: PdfReader(project_path(row["raw_file"]))
        for year, row in by_year.items()
    }

    require(
        len(readers[2024].pages) == 44
        and len(readers[2025].pages) == 70,
        "PDF page counts changed.",
    )

    result = extract(readers, read_csv(identity_path))

    require(
        all(
            digest(path) == expected
            for path, expected in watched.items()
        ),
        "An input changed during extraction.",
    )

    stamp = datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%S_%fZ"
    )

    output = (
        ROOT / "data/processed/reference"
        / ("sltda_district_tables_" + stamp)
    )

    output.mkdir(parents=True, exist_ok=False)

    exports = {
        "district_room_capacity.csv": "capacity",
        "district_occupancy_2024.csv": "occupancy",
        "monthly_occupancy_2024.csv": "months",
        "published_table_totals.csv": "totals",
        "sltda_table_row_audit.csv": "audits",
        "sltda_table_checks.csv": "checks",
    }

    for name, key in exports.items():
        write_csv(output / name, result[key])

    flags = [
        row
        for row in result["checks"]
        if row["status"] == "review"
    ]

    report = dict(
        schema="sltda_district_tables_v1",
        status="tables_extracted_with_review_flags",
        created_utc=datetime.now(timezone.utc).isoformat(),
        data_origin="PUBLIC_REFERENCE",
        source_folder=relative(source_folder),
        district_identity_file=relative(identity_path),
        rows={
            key: len(result[key])
            for key in (
                "capacity", "occupancy", "months",
                "totals", "audits", "checks",
            )
        },
        districts=25,
        capacity_reference_years=[2024, 2025],
        occupancy_reference_year=2024,
        occupancy_districts_reported=25 - len(result["missing"]),
        occupancy_missing_districts=result["missing"],
        responding_establishments=result["responding"],
        observed_monthly_occupancy_values=sum(
            row["occupancy_reported"] is not None
            for row in result["months"]
        ),
        missing_monthly_occupancy_values=sum(
            row["occupancy_reported"] is None
            for row in result["months"]
        ),
        numerical_review_flags=flags,
        published_values_replaced=False,
        missing_values_imputed=False,
        bed_capacity_established=False,
        district_establishment_counts_established=False,
        occupancy_denominator_review_complete=False,
        numerical_consistency_review_complete=False,
        typology_weights_created=False,
        district_demand_ready=False,
        software=dict(
            python=sys.version.split()[0],
            pypdf=version("pypdf"),
        ),
        input_sha256={
            relative(path): expected
            for path, expected in watched.items()
        },
        output_sha256={
            path.name: digest(path)
            for path in sorted(output.glob("*.csv"))
        },
        interpretation_limits=[
            "Room counts are not beds or district establishment counts.",
            "Occupancy describes the listed responding graded "
            "establishments; absent districts remain missing.",
            "The selected occupancy tables do not specify room/bed "
            "denominator or annual weighting; simple-monthly-mean "
            "checks are diagnostics.",
            "Comparative totals and rounding differences are retained "
            "for review, without assuming a source error.",
            "Capacity, occupancy and mapped OSM objects are distinct "
            "measures; no tourist-vehicle or charging-demand scaling "
            "is performed.",
        ],
    )

    (output / "sltda_table_report.json").write_text(
        json.dumps(report, indent=2) + "\n",
        encoding="utf-8",
    )

    print("SLTDA district table extraction complete.")
    print("district_room_rows:", len(result["capacity"]))
    print(
        "occupancy_districts_reported:",
        report["occupancy_districts_reported"],
    )
    print("responding_establishments:", result["responding"])
    print(
        "observed_monthly_values:",
        report["observed_monthly_occupancy_values"],
    )
    print("missing_occupancy_districts:", result["missing"])
    print("numerical_review_flags:", len(flags))

    print(
        "Source hashes, district joins and extraction-accounting "
        "checks passed; review flags retained."
    )

    print("Output:", relative(output))

    print(
        "Public table extraction only; definitions, source differences "
        "and demand scaling remain under review."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            "SLTDA table extraction failed:",
            exc,
            file=sys.stderr,
        )
        raise SystemExit(1)