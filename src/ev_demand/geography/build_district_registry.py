"""Build a sourced district reference and optionally audit owner residence keys."""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import unicodedata
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

ROOT = Path(__file__).resolve().parents[3]

SOURCE_URL = "https://www.statistics.gov.lk/qlink/AdminDivCodes_Excel"
SOURCE_PAGE = "https://www.statistics.gov.lk/qlink/AdminDivCode"
SOURCE_ID = "DCS_ADMINISTRATIVE_DIVISION_CODES"

NS = {
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
}
MAX_DOWNLOAD = 10 * 1024 * 1024

PROVINCES = dict(zip("123456789", (
    "Western",
    "Central",
    "Southern",
    "Northern",
    "Eastern",
    "North Western",
    "North Central",
    "Uva",
    "Sabaragamuwa",
)))

EXPECTED = dict(item.split(":", 1) for item in (
    "11:Colombo",
    "12:Gampaha",
    "13:Kalutara",
    "21:Kandy",
    "22:Matale",
    "23:Nuwara Eliya",
    "31:Galle",
    "32:Matara",
    "33:Hambantota",
    "41:Jaffna",
    "42:Mannar",
    "43:Vavuniya",
    "44:Mullaitivu",
    "45:Kilinochchi",
    "51:Batticaloa",
    "52:Ampara",
    "53:Trincomalee",
    "61:Kurunegala",
    "62:Puttalam",
    "71:Anuradhapura",
    "72:Polonnaruwa",
    "81:Badulla",
    "82:Moneragala",
    "91:Ratnapura",
    "92:Kegalle",
))

NAME_VARIANTS = {
    "Moneragala": ("Monaragala",),
    "Ratnapura": ("Rathnapura", "Ratnapure"),
    "Hambantota": ("Hambanthota",),
    "Nuwara Eliya": ("NuwaraEliya",),
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalize_name(value):
    text = unicodedata.normalize("NFKC", value).casefold().strip()
    text = re.sub(r"[\s_-]+", " ", text)
    text = re.sub(r"^district of\s+", "", text)
    return re.sub(r"\s+district$", "", text).strip()


def make_aliases():
    lookup, rows = {}, []

    for code, name in sorted(EXPECTED.items()):
        for alias in (name,) + NAME_VARIANTS.get(name, ()):
            key = normalize_name(alias)

            require(
                key not in lookup,
                "Duplicate or ambiguous district alias: " + alias,
            )

            lookup[key] = code

            rows.append(dict(
                alias=alias,
                normalized_alias=key,
                district_id="DCS-" + code,
                district_name=name,
                alias_kind=(
                    "canonical"
                    if alias == name
                    else "declared_spelling_variant"
                ),
            ))

    return lookup, rows


def read_reference(path):
    lookup, _ = make_aliases()
    registry, checked = {}, 0

    with ZipFile(path) as book:
        require(
            sum(item.file_size for item in book.infolist())
            <= 100 * 1024 * 1024,
            "Unexpectedly large administrative workbook.",
        )

        workbook = ET.fromstring(book.read("xl/workbook.xml"))

        require(
            [
                sheet.get("name")
                for sheet in workbook.findall(
                    "m:sheets/m:sheet", NS
                )
            ] == ["GNDList"],
            "DCS workbook sheets changed; review the source format.",
        )

        shared = [
            "".join(
                text.text or ""
                for text in item.findall(".//m:t", NS)
            )
            for item in ET.fromstring(
                book.read("xl/sharedStrings.xml")
            ).findall("m:si", NS)
        ]

        sheet = ET.fromstring(
            book.read("xl/worksheets/sheet1.xml")
        )

        def values(row):
            result = {}

            for cell in row.findall("m:c", NS):
                column = re.match(
                    r"[A-Z]+", cell.get("r", "")
                )

                require(
                    column is not None,
                    "Invalid spreadsheet cell reference.",
                )

                if column.group() not in {"B", "C", "D", "E", "F"}:
                    continue

                require(
                    cell.find("m:f", NS) is None,
                    "Formula found in a reference identity field.",
                )

                value = cell.findtext(
                    "m:v", default="", namespaces=NS
                )

                if cell.get("t") == "s" and value:
                    value = shared[int(value)]
                elif cell.get("t") == "inlineStr":
                    value = "".join(
                        text.text or ""
                        for text in cell.findall(".//m:t", NS)
                    )

                result[column.group()] = value.strip()

            return result

        rows = sheet.findall("m:sheetData/m:row", NS)
        require(bool(rows), "Empty administrative workbook.")

        require(
            values(rows[0]) == dict(
                B="GND_UID",
                C="Province_Code",
                D="Province_Name",
                E="District_Code",
                F="District_Name",
            ),
            "DCS workbook columns changed; review the source format.",
        )

        for row in rows[1:]:
            value = values(row)

            if not any(value.values()):
                continue

            province = value.get("C", "")
            local_code = value.get("E", "")

            require(
                province in PROVINCES
                and re.fullmatch(r"[1-9]", local_code) is not None,
                "Invalid province/district component code.",
            )

            code = province + local_code

            require(
                code in EXPECTED
                and lookup.get(
                    normalize_name(value.get("F", ""))
                ) == code,
                "Unexpected district/code mapping: "
                + code + " / " + value.get("F", ""),
            )

            require(
                normalize_name(value.get("D", ""))
                == normalize_name(PROVINCES[province]),
                "Province name/code mismatch.",
            )

            require(
                value.get("B", "").startswith(code),
                "GND identifier does not confirm its district prefix.",
            )

            entry = dict(
                district_id="DCS-" + code,
                dcs_district_code=code,
                source_province_code=province,
                source_district_code_in_province=local_code,
                district_name=EXPECTED[code],
                province_name=PROVINCES[province],
                source_district_name=value["F"],
                source_province_name=value["D"],
                data_origin="PUBLIC_REFERENCE",
                source_id=SOURCE_ID,
            )

            require(
                code not in registry or registry[code] == entry,
                "Conflicting district reference rows.",
            )

            registry[code] = entry
            checked += 1

    require(
        set(registry) == set(EXPECTED),
        "Expected the complete 25-district reference.",
    )

    return [
        registry[code] for code in sorted(registry)
    ], checked


def audit_owners(path):
    lookup, _ = make_aliases()
    rows, seen = [], set()

    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)

        require(
            {
                "owner_id",
                "data_origin",
                "home_district_answer",
            }.issubset(reader.fieldnames or ()),
            "Owner features lack required residence fields.",
        )

        for row in reader:
            owner = row["owner_id"]

            require(
                owner and owner not in seen,
                "Missing or duplicate owner ID.",
            )

            require(
                row["data_origin"] in {"SYNTHETIC", "OBSERVED"},
                "Unsupported owner-data origin.",
            )

            seen.add(owner)

            answer = row["home_district_answer"]
            key = normalize_name(answer)
            code = lookup.get(key)

            status = (
                "unmatched"
                if code is None
                else (
                    "matched_canonical"
                    if key == normalize_name(EXPECTED[code])
                    else "matched_alias"
                )
            )

            rows.append(dict(
                owner_id=owner,
                data_origin=row["data_origin"],
                home_district_answer=answer,
                normalized_home_district=key,
                district_id="DCS-" + code if code else "",
                district_name=EXPECTED[code] if code else "",
                match_status=status,
            ))

    require(bool(rows), "Owner features are empty.")
    return rows


def write_csv(path, rows):
    with path.open(
        "w", encoding="utf-8-sig", newline=""
    ) as handle:
        writer = csv.DictWriter(
            handle, fieldnames=tuple(rows[0])
        )
        writer.writeheader()
        writer.writerows(rows)


def build_registry(
    owner_features=None,
    source_xlsx=None,
    project_root=ROOT,
):
    root = Path(project_root).resolve()
    watched = {}

    def input_path(value):
        path = (root / value).resolve()

        require(
            path.is_relative_to(root) and path.is_file(),
            "Input must be a file inside the project.",
        )

        watched[path] = sha256(path)
        return path

    input_path(Path("docs/research_pipeline_specification.md"))
    input_path(
        Path("src/ev_demand/geography/build_district_registry.py")
    )

    owner_path = (
        input_path(owner_features)
        if owner_features is not None
        else None
    )

    retrieved_utc, final_url = None, None

    if source_xlsx is not None:
        source_path = input_path(source_xlsx)
        payload = source_path.read_bytes()
        acquisition = (
            "local_copy; upstream identity declared, "
            "not checked by a live download"
        )
    else:
        request = Request(
            SOURCE_URL,
            headers={"User-Agent": "EVChargingResearch/1.0"},
        )

        with urlopen(request, timeout=30) as response:
            final_url = response.geturl()
            parsed = urlparse(final_url)

            require(
                parsed.scheme == "https"
                and parsed.hostname in {
                    "statistics.gov.lk",
                    "www.statistics.gov.lk",
                },
                "Administrative download redirected outside "
                "the expected official host.",
            )

            payload = response.read(MAX_DOWNLOAD + 1)

        retrieved_utc = datetime.now(timezone.utc).isoformat()
        acquisition = "downloaded_from_official_endpoint"

    require(
        0 < len(payload) <= MAX_DOWNLOAD,
        "Administrative download is empty or too large.",
    )

    stamp = datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%S_%fZ"
    )

    raw = (
        root
        / "data/raw/public/district_registry_sources"
        / stamp
    )
    raw.mkdir(parents=True, exist_ok=False)

    snapshot = raw / "dcs_administrative_division_codes.xlsx"
    snapshot.write_bytes(payload)
    watched[snapshot] = sha256(snapshot)

    registry, checked = read_reference(snapshot)
    _, aliases = make_aliases()

    owners = (
        audit_owners(owner_path)
        if owner_path is not None
        else []
    )

    require(
        all(
            sha256(path) == old
            for path, old in watched.items()
        ),
        "Input changed during registry construction.",
    )

    output = (
        root
        / "data/processed/reference"
        / ("district_registry_" + stamp)
    )
    output.mkdir(parents=True, exist_ok=False)

    write_csv(output / "district_registry.csv", registry)
    write_csv(output / "district_aliases.csv", aliases)

    if owners:
        write_csv(
            output / "owner_home_district_matches.csv",
            owners,
        )

    counts = Counter(
        row["match_status"] for row in owners
    )

    report = dict(
        schema_version="district_registry_v1",
        status="complete",
        registry_origin="PUBLIC_REFERENCE",
        districts=len(registry),
        provinces=len({
            row["source_province_code"]
            for row in registry
        }),
        source_records_checked=checked,
        alias_keys=len(aliases),
        coding_scheme=(
            "Project IDs DCS-<province code>"
            "<district-within-province code>; not ISO codes"
        ),
        source=dict(
            source_id=SOURCE_ID,
            page_url=SOURCE_PAGE,
            download_url=SOURCE_URL,
            final_url=final_url,
            acquisition=acquisition,
            retrieved_utc=retrieved_utc,
            source_reference_date=None,
            reference_date_note=(
                "Workbook reference date not established; "
                "retrieval date is recorded separately."
            ),
            workbook_sheet="GNDList",
            snapshot=snapshot.relative_to(root).as_posix(),
            sha256=watched[snapshot],
        ),
        owner_audit=dict(
            performed=owner_path is not None,
            owners=len(owners),
            matched=(
                counts["matched_canonical"]
                + counts["matched_alias"]
            ),
            unmatched=counts["unmatched"],
            match_status_counts=dict(counts),
            input_origins=dict(Counter(
                row["data_origin"] for row in owners
            )),
            unmatched_answers=dict(Counter(
                row["home_district_answer"]
                for row in owners
                if row["match_status"] == "unmatched"
            )),
        ),
        checks=dict(
            district_code_name_pairs_match=True,
            province_pairs_match=True,
            gnd_prefixes_confirm_codes=True,
            alias_keys_unambiguous=True,
            input_hashes_unchanged=True,
        ),
        input_sha256={
            path.relative_to(root).as_posix(): old
            for path, old in watched.items()
        },
        output_sha256={
            path.name: sha256(path)
            for path in output.glob("*.csv")
        },
        geometry_included=False,
        fleet_counts_included=False,
        district_demand_ready=False,
        interpretation=[
            "Only administrative identity fields are "
            "extracted from the official workbook.",
            "Spelling variants and name normalization are "
            "declared project matching rules; no fuzzy matching.",
            "Numeric/ISO codes and city names are not automatically "
            "interpreted as district-name answers.",
            "Owner matches identify residence, not charging "
            "locations or national fleet shares.",
            "Unmatched answers are retained; no owner readiness "
            "or simulator parameters are changed.",
        ],
    )

    require(
        all(
            sha256(path) == old
            for path, old in watched.items()
        ),
        "Input changed while exporting the registry.",
    )

    (output / "district_registry_report.json").write_text(
        json.dumps(report, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )

    return output, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument(
        "--owner-features",
        help="Optional owners_features.csv to audit residence-name joins",
    )
    parser.add_argument(
        "--source-xlsx",
        help=(
            "Optional existing DCS workbook inside the project; "
            "otherwise download it"
        ),
    )

    args = parser.parse_args()

    output, report = build_registry(
        args.owner_features,
        args.source_xlsx,
    )

    print("District reference registry complete.")

    for key in (
        "districts",
        "provinces",
        "source_records_checked",
        "alias_keys",
    ):
        print(f"{key}: {report[key]}")

    if report["owner_audit"]["performed"]:
        for key in ("owners", "matched", "unmatched"):
            print(
                f"owner_residence_{key}: "
                f"{report['owner_audit'][key]}"
            )

    print(
        "Official district/code, province, alias "
        "and input-integrity checks passed."
    )
    print(f"Source snapshot: {report['source']['snapshot']}")
    print(f"Output: {output.relative_to(ROOT)}")
    print(
        "Public administrative reference and "
        "optional owner-residence audit only."
    )


if __name__ == "__main__":
    try:
        main()
    except (
        ValueError,
        OSError,
        KeyError,
        TypeError,
        IndexError,
        BadZipFile,
        ET.ParseError,
    ) as error:
        print(
            f"District registry failed: {error}",
            file=sys.stderr,
        )
        raise SystemExit(1)