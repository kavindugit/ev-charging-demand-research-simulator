"""Archive three pinned official DMT PDFs; no fleet estimates or simulations."""
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
INDEX = (
    "https://dmt.gov.lk/index.php?"
    "Itemid=132&id=16&lang=en&option=com_content&view=article"
)
BASE_URL = "https://dmt.gov.lk/images/2026/"

SOURCES = {
    "vehicle_population_2014_2025": dict(
        filename="Vehicle_Population_2014-2025.pdf",
        title="Total Vehicle Population 2014-2025",
        reference_period="2014-2025",
        bytes=72317,
        pages=1,
        sha256=(
            "bed6a4b57ff05b8bb299d45e5fd5b260d"
            "f54b773c672fd19d5e7b8d31714e757"
        ),
        required_text=[
            "TOTAL VEHICLE POPULATION",
            "NEW REGISTRATION",
            "2014",
            "2025",
        ],
        scope=(
            "National all-fuel vehicle population and annual "
            "new registrations, by class"
        ),
    ),
    "monthly_registrations_2025": dict(
        filename="Monthly_New_registration_of_Vehicles_2025.pdf",
        title="Monthly New Registration of Motor Vehicles 2025",
        reference_period="2025",
        bytes=69967,
        pages=1,
        sha256=(
            "c17a23465c1499269827391fbae58fd5"
            "e5500797dff537a746195d42a3f0370d"
        ),
        required_text=[
            "MONTHLY NEW REGISTRATION",
            "2025",
            "JAN",
            "DEC",
        ],
        scope=(
            "National monthly new registration flows, by vehicle "
            "category; not electric-only"
        ),
    ),
    "fuel_registrations_2025": dict(
        filename=(
            "New_Registrations_of_Motor_Vehicles_"
            "by_Type_of_Fuel_-_2025.pdf"
        ),
        title="New Registrations of Motor Vehicles by Type of Fuel 2025",
        reference_period="2025",
        bytes=39015,
        pages=1,
        sha256=(
            "06529f2e5acf96fceeb0a4449a70b889"
            "65e0a12d1a01ad673022be026d9e212d"
        ),
        required_text=[
            "NEW REGISTRATIONS",
            "TYPE OF FUEL",
            "2025",
            "January",
            "December",
        ],
        scope=(
            "National monthly new registration flows, by class "
            "and reported fuel category"
        ),
    ),
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
        and parsed.hostname in {"dmt.gov.lk", "www.dmt.gov.lk"}
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
    safe_url(url)
    partial = destination.with_suffix(destination.suffix + ".part")

    for attempt in range(1, 4):
        try:
            request = Request(
                url,
                headers={"User-Agent": "EVChargingDemandResearch/1.0"},
            )
            with (
                urlopen(request, timeout=45) as response,
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
                    f"Download failed; cache retained at {partial}: {exc}"
                ) from exc

            print(f"Download retry {attempt}/2: {exc}", flush=True)
            time.sleep(2 * attempt)


def inspect_pdf(path, source_id, specification):
    require(
        path.stat().st_size == specification["bytes"],
        "Unexpected PDF size: " + source_id,
    )
    require(
        digest(path) == specification["sha256"],
        "PDF content changed; retain the source for review: " + source_id,
    )

    with path.open("rb") as handle:
        require(handle.read(5) == b"%PDF-", "The source is not a PDF.")

    reader = PdfReader(path)
    require(not reader.is_encrypted, "Encrypted source PDF.")
    require(
        len(reader.pages) == specification["pages"],
        "Unexpected PDF page count.",
    )

    rows, texts = [], []
    for page_number, page in enumerate(reader.pages, 1):
        text = page.extract_text() or ""
        require(
            bool(text.strip()),
            "Source page has no extractable text.",
        )
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

        rows.append(
            dict(
                source_id=source_id,
                pdf_page=page_number,
                text_characters=len(text),
                extracted_text_sha256=text_hash,
                data_origin="PUBLIC_REFERENCE",
            )
        )
        texts.append(
            dict(
                source_id=source_id,
                pdf_page=page_number,
                text=text,
                extracted_text_sha256=text_hash,
            )
        )

    normalized = re.sub(
        r"\s+", " ", " ".join(row["text"] for row in texts)
    ).upper()

    require(
        all(
            term.upper() in normalized
            for term in specification["required_text"]
        ),
        "Source title or reference-period text check failed: " + source_id,
    )
    return rows, texts


def write_csv(path, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    raw = ROOT / "data/raw/public/dmt_sources" / stamp
    output = ROOT / "data/processed/reference" / ("dmt_sources_" + stamp)
    raw.mkdir(parents=True, exist_ok=False)

    print("DMT source snapshot: " + relative(raw), flush=True)

    script = Path(__file__).resolve()
    watched = {script: digest(script)}
    document = ROOT / "docs/research_pipeline_specification.md"
    if document.exists():
        watched[document] = digest(document)

    index_path = raw / "dmt_statistics_index.html"
    index_http = fetch(INDEX, index_path, 4 * 1024 * 1024)

    links = ReportLinks()
    links.feed(index_path.read_bytes().decode("utf-8-sig"))
    require(
        all(
            BASE_URL + spec["filename"] in links.links
            for spec in SOURCES.values()
        ),
        "A pinned PDF is not linked by the saved official index; "
        "review that snapshot.",
    )
    watched[index_path] = digest(index_path)

    manifest, page_rows, page_texts, details = [], [], [], []

    for source_id, spec in SOURCES.items():
        print(
            "Downloading and inspecting " + source_id + "...",
            flush=True,
        )
        pdf = raw / (source_id + ".pdf")
        source_url = BASE_URL + spec["filename"]
        http = fetch(source_url, pdf, 4 * 1024 * 1024)

        rows, texts = inspect_pdf(pdf, source_id, spec)
        page_rows.extend(rows)
        page_texts.extend(texts)
        watched[pdf] = spec["sha256"]

        manifest.append(
            dict(
                source_id=source_id,
                title=spec["title"],
                publisher="Department of Motor Traffic, Sri Lanka",
                reference_period=spec["reference_period"],
                geography="Sri Lanka national",
                measurement_scope=spec["scope"],
                source_url=source_url,
                official_index_url=INDEX,
                linked_from_saved_index=True,
                raw_file=relative(pdf),
                bytes=pdf.stat().st_size,
                sha256=spec["sha256"],
                pdf_pages=len(rows),
                retrieved_utc=http["retrieved_utc"],
                data_origin="PUBLIC_REFERENCE",
            )
        )
        details.append(
            dict(
                source_id=source_id,
                http=http,
                sha256=spec["sha256"],
            )
        )
        print(
            f"{source_id}: pinned SHA256 matched; "
            f"{len(rows)} page read.",
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
    write_csv(output / "dmt_source_manifest.csv", manifest)
    write_csv(output / "dmt_document_page_inventory.csv", page_rows)

    with (
        output / "dmt_extracted_page_text.jsonl"
    ).open("w", encoding="utf-8") as handle:
        for row in page_texts:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    report = dict(
        schema="dmt_source_snapshot_v1",
        status="source_snapshots_inspected",
        created_utc=datetime.now(timezone.utc).isoformat(),
        data_origin="PUBLIC_REFERENCE",
        source_documents=len(manifest),
        pdf_pages_read=len(page_rows),
        raw_source_folder=relative(raw),
        official_index_http=index_http,
        sources=details,
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
            for path in sorted(output.iterdir())
        },
        checks=dict(
            official_index_links=True,
            pinned_pdf_hashes=True,
            expected_page_counts=True,
            all_pages_text_inspected=True,
            title_and_period_text=True,
            inputs_unchanged=True,
        ),
        numerical_tables_extracted=False,
        numerical_consistency_review_complete=False,
        fuel_category_definitions_review_complete=False,
        national_active_electric_stock_established=False,
        district_electric_stock_established=False,
        adoption_scenarios_created=False,
        district_demand_ready=False,
        new_demand_simulations=0,
        interpretation_limits=[
            "Population tables and registration-flow tables "
            "measure different quantities.",
            "The all-fuel population table does not establish "
            "electric vehicle stock.",
            "New registrations during 2025 are not the active "
            "electric fleet at year end.",
            "Reported electric and mixed-fuel categories must be "
            "reviewed separately; mixed fuel does not establish "
            "plug-in capability.",
            "These three national documents do not establish "
            "district EV counts, model or battery composition, "
            "or retirement rates.",
            "PDF text reading order is not verified table-column "
            "order; no numeric cells are parsed in this step.",
            "Document names and PDF metadata are not evidence "
            "of a publication date.",
            "SHA256 pins identify the research snapshots; "
            "they are not publisher-issued checksums.",
            "No open-data licence is asserted; "
            "the publisher retains its rights.",
        ],
    )

    (output / "dmt_source_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print("DMT public source preparation complete.")
    print("source_documents:", len(manifest))
    print("pdf_pages_read:", len(page_rows))
    print(
        "Official-link, pinned-source, PDF-reading "
        "and input-integrity checks passed."
    )
    print("Raw sources:", relative(raw))
    print("Output:", relative(output))
    print(
        "Source preparation only; fleet estimates, district "
        "allocation and adoption scenarios remain pending."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("DMT source preparation failed:", exc, file=sys.stderr)
        raise SystemExit(1)
        