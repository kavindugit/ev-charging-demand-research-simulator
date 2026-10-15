"""Preserve synthetic interview evidence; do not infer simulator parameters."""
from __future__ import annotations

import argparse
from bisect import bisect_right
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[3]

CASE = re.compile(
    r"^## (S\d{3})[ \t]+[—–-][ \t]+SYNTHETIC[ \t]*\r?$",
    re.M,
)
QUESTION = re.compile(
    r"^\*\*Q(\d+)\. Interviewer:\*\*[ \t]*(.*)\r?$",
    re.M,
)
SPEAKER = re.compile(
    r"^\*\*(S\d{3}) \(fictional\):\*\*[ \t]*",
    re.M,
)

THEMES = {
    1: "mock_consent",
    2: "usual_routine",
    3: "context_choice",
    4: "charging_trigger",
    5: "stop_target",
    6: "home_start_delay",
    7: "tou_influence",
    8: "public_charger_choice",
    9: "unavailable_charger",
    10: "long_trip_planning",
    11: "destination_charging",
    12: "day_type_variation",
    13: "hypothetical_price_response",
    14: "disconnect_vs_active_charging",
    15: "uncertainty",
    16: "unplanned_session",
    17: "coordination",
    18: "learning",
    19: "difficulties",
    20: "additional_influences",
}

REVIEW_FIELDS = [
    "evidence_id",
    "case_id",
    "question_number",
    "question_theme",
    "question_text",
    "answer_text",
    "data_origin",
    "source_file",
    "source_sha256",
    "answer_line_start",
    "answer_line_end",
    "reviewed_codes",
    "evidence_type",
    "relevant_contexts",
    "candidate_simulator_rule",
    "explicit_numeric_value",
    "numeric_unit",
    "review_status",
    "review_notes",
]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_jsonl(path, rows):
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def parse_transcripts(text, filename, source_hash):
    headings = list(CASE.finditer(text))

    if not headings:
        raise ValueError(
            "No synthetic case headings found; check the source format."
        )

    preamble = text[:headings[0].start()]

    if "SYNTHETIC" not in preamble or "invented" not in preamble:
        raise ValueError("The synthetic source declaration is missing.")

    line_starts = [0] + [
        match.end() for match in re.finditer("\n", text)
    ]

    def line_at(offset):
        return bisect_right(line_starts, offset)

    cases = []
    answers = []
    seen = set()

    for index, heading in enumerate(headings):
        case_id = heading.group(1)

        if case_id in seen:
            raise ValueError(f"Duplicate interview case: {case_id}.")

        seen.add(case_id)

        end = (
            headings[index + 1].start()
            if index + 1 < len(headings)
            else len(text)
        )

        body = text[heading.end():end]
        questions = list(QUESTION.finditer(body))

        question_numbers = [
            int(question.group(1)) for question in questions
        ]

        if question_numbers != list(range(1, 21)):
            raise ValueError(
                f"{case_id}: expected Q1–Q20 exactly once, in order."
            )

        if len(list(SPEAKER.finditer(body))) != 20:
            raise ValueError(
                f"{case_id}: expected exactly 20 answer headers."
            )

        metadata = body[:questions[0].start()]

        situation = re.findall(
            r"^\*\*Fictional situation:\*\*[ \t]*(.*)$",
            metadata,
            re.M,
        )
        contexts = re.findall(
            r"^\*\*Charging contexts discussed:\*\*[ \t]*(.*)$",
            metadata,
            re.M,
        )

        if len(situation) != 1 or len(contexts) != 1:
            raise ValueError(
                f"{case_id}: missing or duplicate case metadata."
            )

        provenance = {
            "data_origin": "SYNTHETIC",
            "source_file": filename,
            "source_sha256": source_hash,
        }

        cases.append({
            **provenance,
            "case_id": case_id,
            "fictional_situation": situation[0].strip(),
            "contexts_discussed_text": contexts[0].strip(),
            "case_text": text[heading.start():end],
            "case_char_start": heading.start(),
            "case_char_end_exclusive": end,
            "case_line_start": line_at(heading.start()),
            "case_line_end": line_at(end - 1),
            "question_count": 20,
            "survey_owner_link": None,
        })

        for number, question in enumerate(questions, start=1):
            stop = (
                questions[number].start()
                if number < 20
                else len(body)
            )

            section = body[question.end():stop]
            speakers = list(SPEAKER.finditer(section))

            if (
                len(speakers) != 1
                or speakers[0].group(1) != case_id
            ):
                raise ValueError(
                    f"{case_id} Q{number}: missing or mismatched speaker."
                )

            speaker = speakers[0]

            if section[:speaker.start()].strip():
                raise ValueError(
                    f"{case_id} Q{number}: unexpected text before the answer."
                )

            start = (
                heading.end()
                + question.end()
                + speaker.end()
            )
            answer_end = heading.end() + stop

            # Remove only the final case separator and trailing whitespace.
            if number == 20:
                separator = re.search(
                    r"\r?\n[ \t]*---[ \t]*(?:\r?\n|[ \t])*\Z",
                    text[start:answer_end],
                )
                if separator:
                    answer_end = start + separator.start()

            while (
                answer_end > start
                and text[answer_end - 1].isspace()
            ):
                answer_end -= 1

            if answer_end <= start:
                raise ValueError(
                    f"{case_id} Q{number}: empty answer."
                )

            answers.append({
                **provenance,
                "evidence_id": f"{case_id}_Q{number:02d}",
                "case_id": case_id,
                "question_number": number,
                "question_theme": THEMES[number],
                "question_text": question.group(2).rstrip("\r"),
                "answer_text": text[start:answer_end],
                "answer_char_start": start,
                "answer_char_end_exclusive": answer_end,
                "answer_line_start": line_at(start),
                "answer_line_end": line_at(answer_end - 1),
                "review_status": "not_reviewed",
            })

    return preamble, cases, answers


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--expected-cases",
        type=int,
        default=40,
    )
    args = parser.parse_args()

    if args.expected_cases < 1:
        raise ValueError("Expected case count must be positive.")

    folder = ROOT / "data" / "raw" / "synthetic"
    inputs = sorted(folder.glob("*.md"))

    if len(inputs) != 1:
        raise ValueError(
            f"Expected one interview .md file in {folder}; "
            f"found {len(inputs)}."
        )

    source = inputs[0]
    original_bytes = source.read_bytes()
    source_hash = hashlib.sha256(original_bytes).hexdigest()
    text = original_bytes.decode("utf-8-sig")

    preamble, cases, answers = parse_transcripts(
        text,
        source.name,
        source_hash,
    )

    if len(cases) != args.expected_cases:
        raise ValueError(
            f"Expected {args.expected_cases} cases; found {len(cases)}."
        )

    for record in answers:
        assert record["answer_text"] == text[
            record["answer_char_start"]:
            record["answer_char_end_exclusive"]
        ]

    if digest(source) != source_hash:
        raise ValueError(
            "Raw interview file changed during preparation; rerun."
        )

    stamp = datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%S_%fZ"
    )
    output = (
        ROOT
        / "data"
        / "interim"
        / "synthetic"
        / f"interviews_{stamp}"
    )
    output.mkdir(parents=True, exist_ok=False)

    write_jsonl(
        output / "interview_cases.jsonl",
        cases,
    )
    write_jsonl(
        output / "interview_answers.jsonl",
        answers,
    )

    with (
        output / "interview_coding_review.csv"
    ).open(
        "w",
        encoding="utf-8-sig",
        newline="",
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=REVIEW_FIELDS,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(answers)

    report = {
        "schema_version": "interview_evidence_v1",
        "data_origin": "SYNTHETIC",
        "source_file": source.name,
        "source_sha256": source_hash,
        "source_declaration": preamble,
        "cases": len(cases),
        "answers": len(answers),
        "questions_per_case": 20,
        "source_offsets": (
            "Zero-based character offsets in UTF-8 decoded text; "
            "end exclusive; lines one-based."
        ),
        "answer_preservation": (
            "Exact source passages, excluding speaker headers "
            "and trailing separators/whitespace."
        ),
        "reviewed_answers": 0,
        "simulator_ready": False,
        "parameters_fitted": False,
        "survey_owner_links_created": 0,
        "synthetic_consent_is_real_consent": False,
        "review_guidance": [
            (
                "Keep reported practice, hypothetical responses "
                "and exceptional periods distinct."
            ),
            (
                "Q13 uses a hypothetical price prompt; inspect "
                "the full answer before coding."
            ),
            (
                "Record explicit numerical statements only; "
                "do not translate qualitative words into probabilities."
            ),
            (
                "Never join interview IDs to survey IDs "
                "by row order or matching number."
            ),
            (
                "Do not treat these fictional cases as empirical "
                "frequencies or real participant quotations."
            ),
            (
                "Case contexts describe discussion topics; "
                "they are not session allocation weights."
            ),
        ],
    }

    report["output_sha256"] = {
        path.name: digest(path)
        for path in sorted(output.iterdir())
        if path.is_file()
    }

    (
        output / "interview_preparation_report.json"
    ).write_text(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )

    print("Interview preparation complete.")
    print(f"cases: {len(cases)}")
    print(f"answers: {len(answers)}")
    print("reviewed_answers: 0")
    print(f"Output: {output.relative_to(ROOT)}")
    print(
        "Full passages preserved; raw file unchanged; "
        "no simulator parameters inferred."
    )


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, UnicodeError) as exc:
        print(
            f"Interview preparation failed: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1)