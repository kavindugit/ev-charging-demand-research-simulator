"""Prepare traceable synthetic survey tables; no numerical imputation."""
from __future__ import annotations

import csv
from datetime import datetime, time, timezone
import json
from pathlib import Path
import sys

import openpyxl

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from ev_demand.ingestion.check_raw_inputs import (
    CONTEXTS,
    EXPECTED,
    blank,
    check_survey,
    find_one,
    normalise,
    sha256,
)

# Preserve answer labels. Numerical feature conversion comes later.
OWNER_FIELDS = {
    "Q4": "home_district_answer",
    "Q5": "vehicle_class_answer",
    "Q6": "battery_capacity_answer",
    "Q7": "vehicle_use_answer",
    "Q8": "weekly_frequency_answer",
    "Q10": "distance_since_charge_answer",
    "Q11": "starting_soc_answer",
    "Q12": "target_soc_answer",
    "Q15": "home_access_answer",
    "Q16": "home_equipment_answer",
    "Q17": "tou_meter_answer",
    "Q18": "intentional_delay_answer",
    "Q19": "timing_influences_answer",
    "Q20": "away_geography_answer",
    "Q21": "outside_district_answer",
    "Q22": "usual_energy_answer",
    "Q23": "recent_available_answer",
    "Q28": "history_willingness_answer",
}

UNKNOWN = {
    "I do not know",
    "I do not remember",
    "Not sure",
    "I do not know the time",
    "I am not sure whether my tariff has an off-peak period",
}

VARIABLE = {
    "It varies a lot",
    "It depends on the trip",
    "I use different types",
    "I do not know / it varies a lot",
}

NON_USE = {
    "I do not charge in this situation",
    "I do not use this charging situation",
    "I do not charge at home",
    "I do not charge away from home",
    "Not applicable – I do not use public charging",
}


def answer_state(value):
    text = normalise(value)

    if not text:
        return "missing"
    if text == "Never":
        return "never"
    if text in UNKNOWN:
        return "unknown"
    if text in VARIABLE:
        return "variable_or_uncertain"
    if text in NON_USE:
        return "not_used"
    if text == "Not applicable":
        return "not_applicable"
    if text == "I know the equipment but not the power level":
        return "power_unknown"

    return "reported_answer"


def serialise(value):
    if blank(value):
        return ""
    if isinstance(value, (datetime, time)):
        return value.isoformat()

    return normalise(value)


def time_text(value):
    if blank(value):
        return ""

    if isinstance(value, datetime):
        value = value.time()

    if isinstance(value, time):
        return value.strftime("%H:%M:%S")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = round(float(value) * 86400) % 86400
        return (
            f"{seconds // 3600:02d}:"
            f"{seconds % 3600 // 60:02d}:"
            f"{seconds % 60:02d}"
        )

    for pattern in (
        "%H:%M",
        "%H:%M:%S",
        "%I:%M %p",
        "%I:%M:%S %p",
    ):
        try:
            return datetime.strptime(
                normalise(value).upper(),
                pattern,
            ).strftime("%H:%M:%S")
        except ValueError:
            pass

    raise ValueError(
        "Unsupported recent-session time value; "
        "no imputation performed."
    )


def write_csv(path, rows, fields):
    # UTF-8 with BOM supports Unicode when opened in Windows Excel.
    with path.open("x", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def prepare(path):
    check = check_survey(path)

    if check["errors"]:
        raise ValueError(
            "Raw survey check failed: "
            + "; ".join(check["errors"])
        )

    source_hash = check["sha256"]
    owners = []
    contexts = []
    recent_sessions = []
    excluded = []

    quality_counts = {
        "recent_block_conflicts": 0,
        "context_nonuse_conflicts": 0,
    }

    workbook = openpyxl.load_workbook(
        path,
        read_only=True,
        data_only=True,
    )

    try:
        sheet = workbook.worksheets[0]
        rows = iter(sheet.values)

        headers = [normalise(h) for h in next(rows)]
        positions = {h: i for i, h in enumerate(headers)}

        field_positions = {
            field: positions[header]
            for field, header in EXPECTED.items()
        }

        for row_number, row in enumerate(rows, start=2):
            if all(blank(value) for value in row):
                continue

            get = lambda i: row[i] if i < len(row) else None

            values = {
                field: get(i)
                for field, i in field_positions.items()
            }

            provenance = {
                "data_origin": "SYNTHETIC",
                "source_file": path.name,
                "source_sha256": source_hash,
                "source_sheet": sheet.title,
                "source_row": row_number,
            }

            eligible = (
                normalise(values["Q1"]) == "Yes"
                and normalise(values["Q2"]) in {
                    "Yes , I own one",
                    "Yes , I regularly drive one",
                }
                and normalise(values["Q3"]) == "Yes, I agree"
            )

            if not eligible:
                excluded.append({
                    **provenance,
                    "reason": "screening_not_eligible",
                })
                continue

            identifier = (
                serialise(get(positions["Synthetic_Response_ID"]))
                if "Synthetic_Response_ID" in positions
                else ""
            )

            owner_id = (
                identifier
                or f"SYN-{source_hash[:12]}-R{row_number:06d}"
            )

            common = {
                "owner_id": owner_id,
                **provenance,
            }

            owner = {
                **common,
                "survey_timestamp_original": serialise(
                    values["Timestamp"]
                ),
            }

            owner.update({
                name: serialise(values[question])
                for question, name in OWNER_FIELDS.items()
            })

            influences = [
                part.strip()
                for part in owner["timing_influences_answer"].split(", ")
                if part.strip()
            ]

            owner["timing_influences_json"] = json.dumps(
                influences,
                ensure_ascii=False,
            )

            recent = normalise(values["Q23"])

            conflict = (
                recent in {"No", "I do not remember"}
                and any(
                    not blank(values[f"Q{n}"])
                    for n in range(24, 28)
                )
            )

            owner["recent_block_conflict"] = conflict
            quality_counts["recent_block_conflicts"] += int(conflict)
            owners.append(owner)

            for context in CONTEXTS:
                frequency = serialise(values[f"Q9_{context}"])
                charger = serialise(values[f"Q13_{context}"])
                start = serialise(values[f"Q14_{context}"])

                states = (
                    answer_state(frequency),
                    answer_state(charger),
                    answer_state(start),
                )

                nonuse_conflict = (
                    (
                        states[0] == "never"
                        and any(
                            state == "reported_answer"
                            for state in states[1:]
                        )
                    )
                    or (
                        states[0] == "reported_answer"
                        and "not_used" in states[1:]
                    )
                )

                quality_counts["context_nonuse_conflicts"] += int(
                    nonuse_conflict
                )

                contexts.append({
                    **common,
                    "context": context,
                    "frequency_answer": frequency,
                    "frequency_state": states[0],
                    "charger_answer": charger,
                    "charger_state": states[1],
                    "start_window_answer": start,
                    "start_window_state": states[2],
                    "nonuse_conflict": nonuse_conflict,
                })

            if recent == "Yes":
                place = serialise(values["Q24"])

                context = next(
                    (
                        code
                        for code, label in CONTEXTS.items()
                        if label == place
                    ),
                    "UNKNOWN",
                )

                recent_sessions.append({
                    **common,
                    "recent_session_id": owner_id + "-RECENT",
                    "evidence_kind": "survey_recall",
                    "context": context,
                    "context_answer": place,
                    "start_time_answer": serialise(values["Q25"]),
                    "start_time_local": time_text(values["Q25"]),
                    "duration_answer": serialise(values["Q26"]),
                    "duration_state": answer_state(values["Q26"]),
                    "energy_answer": serialise(values["Q27"]),
                    "energy_state": answer_state(values["Q27"]),
                    "session_date_state": "not_collected",
                    "active_duration_confirmed": False,
                })

    finally:
        workbook.close()

    if sha256(path) != source_hash:
        raise ValueError(
            "Source file changed during processing; "
            "rerun before exporting."
        )

    if len(contexts) != 4 * len(owners):
        raise ValueError("Owner-context relationship check failed.")

    if len({row["owner_id"] for row in owners}) != len(owners):
        raise ValueError("Duplicate owner identifiers.")

    if (
        len(owners) != check["eligible_rows"]
        or len(recent_sessions) != check["recent_session_yes_rows"]
    ):
        raise ValueError(
            "Prepared counts do not match the raw input check."
        )

    return (
        owners,
        contexts,
        recent_sessions,
        excluded,
        check,
        quality_counts,
    )


def main():
    try:
        path = find_one(ROOT / "data/raw/synthetic", ".xlsx")

        owners, contexts, recent, excluded, check, quality = prepare(
            path
        )

        stamp = datetime.now(timezone.utc).strftime(
            "%Y%m%dT%H%M%S_%fZ"
        )

        output = (
            ROOT
            / "data/interim/synthetic"
            / f"survey_{stamp}"
        )

        output.mkdir(parents=True, exist_ok=False)

        common = [
            "owner_id",
            "data_origin",
            "source_file",
            "source_sha256",
            "source_sheet",
            "source_row",
        ]

        owner_fields = (
            common
            + ["survey_timestamp_original"]
            + list(OWNER_FIELDS.values())
            + ["timing_influences_json", "recent_block_conflict"]
        )

        context_fields = common + [
            "context",
            "frequency_answer",
            "frequency_state",
            "charger_answer",
            "charger_state",
            "start_window_answer",
            "start_window_state",
            "nonuse_conflict",
        ]

        recent_fields = common + [
            "recent_session_id",
            "evidence_kind",
            "context",
            "context_answer",
            "start_time_answer",
            "start_time_local",
            "duration_answer",
            "duration_state",
            "energy_answer",
            "energy_state",
            "session_date_state",
            "active_duration_confirmed",
        ]

        excluded_fields = [
            "data_origin",
            "source_file",
            "source_sha256",
            "source_sheet",
            "source_row",
            "reason",
        ]

        write_csv(
            output / "owners.csv",
            owners,
            owner_fields,
        )

        write_csv(
            output / "owner_contexts.csv",
            contexts,
            context_fields,
        )

        write_csv(
            output / "recent_sessions.csv",
            recent,
            recent_fields,
        )

        write_csv(
            output / "excluded_rows.csv",
            excluded,
            excluded_fields,
        )

        report = {
            "stage": "survey_structural_preparation",
            "data_origin": "SYNTHETIC",
            "source_sha256": check["sha256"],
            "counts": {
                "owners": len(owners),
                "owner_contexts": len(contexts),
                "recent_sessions": len(recent),
                "excluded_rows": len(excluded),
            },
            "quality_counts": quality,
            "raw_check_warnings": check["warnings"],
            "simulation_ready": False,
            "full_option_validation_completed": False,
            "notes": [
                "No midpoints, probabilities, power values or energy values were imputed.",
                "Q29 contact details and screening answers are excluded from analytical tables.",
                "Usual energy is owner-level evidence, not a separate observation for each context.",
                "Recent sessions are recalled events with no collected session date.",
                "Interviews and app logs are separate sources and were not processed here.",
                "Category labels remain unchanged except whitespace normalisation; state labels are annotations.",
                "Fallback owner IDs are stable only within the same source-file version and row.",
            ],
        }

        with (output / "preparation_report.json").open(
            "x",
            encoding="utf-8",
        ) as handle:
            json.dump(
                report,
                handle,
                ensure_ascii=False,
                indent=2,
            )
            handle.write("\n")

        print("Survey preparation complete.")

        for name, count in report["counts"].items():
            print(f"{name}: {count}")

        print("Quality flags:", quality)
        print("Output:", output.relative_to(ROOT))
        print(
            "Raw survey unchanged. "
            "Ranges preserved. Contact details excluded."
        )

        return 0

    except (OSError, ValueError) as error:
        print(
            "Survey preparation stopped:",
            error,
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())