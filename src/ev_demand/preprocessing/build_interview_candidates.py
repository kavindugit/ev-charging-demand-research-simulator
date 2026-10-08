"""Find review candidates in full answers; never accept codes automatically."""
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

ROOT = Path(__file__).resolve().parents[3]

# These labels identify discussion topics, not confirmed behaviours.
# Negated, hypothetical and exceptional statements require review.
CODEBOOK = {
    "DELAY_DISCUSSION": {
        "pattern": r"\b(delay\w*|schedul\w*|timer|later|immediately)\b",
        "rule_family": "session_start_timing",
        "review_question": (
            "Is charging delayed, immediate, conditional, or unavailable?"
        ),
    },
    "PRICE_DISCUSSION": {
        "pattern": r"\b(tou|tariff\w*|cheap\w*|price\w*|discount\w*|cost\w*)\b",
        "rule_family": "price_timing_constraints",
        "review_question": (
            "Is this actual tariff behaviour or a hypothetical response?"
        ),
    },
    "TRIP_DISCUSSION": {
        "pattern": r"\b(next|tomorrow|urgent|unexpected|reserve|journey|trip\w*)\b",
        "rule_family": "next_trip_requirement",
        "review_question": (
            "Does an upcoming trip affect timing, energy, or the target?"
        ),
    },
    "TARGET_DISCUSSION": {
        "pattern": r"\b(target\w*|top-up|fill\w*|full|higher|enough|battery|percentage)\b",
        "rule_family": "session_energy_and_stop",
        "review_question": (
            "What trigger or stop condition is actually described?"
        ),
    },
    "CONTEXT_DISCUSSION": {
        "pattern": r"\b(home|public|workplace|office|hotel|restaurant|destination|route)\b",
        "rule_family": "charging_context_choice",
        "review_question": (
            "Which context is used, avoided, or only considered?"
        ),
    },
    "AVAILABILITY_DISCUSSION": {
        "pattern": r"\b(queue\w*|occupied|unavailable|reliable|reliability|backup|alternative\w*|wait\w*)\b",
        "rule_family": "charger_unavailability_response",
        "review_question": (
            "Does the owner wait, switch location, postpone, or leave?"
        ),
    },
    "SPEED_DISCUSSION": {
        "pattern": r"\b(fast\w*|slow\w*|speed|ac|dc)\b",
        "rule_family": "charger_choice",
        "review_question": (
            "Is charger preference conditional on time or activity?"
        ),
    },
    "DAY_TYPE_DISCUSSION": {
        "pattern": r"\b(weekday\w*|weekend\w*|workday\w*|shift\w*)\b",
        "rule_family": "day_type_and_schedule",
        "review_question": (
            "Is the routine different by day type or work schedule?"
        ),
    },
    "PARKING_DISCUSSION": {
        "pattern": r"\b(connect\w*|disconnect\w*|unplug\w*|park\w*|drawing|finished|finishes|completion)\b",
        "rule_family": "active_charging_vs_connected_time",
        "review_question": (
            "Does connected or parked time exceed active charging time?"
        ),
    },
    "EXCEPTION_DISCUSSION": {
        "pattern": r"\b(temporary|temporarily|exception\w*|outage\w*|failure|failed|repair\w*|broken)\b",
        "rule_family": "exceptional_period",
        "review_question": (
            "Is this temporary behaviour rather than the usual routine?"
        ),
    },
    "COORDINATION_DISCUSSION": {
        "pattern": r"\b(shar\w*|coordinat\w*|book\w*|spouse|family|appliance\w*)\b",
        "rule_family": "shared_access_constraints",
        "review_question": (
            "What access or schedule constraint is actually described?"
        ),
    },
}

FIELDS = [
    "evidence_id",
    "case_id",
    "question_number",
    "question_theme",
    "question_text",
    "answer_text",
    "fictional_situation",
    "contexts_discussed_text",
    "data_origin",
    "source_file",
    "source_sha256",
    "answer_line_start",
    "answer_line_end",
    "candidate_codes",
    "candidate_rule_families",
    "matched_passages_json",
    "prompt_note",
    "review_status",
    "accepted_codes",
    "evidence_type",
    "reviewed_contexts",
    "reviewed_rule",
    "review_notes",
]


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_jsonl(path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def candidates(answer, question_number):
    if question_number == 1:
        return [], [], []

    codes = []
    families = []
    passages = []

    for code, definition in CODEBOOK.items():
        matches = list(
            re.finditer(definition["pattern"], answer, re.I)
        )

        if not matches:
            continue

        codes.append(code)
        families.append(definition["rule_family"])

        # Keep every match, its sentence and exact answer offsets.
        for match in matches:
            start = max(
                answer.rfind(". ", 0, match.start()),
                answer.rfind("\n", 0, match.start()),
            )

            if start < 0:
                start = 0
            else:
                start += (
                    2 if answer[start:start + 2] == ". " else 1
                )

            ends = [
                position
                for position in (
                    answer.find(". ", match.end()),
                    answer.find("\n", match.end()),
                )
                if position >= 0
            ]

            end = min(ends) + 1 if ends else len(answer)

            passages.append({
                "candidate_code": code,
                "matched_text": match.group(),
                "match_start": match.start(),
                "match_end_exclusive": match.end(),
                "passage": answer[start:end],
                "passage_start": start,
                "passage_end_exclusive": end,
            })

    return codes, families, passages


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    args = parser.parse_args()

    folder = (ROOT / args.input).resolve()
    allowed = (ROOT / "data/interim/synthetic").resolve()

    if not folder.is_relative_to(allowed):
        raise ValueError(
            "Input must be under data/interim/synthetic."
        )

    files = {
        name: folder / name
        for name in (
            "interview_cases.jsonl",
            "interview_answers.jsonl",
            "interview_preparation_report.json",
        )
    }

    hashes = {
        name: sha256(path)
        for name, path in files.items()
    }

    report = json.loads(
        files["interview_preparation_report.json"].read_text(
            encoding="utf-8"
        )
    )
    cases = read_jsonl(files["interview_cases.jsonl"])
    answers = read_jsonl(files["interview_answers.jsonl"])

    if report.get("data_origin") != "SYNTHETIC":
        raise ValueError(
            "This step accepts synthetic practice data only."
        )

    if (
        len(cases) != report["cases"]
        or len(answers) != report["answers"]
    ):
        raise ValueError(
            "Input counts differ from the preparation report."
        )

    for name in (
        "interview_cases.jsonl",
        "interview_answers.jsonl",
    ):
        if hashes[name] != report["output_sha256"][name]:
            raise ValueError(
                f"Prepared evidence changed: {name}."
            )

    case_map = {
        row["case_id"]: row
        for row in cases
    }

    if len(case_map) != len(cases):
        raise ValueError("Duplicate case IDs.")

    seen = set()
    question_sets = {}
    rows = []
    counts = Counter()

    for record in answers:
        evidence_id = record["evidence_id"]
        case_id = record["case_id"]
        number = record["question_number"]

        if evidence_id in seen or case_id not in case_map:
            raise ValueError(
                "Duplicate evidence ID or missing interview case."
            )

        seen.add(evidence_id)
        question_sets.setdefault(case_id, []).append(number)

        for item in (record, case_map[case_id]):
            if (
                item["data_origin"] != "SYNTHETIC"
                or item["source_sha256"] != report["source_sha256"]
            ):
                raise ValueError(
                    "Inconsistent source provenance."
                )

        codes, families, passages = candidates(
            record["answer_text"],
            number,
        )
        counts.update(codes)

        if number == 1:
            prompt_note = "mock_consent_only"
            review_status = "excluded_mock_consent"
        elif number == 13:
            prompt_note = "hypothetical_price_prompt"
            review_status = "needs_review"
        else:
            prompt_note = "read_full_answer"
            review_status = "needs_review"

        rows.append({
            **record,
            "fictional_situation": (
                case_map[case_id]["fictional_situation"]
            ),
            "contexts_discussed_text": (
                case_map[case_id]["contexts_discussed_text"]
            ),
            "candidate_codes": ";".join(codes),
            "candidate_rule_families": ";".join(families),
            "matched_passages_json": json.dumps(
                passages,
                ensure_ascii=False,
            ),
            "prompt_note": prompt_note,
            "review_status": review_status,
        })

    if (
        set(question_sets) != set(case_map)
        or any(
            sorted(numbers) != list(range(1, 21))
            for numbers in question_sets.values()
        )
    ):
        raise ValueError(
            "Each case must contain Q1–Q20 exactly once."
        )

    if any(
        sha256(files[name]) != value
        for name, value in hashes.items()
    ):
        raise ValueError(
            "Input changed during processing; rerun."
        )

    stamp = datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%S_%fZ"
    )

    output = (
        ROOT
        / "data/processed/synthetic"
        / f"interview_candidates_{stamp}"
    )
    output.mkdir(parents=True, exist_ok=False)

    with (
        output / "behaviour_candidates.csv"
    ).open(
        "w",
        encoding="utf-8-sig",
        newline="",
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=FIELDS,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)

    (
        output / "behaviour_codebook.json"
    ).write_text(
        json.dumps(
            CODEBOOK,
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )

    summary = {
        "data_origin": "SYNTHETIC",
        "cases": len(cases),
        "answers_retained": len(rows),
        "behaviour_answers": sum(
            row["question_number"] != 1
            for row in rows
        ),
        "excluded_mock_consent_answers": sum(
            row["question_number"] == 1
            for row in rows
        ),
        "answers_with_candidates": sum(
            bool(row["candidate_codes"])
            for row in rows
        ),
        "candidate_answer_counts": dict(counts),
        "accepted_codes": 0,
        "input_sha256": hashes,
        "parameters_fitted": False,
        "simulator_ready": False,
        "limitations": [
            (
                "Keyword candidates are review aids, "
                "not verified behavioural findings."
            ),
            (
                "Negation and hypothetical or exceptional statements "
                "require full-answer review."
            ),
            (
                "No match does not mean that the behaviour is absent."
            ),
            (
                "Counts describe scripted answer matches, "
                "not owner prevalence or probabilities."
            ),
            (
                "Qualitative timing rules must not shift "
                "already-reported survey start times twice."
            ),
        ],
    }

    (
        output / "candidate_report.json"
    ).write_text(
        json.dumps(
            summary,
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )

    print("Interview candidate coding complete.")

    for key in (
        "cases",
        "answers_retained",
        "behaviour_answers",
        "excluded_mock_consent_answers",
        "answers_with_candidates",
        "accepted_codes",
    ):
        print(f"{key}: {summary[key]}")

    print(f"Output: {output.relative_to(ROOT)}")
    print(
        "Full answers retained; candidates require review; "
        "no probabilities fitted."
    )


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(
            f"Candidate coding failed: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1)