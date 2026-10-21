"""Archive and inspect two pinned official SLTDA reports; no numeric fitting."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
from importlib.metadata import version
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from pypdf import PdfReader


ROOT = Path(__file__).resolve().parents[3]

INDEX = "https://www.sltda.gov.lk/en/annual-statistical-report"

SOURCES = {
    2024: dict(
        url=(
            "https://www.sltda.gov.lk/storage/common_media/"
            "Year_In_Review_2024_Final_2024_Jan-Dec_updated_20_04_2026.pdf"
        ),
        bytes=4235505,
        pages=44,
        sha256=(
            "65fde930d94619d2ffdf5df02a64bb210"
            "2a5dd43676fb0c73a77e03f6026c0bf"
        ),
    ),
    2025: dict(
        url=(
            "https://www.sltda.gov.lk/storage/common_media/"
            "Year_in_Review_2025_Final_updated_Report_2026_04_02-1.pdf"
        ),
        bytes=4377913,
        pages=70,
        sha256=(
            "4a0f77c2824a150b353d7f070defa905"
            "e09efb1f277d98358ba8eced3cce3727"
        ),
    ),
}

TERMS = {
    "room": r"\brooms?\b",
    "bed": r"\bbeds?\b",
    "occupancy": r"\boccupancy\b",
    "district": r"\bdistricts?\b",
    "establishment": r"\bestablishments?\b",
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def relative(path):
    return path.resolve().relative_to(ROOT.resolve()).as_posix()


def safe_url(url):
    parsed = urlparse(url)

    require(
        parsed.scheme == "https"
        and parsed.hostname in {"sltda.gov.lk", "www.sltda.gov.lk"}
        and parsed.username is None
        and parsed.password is None
        and parsed.port in {None, 443},
        "Unexpected source URL: " + url,
    )

    return url


class ReportLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = set()

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.add(urljoin(INDEX, href))


def fetch(url, destination, limit):
    """Retry small downloads; publish a file only after a full read."""
    safe_url(url)
    partial = destination.with_suffix(destination.suffix + ".part")

    for attempt in range(1, 4):
        try:
            request = Request(
                url,
                headers={"User-Agent": "EVChargingDemandResearch/1.0"},
            )

            with (
                urlopen(request, timeout=60) as response,
                partial.open("wb") as handle,
            ):
                final_url = safe_url(response.geturl())
                require(response.status == 200, "Unexpected HTTP status.")

                count = 0

                while block := response.read(65536):
                    count += len(block)

                    require(
                        count <= limit,
                        "Source exceeds the download-size limit.",
                    )

                    handle.write(block)

                declared = response.headers.get("Content-Length")

                require(
                    count > 0
                    and (declared is None or count == int(declared)),
                    "Empty or incomplete HTTP response.",
                )

                metadata = dict(
                    requested_url=url,
                    final_url=final_url,
                    bytes=count,
                    retrieved_utc=datetime.now(timezone.utc).isoformat(),
                    content_type=response.headers.get("Content-Type"),
                    etag=response.headers.get("ETag"),
                    last_modified=response.headers.get("Last-Modified"),
                )

            partial.replace(destination)
            return metadata

        except (OSError, ValueError) as exc:
            if attempt == 3:
                raise RuntimeError(
                    f"Download failed; partial file retained: "
                    f"{partial}: {exc}"
                ) from exc

            print(f"Download retry {attempt}/2: {exc}", flush=True)
            time.sleep(2 * attempt)


def inspect_pdf(path, year, specification):
    require(
        path.stat().st_size == specification["bytes"],
        f"Unexpected PDF size for {year}.",
    )

    require(
        digest(path) == specification["sha256"],
        f"PDF content changed for {year}; retain it for source review "
        "rather than changing the pin.",
    )

    with path.open("rb") as handle:
        require(handle.read(5) == b"%PDF-", "The source is not a PDF.")

    reader = PdfReader(path)

    require(not reader.is_encrypted, "Encrypted source PDF.")

    require(
        len(reader.pages) == specification["pages"],
        "Unexpected PDF page count.",
    )

    rows = []
    first_pages = []

    for page_number, page in enumerate(reader.pages, 1):
        text = page.extract_text() or ""

        if page_number <= 2:
            first_pages.append(text)

        row = dict(
            report_year=year,
            pdf_page=page_number,
            text_characters=len(text),
            extracted_text_sha256=hashlib.sha256(
                text.encode("utf-8")
            ).hexdigest(),
        )

        row.update({
            f"has_{name}_term": bool(re.search(pattern, text, re.I))
            for name, pattern in TERMS.items()
        })

        rows.append(row)

    require(
        str(year) in " ".join(first_pages),
        "Report-year text check failed.",
    )

    require(
        any(row["text_characters"] > 0 for row in rows),
        "No extractable PDF text.",
    )

    return rows


def write_csv(path, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument(
        "--years",
        nargs="+",
        type=int,
        choices=sorted(SOURCES),
        default=[2024, 2025],
    )

    args = parser.parse_args()
    years = sorted(set(args.years))

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")

    raw = ROOT / "data/raw/public/sltda_sources" / stamp
    output = (
        ROOT / "data/processed/reference" / ("sltda_sources_" + stamp)
    )

    raw.mkdir(parents=True, exist_ok=False)

    print("SLTDA source snapshot: " + relative(raw), flush=True)

    script = Path(__file__).resolve()
    watched = {script: digest(script)}

    for name in (
        "research_pipeline_specification.md",
        "spatial_source_review_provisional_decisions.md",
    ):
        document = ROOT / "docs" / name

        if document.exists():
            watched[document] = digest(document)

    index_path = raw / "annual_statistical_report_index.html"

    index_http = fetch(
        INDEX,
        index_path,
        4 * 1024 * 1024,
    )

    links = ReportLinks()
    links.feed(index_path.read_bytes().decode("utf-8-sig"))

    require(
        all(SOURCES[year]["url"] in links.links for year in years),
        "A pinned PDF is no longer linked by the official index; "
        "inspect the saved index.",
    )

    watched[index_path] = digest(index_path)

    manifest = []
    page_rows = []
    source_details = []

    for year in years:
        spec = SOURCES[year]
        pdf = raw / f"sltda_year_in_review_{year}.pdf"

        print(
            f"Downloading and reading SLTDA {year} report...",
            flush=True,
        )

        http = fetch(spec["url"], pdf, 16 * 1024 * 1024)

        rows = inspect_pdf(pdf, year, spec)
        page_rows.extend(rows)
        watched[pdf] = spec["sha256"]

        manifest.append(dict(
            report_year=year,
            title=f"SLTDA Year in Review {year}",
            publisher="Sri Lanka Tourism Development Authority",
            source_url=spec["url"],
            official_index_url=INDEX,
            linked_from_saved_index=True,
            raw_file=relative(pdf),
            bytes=pdf.stat().st_size,
            sha256=spec["sha256"],
            pdf_pages=len(rows),
            retrieved_utc=http["retrieved_utc"],
            data_origin="PUBLIC_REFERENCE",
        ))

        source_details.append(dict(
            report_year=year,
            http=http,
            source_sha256=spec["sha256"],
            expected_pdf_pages=spec["pages"],
            keyword_pages={
                name: [
                    row["pdf_page"]
                    for row in rows
                    if row[f"has_{name}_term"]
                ]
                for name in TERMS
            },
        ))

        print(
            f"SLTDA {year}: SHA256 matched; {len(rows)} pages read.",
            flush=True,
        )

    require(
        all(
            digest(path) == expected
            for path, expected in watched.items()
        ),
        "A source or implementation input changed during acquisition.",
    )

    output.mkdir(parents=True, exist_ok=False)

    write_csv(output / "sltda_source_manifest.csv", manifest)
    write_csv(output / "sltda_document_page_inventory.csv", page_rows)

    report = dict(
        schema="sltda_source_snapshot_v1",
        status="source_snapshots_inspected",
        created_utc=datetime.now(timezone.utc).isoformat(),
        data_origin="PUBLIC_REFERENCE",
        report_years=years,
        source_documents=len(manifest),
        pdf_pages_read=len(page_rows),
        raw_source_folder=relative(raw),
        official_index_http=index_http,
        sources=source_details,
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
        checks=dict(
            official_index_links=True,
            pinned_pdf_hashes=True,
            expected_page_counts=True,
            all_pages_text_inspected=True,
            inputs_unchanged=True,
        ),
        numerical_tables_extracted=False,
        numerical_consistency_review_complete=False,
        bed_capacity_established=False,
        occupancy_denominator_review_complete=False,
        typology_weights_created=False,
        district_demand_ready=False,
        interpretation_limits=[
            "Keyword pages are navigation aids; text extraction does "
            "not establish table correctness or absence.",
            "Report years, capacity units and occupancy reporting "
            "populations require separate review.",
            "Rooms are not beds; capacity and occupancy are not "
            "vehicle arrivals or charging demand.",
            "No open-data licence is asserted for the SLTDA reports; "
            "publisher rights are retained.",
        ],
    )

    (output / "sltda_source_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print("SLTDA public report source preparation complete.")
    print("report_years:", years)
    print("source_documents:", len(manifest))
    print("pdf_pages_read:", len(page_rows))

    print(
        "Official-link, pinned-source, PDF-reading and "
        "input-integrity checks passed."
    )

    print("Raw sources:", relative(raw))
    print("Output:", relative(output))

    print(
        "Source preparation only; numerical tables, occupancy "
        "definitions and demand scaling remain unverified."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            "SLTDA source preparation failed:",
            exc,
            file=sys.stderr,
        )
        raise SystemExit(1)