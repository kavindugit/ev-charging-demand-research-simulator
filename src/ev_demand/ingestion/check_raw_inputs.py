"""Check synthetic raw inputs without cleaning or changing them."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, time, timezone
import hashlib
import json
from pathlib import Path
import re
import sys

import openpyxl

ROOT = Path(__file__).resolve().parents[3]

QUESTIONS = {
    1: "Are you 18 years of age or older?",
    2: "Do you own or regularly drive a battery-electric vehicle in Sri Lanka?",
    3: "Have you read the information above and voluntarily agreed to participate?",
    4: "What is your home district?",
    5: "What type of battery-electric vehicle do you mainly use?",
    6: "What is the approximate battery capacity of this vehicle?",
    7: "How is this vehicle mainly used?",
    8: "In a usual week, approximately how many times do you charge this vehicle in total?",
    10: "Before charging, how far do you usually drive since the previous charge?",
    11: "When you start charging, what is the battery level usually around?",
    12: "When you charge, what battery level do you usually charge up to?",
    15: "Do you have access to charging at home?",
    16: "If you charge at home, what type of home charging equipment do you usually use?",
    17: "Does your home have a registered CEB Time-of-Use (TOU) electricity meter?",
    18: "If you charge at home, do you intentionally delay charging to an off-peak period to reduce electricity cost?",
    19: "What mainly influences the time you choose to charge?",
    20: "When you charge away from home, where does it normally happen?",
    21: "If you often charge outside your home district, which district is most common?",
    22: "If you know, approximately how much energy is normally delivered during one charging session?",
    23: "In the last 2–3 days, did you charge this vehicle?",
    24: "If yes, where did the most recent charging session happen?",
    25: "If yes, approximately what time did that most recent charging session start?",
    26: "If yes, approximately how long did that most recent charging session last?",
    27: "If yes, approximately how much energy was delivered in that most recent charging session?",
    28: "Are you willing to share redacted public-charging app history screenshots or records for academic research?",
    29: "If you are willing to share app history records, how can the researcher contact you?",
}

CONTEXTS = {
    "T2": "At home",
    "T1": "At workplace, education place, shopping area, or other routine non-home place",
    "T3": "At tourism, leisure, hotel, restaurant, or destination place",
    "T4": "During a long trip / highway / en-route stop",
}

GRID_QUESTIONS = {
    9: "How often do you charge in each of the following situations?",
    13: "What charger type do you normally use in each situation?",
    14: "At what time do you usually start charging in each situation?",
}

EXPECTED = {"Timestamp": "Timestamp"}
EXPECTED.update({f"Q{n}": f"{n}. {text}" for n, text in QUESTIONS.items()})

for n, text in GRID_QUESTIONS.items():
    for context, label in CONTEXTS.items():
        EXPECTED[f"Q{n}_{context}"] = f"{n}. {text} [{label}]"


def normalise(value):
    return " ".join(str(value).split()) if value is not None else ""


def blank(value):
    return value is None or (
        isinstance(value, str) and not value.strip()
    )


def sha256(path):
    digest = hashlib.sha256()

    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def valid_time(value):
    if isinstance(value, (time, datetime)):
        return True

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return 0 <= value < 1

    for pattern in (
        "%H:%M",
        "%H:%M:%S",
        "%I:%M %p",
        "%I:%M:%S %p",
    ):
        try:
            datetime.strptime(normalise(value).upper(), pattern)
            return True
        except ValueError:
            pass

    return False


def find_one(folder, extension):
    files = sorted(
        p for p in folder.glob(f"*{extension}")
        if not p.name.startswith("~$")
    )

    if len(files) != 1:
        raise ValueError(
            f"Expected one {extension} file in {folder}; "
            f"found {len(files)}."
        )

    return files[0]


def check_survey(path):
    report = {
        "filename": path.name,
        "sha256": sha256(path),
        "errors": [],
        "warnings": [],
    }

    workbook = openpyxl.load_workbook(
        path,
        read_only=True,
        data_only=True,
    )

    try:
        sheet = workbook.worksheets[0]
        rows = iter(sheet.values)
        headers = [normalise(h) for h in next(rows, [])]

        report["sheet"] = sheet.title
        report["column_count"] = len(headers)

        if len(headers) != len(set(headers)):
            report["errors"].append("Duplicate column headers.")

        missing = [
            field
            for field, header in EXPECTED.items()
            if header not in headers
        ]

        report["missing_fields"] = missing

        if missing:
            report["errors"].append(
                "Survey columns are missing or renamed."
            )

        if report["errors"]:
            return report

        positions = {h: i for i, h in enumerate(headers)}

        field_positions = {
            field: positions[header]
            for field, header in EXPECTED.items()
        }

        blank_counts = Counter({field: 0 for field in EXPECTED})

        total = eligible = recent_yes = recent_skip = 0
        branch_conflicts = invalid_times = invalid_screening = 0
        invalid_recent = origin_conflicts = duplicate_ids = 0
        seen_ids = set()

        allowed_screening = {
            "Q1": {"Yes", "No"},
            "Q2": {
                "Yes , I own one",
                "Yes , I regularly drive one",
                "No",
            },
            "Q3": {
                "Yes, I agree",
                "No, I do not agree",
            },
        }

        for row in rows:
            if all(blank(value) for value in row):
                continue

            total += 1

            get = lambda i: row[i] if i < len(row) else None

            values = {
                field: get(i)
                for field, i in field_positions.items()
            }

            for field, value in values.items():
                blank_counts[field] += int(blank(value))

            invalid_screening += int(
                any(
                    normalise(values[field])
                    not in allowed_screening[field]
                    for field in allowed_screening
                )
            )

            is_eligible = (
                normalise(values["Q1"]) == "Yes"
                and normalise(values["Q2"]) in {
                    "Yes , I own one",
                    "Yes , I regularly drive one",
                }
                and normalise(values["Q3"]) == "Yes, I agree"
            )

            eligible += int(is_eligible)

            if "Data_Origin" in positions:
                origin_conflicts += int(
                    normalise(
                        get(positions["Data_Origin"])
                    ).upper() != "SYNTHETIC"
                )

            if "Synthetic_Response_ID" in positions:
                identifier = normalise(
                    get(positions["Synthetic_Response_ID"])
                )

                if identifier:
                    duplicate_ids += int(identifier in seen_ids)
                    seen_ids.add(identifier)

            if is_eligible:
                recent = normalise(values["Q23"])

                recent_yes += int(recent == "Yes")

                recent_skip += int(
                    recent in {"No", "I do not remember"}
                )

                invalid_recent += int(
                    recent not in {
                        "Yes",
                        "No",
                        "I do not remember",
                    }
                )

                if recent in {"No", "I do not remember"}:
                    branch_conflicts += int(
                        any(
                            not blank(values[f"Q{n}"])
                            for n in range(24, 28)
                        )
                    )

                if not blank(values["Q25"]):
                    invalid_times += int(
                        not valid_time(values["Q25"])
                    )

        report.update({
            "row_count": total,
            "eligible_rows": eligible,
            "excluded_rows": total - eligible,
            "recent_session_yes_rows": recent_yes,
            "recent_session_skipped_rows": recent_skip,
            "blank_counts": dict(blank_counts),
            "branch_conflict_rows": branch_conflicts,
            "invalid_start_times": invalid_times,
            "invalid_screening_rows": invalid_screening,
            "invalid_recent_session_answers": invalid_recent,
            "duplicate_synthetic_ids": duplicate_ids,
        })

        if not total:
            report["errors"].append("No survey response rows.")

        if invalid_screening or invalid_recent or invalid_times:
            report["errors"].append(
                "Invalid screening, recent-session or time values; "
                "inspect the reported counts."
            )

        if origin_conflicts or duplicate_ids:
            report["errors"].append(
                "Synthetic metadata contains inconsistent labels "
                "or duplicate IDs."
            )

        if "Data_Origin" not in positions:
            report["warnings"].append(
                "No Data_Origin column; the synthetic label comes "
                "from this development workflow."
            )

        if branch_conflicts:
            report["warnings"].append(
                "Some skipped recent-session blocks contain "
                "answers; review them later."
            )

        return report

    finally:
        workbook.close()


def check_interviews(path):
    text = path.read_text(encoding="utf-8-sig")

    report = {
        "filename": path.name,
        "sha256": sha256(path),
        "errors": [],
        "warnings": [],
    }

    if "SYNTHETIC" not in text[:2500].upper():
        report["errors"].append(
            "Expected a synthetic-data notice in the preamble."
        )

    cases = list(
        re.finditer(
            r"^##\s+(S\d{3})\s+[—-]\s+SYNTHETIC\s*$",
            text,
            re.MULTILINE,
        )
    )

    case_ids = [match.group(1) for match in cases]
    incomplete = []

    for i, match in enumerate(cases):
        end = (
            cases[i + 1].start()
            if i + 1 < len(cases)
            else len(text)
        )

        body = text[match.end():end]

        questions = [
            int(n)
            for n in re.findall(
                r"^\*\*Q(\d+)\. Interviewer:",
                body,
                re.MULTILINE,
            )
        ]

        answer_pattern = (
            r"^\*\*"
            + re.escape(match.group(1))
            + r" \(fictional\):\*\*"
        )

        answers = re.findall(
            answer_pattern,
            body,
            re.MULTILINE,
        )

        if questions != list(range(1, 21)) or len(answers) != 20:
            incomplete.append(match.group(1))

    report.update({
        "case_count": len(cases),
        "incomplete_case_ids": incomplete,
        "coding_performed": False,
    })

    if (
        not cases
        or incomplete
        or len(case_ids) != len(set(case_ids))
    ):
        report["errors"].append(
            "Missing, duplicate or incomplete interview cases."
        )

    return report


def main():
    raw = ROOT / "data/raw/synthetic"

    try:
        survey = check_survey(find_one(raw, ".xlsx"))
        interviews = check_interviews(find_one(raw, ".md"))

        errors = survey["errors"] + interviews["errors"]
        warnings = survey["warnings"] + interviews["warnings"]

        status = (
            "BLOCKED" if errors
            else "REVIEW" if warnings
            else "PASSED"
        )

        report = {
            "step": "raw_input_format_check",
            "data_origin": "SYNTHETIC",
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "survey": survey,
            "interviews": interviews,
            "simulation_ready": False,
            "scope": (
                "Format, screening, recent-block and transcript "
                "completeness checks only. Full option validation, "
                "cleaning and coding follow later."
            ),
        }

        output_folder = ROOT / "outputs/experiments/synthetic"
        output_folder.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now(timezone.utc).strftime(
            "%Y%m%dT%H%M%S_%fZ"
        )

        output = output_folder / f"raw_input_check_{stamp}.json"

        with output.open("x", encoding="utf-8") as handle:
            json.dump(
                report,
                handle,
                ensure_ascii=False,
                indent=2,
            )
            handle.write("\n")

        print(f"Input check: {status}")
        print("Survey rows:", survey.get("row_count", "not evaluated"))
        print("Eligible rows:", survey.get("eligible_rows", "not evaluated"))
        print(
            "Recent-session rows:",
            survey.get("recent_session_yes_rows", "not evaluated"),
        )
        print(
            "Skipped recent-session rows:",
            survey.get("recent_session_skipped_rows", "not evaluated"),
        )
        print("Interview cases:", interviews["case_count"])

        for message in errors + warnings:
            print("-", message)

        print("Report:", output.relative_to(ROOT))
        print(
            "Raw files unchanged. "
            "No responses or interview passages exported."
        )

        return 2 if errors else 0

    except (OSError, ValueError) as error:
        print("Could not check inputs:", error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())