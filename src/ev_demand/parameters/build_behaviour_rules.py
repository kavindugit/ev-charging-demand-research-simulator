"""Build traceable design rules from selected synthetic interview passages."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]

# Contextually reviewed examples, not automatically accepted keyword codes.
# Evidence quotations are checked against the preserved full answers.
RULES = [
    {
        "rule_id": "R01",
        "name": "Keep multiple contexts within one owner",
        "status": "design_constraint",
        "candidate_code": "CONTEXT_DISCUSSION",
        "evidence_id": "S001_Q02",
        "quote": (
            "If I have errands after work, I sometimes use "
            "a charger near the office."
        ),
        "implementation": (
            "Sample an owner profile with all its context records. "
            "Allocate each session to one context; do not treat "
            "each context row as a different owner."
        ),
        "survey_fields": [
            "owner_id",
            "context",
            "context_frequency_min",
            "context_frequency_max",
        ],
        "boundary": (
            "Interview context mentions are not allocation weights. "
            "Survey total and context frequencies still need reconciliation."
        ),
    },
    {
        "rule_id": "R02",
        "name": "Use the reported charging-start window once",
        "status": "design_constraint",
        "candidate_code": "DELAY_DISCUSSION",
        "evidence_id": "S001_Q06",
        "quote": (
            "I connect it when I get home, but I set the car "
            "to begin charging later."
        ),
        "implementation": (
            "Use survey charging-start windows as actual start windows. "
            "Do not apply an additional automatic TOU delay "
            "to the baseline profile."
        ),
        "survey_fields": [
            "window_start_hour",
            "window_end_hour",
            "window_crosses_midnight",
            "intentional_delay_answer",
            "tou_meter_answer",
        ],
        "boundary": (
            "A future tariff-change scenario would require separately "
            "stated assumptions; usual reported timing does not "
            "measure its causal effect."
        ),
    },
    {
        "rule_id": "R03",
        "name": "Separate active charging from connected time",
        "status": "design_constraint",
        "candidate_code": "PARKING_DISCUSSION",
        "evidence_id": "S001_Q14",
        "quote": (
            "At home the car can remain connected after it finishes, "
            "but it stops drawing power at the target."
        ),
        "implementation": (
            "Accumulate demand only over active charging intervals. "
            "Keep parked or connected duration separate from "
            "electrical charging duration."
        ),
        "survey_fields": [
            "reported_duration_hours_min",
            "reported_duration_hours_max",
            "active_duration_confirmed",
        ],
        "boundary": (
            "A recalled duration is not automatically a measured active "
            "duration. Any simplified power curve must be declared separately."
        ),
    },
    {
        "rule_id": "R04",
        "name": "Respect reported home-charging access",
        "status": "design_constraint",
        "candidate_code": "CONTEXT_DISCUSSION",
        "evidence_id": "S002_Q06",
        "quote": (
            "I cannot charge at home. I park in a shared apartment area "
            "and there is no approved charging point there."
        ),
        "implementation": (
            "If survey home access is 'No, I cannot charge at home', "
            "do not create T2 sessions. Flag contradictory home-use answers "
            "for review instead of silently rewriting them."
        ),
        "survey_fields": [
            "home_access_answer",
            "frequency_answer",
            "context",
        ],
        "boundary": (
            "Limited access is not the same as no access. "
            "The interview case cannot be linked to a survey owner "
            "by its number."
        ),
    },
    {
        "rule_id": "R05",
        "name": "Keep energy accounting consistent across sessions",
        "status": "design_constraint",
        "candidate_code": "CONTEXT_DISCUSSION",
        "evidence_id": "S001_Q16",
        "quote": "I charged less at home the following night.",
        "implementation": (
            "Account for energy across an owner's sessions. "
            "A context switch must not automatically add a second "
            "full daily energy requirement; extra travel can increase "
            "energy only through an explicit assumption or observation."
        ),
        "survey_fields": [
            "sessions_per_week_min",
            "sessions_per_week_max",
            "usual_energy_kwh_min",
            "usual_energy_kwh_max",
        ],
        "boundary": (
            "The passage supports possible substitution, not a fixed "
            "amount or universal replacement rule. Do not add "
            "distance-derived energy to delivered energy "
            "for the same requirement."
        ),
    },
    {
        "rule_id": "R06",
        "name": "Keep temporary disruption separate from usual behaviour",
        "status": "design_constraint",
        "candidate_code": "EXCEPTION_DISCUSSION",
        "evidence_id": "S040_Q19",
        "quote": (
            "A temporary equipment problem can make a normally "
            "home-dominant owner appear public-dominant "
            "in a short observation window."
        ),
        "implementation": (
            "Retain an exceptional-period annotation when reviewing "
            "evidence. Do not overwrite usual survey behaviour solely "
            "because a short public-charging history differs."
        ),
        "survey_fields": [
            "recent_available_answer",
            "frequency_answer",
            "home_access_answer",
        ],
        "boundary": (
            "Do not infer an equipment failure from a mismatch alone, "
            "or automatically discard records. An exceptional-period "
            "flag requires supporting evidence."
        ),
    },
    {
        "rule_id": "R07",
        "name": "Trip urgency may change charging decisions",
        "status": "deferred_effect",
        "candidate_code": "TRIP_DISCUSSION",
        "evidence_id": "S001_Q15",
        "quote": (
            "If I do not know whether there will be an extra trip "
            "tomorrow, I charge earlier and aim a little higher."
        ),
        "implementation": (
            "Keep this as a contextual dependency. Do not introduce "
            "random urgent-trip events or a fixed target increase "
            "in the baseline yet."
        ),
        "survey_fields": [
            "start_soc_fraction_min",
            "target_soc_fraction_max",
            "timing_influences_answer",
        ],
        "boundary": (
            "An event rate and adjustment magnitude need observations "
            "or explicitly labelled sensitivity assumptions; "
            "'a little higher' provides neither."
        ),
    },
    {
        "rule_id": "R08",
        "name": "Unavailable chargers may change the next action",
        "status": "deferred_effect",
        "candidate_code": "AVAILABILITY_DISCUSSION",
        "evidence_id": "S001_Q09",
        "quote": (
            "If I can get home comfortably, I leave; otherwise "
            "I check a second station or wait a short time."
        ),
        "implementation": (
            "Retain the conditional options for later analysis. "
            "Do not invent queue durations, station failures, "
            "or switch probabilities in the baseline."
        ),
        "survey_fields": [
            "home_access_answer",
            "charger_code",
        ],
        "boundary": (
            "Executing a queue or station-availability model would "
            "require further evidence or an explicitly separate "
            "sensitivity scenario."
        ),
    },
]


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def input_folder(value, allowed):
    folder = (ROOT / value).resolve()

    if not folder.is_relative_to((ROOT / allowed).resolve()):
        raise ValueError(f"Input must be under {allowed}.")

    return folder


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interviews", required=True)
    parser.add_argument("--candidates", required=True)
    args = parser.parse_args()

    interviews = input_folder(
        args.interviews,
        "data/interim/synthetic",
    )
    candidate_folder = input_folder(
        args.candidates,
        "data/processed/synthetic",
    )

    paths = {
        "interview_answers": (
            interviews / "interview_answers.jsonl"
        ),
        "interview_report": (
            interviews / "interview_preparation_report.json"
        ),
        "candidates": (
            candidate_folder / "behaviour_candidates.csv"
        ),
        "candidate_report": (
            candidate_folder / "candidate_report.json"
        ),
    }

    hashes = {
        name: sha256(path)
        for name, path in paths.items()
    }

    report = json.loads(
        paths["interview_report"].read_text(encoding="utf-8")
    )
    candidate_report = json.loads(
        paths["candidate_report"].read_text(encoding="utf-8")
    )

    if any(
        item.get("data_origin") != "SYNTHETIC"
        for item in (report, candidate_report)
    ):
        raise ValueError(
            "This rule package is for synthetic development only."
        )

    if (
        hashes["interview_answers"]
        != report["output_sha256"]["interview_answers.jsonl"]
    ):
        raise ValueError(
            "Preserved interview answers changed."
        )

    for report_key, hash_key in (
        ("interview_answers.jsonl", "interview_answers"),
        ("interview_preparation_report.json", "interview_report"),
    ):
        if (
            candidate_report["input_sha256"][report_key]
            != hashes[hash_key]
        ):
            raise ValueError(
                "Candidates and interviews come from different preparations."
            )

    answers = [
        json.loads(line)
        for line in paths["interview_answers"]
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]

    with paths["candidates"].open(
        encoding="utf-8-sig",
        newline="",
    ) as handle:
        candidates = list(csv.DictReader(handle))

    answer_map = {
        row["evidence_id"]: row
        for row in answers
    }
    candidate_map = {
        row["evidence_id"]: row
        for row in candidates
    }

    if (
        len(answer_map) != len(answers)
        or len(candidate_map) != len(candidates)
    ):
        raise ValueError("Duplicate evidence IDs.")

    if (
        len(answers) != report["answers"]
        or len(candidates) != candidate_report["answers_retained"]
        or set(answer_map) != set(candidate_map)
    ):
        raise ValueError(
            "Candidate evidence coverage is inconsistent."
        )

    for evidence_id, answer in answer_map.items():
        candidate = candidate_map[evidence_id]

        if any(
            candidate[key] != str(answer[key])
            for key in (
                "answer_text",
                "question_text",
                "case_id",
                "question_number",
                "source_sha256",
                "data_origin",
            )
        ):
            raise ValueError(
                f"Candidate source evidence differs: {evidence_id}."
            )

        if (
            answer["data_origin"] != "SYNTHETIC"
            or answer["source_sha256"] != report["source_sha256"]
        ):
            raise ValueError(
                "Inconsistent interview provenance."
            )

    linked_rules = []

    for rule in RULES:
        answer = answer_map[rule["evidence_id"]]
        candidate = candidate_map[rule["evidence_id"]]

        if rule["quote"] not in answer["answer_text"]:
            raise ValueError(
                f"Evidence quotation not found for {rule['rule_id']}."
            )

        if (
            rule["candidate_code"]
            not in candidate["candidate_codes"].split(";")
        ):
            raise ValueError(
                f"Expected review candidate missing for {rule['rule_id']}."
            )

        linked_rules.append({
            **rule,
            "data_origin": "SYNTHETIC",
            "review_method": (
                "AI-assisted contextual reading for synthetic design; "
                "exact quotation verified by script"
            ),
            "human_review_confirmed": False,
            "full_answer": answer["answer_text"],
            "source_file": answer["source_file"],
            "source_sha256": answer["source_sha256"],
            "answer_line_start": answer["answer_line_start"],
            "answer_line_end": answer["answer_line_end"],
            "numeric_parameters": {},
        })

    if any(
        sha256(paths[name]) != value
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
        / f"behaviour_rules_{stamp}"
    )
    output.mkdir(parents=True, exist_ok=False)

    package = {
        "schema_version": "behaviour_rules_v1",
        "data_origin": "SYNTHETIC",
        "purpose": (
            "Constraints and deferred effects "
            "for developing the session simulator"
        ),
        "rules": linked_rules,
        "input_sha256": hashes,
        "review_scope": (
            "Selected passages only; the remaining interview answers "
            "are not coded or approved by this step."
        ),
        "empirical_validation_completed": False,
        "parameters_fitted": False,
        "simulator_parameter_package_ready": False,
        "constraints": [
            (
                "No survey owner is linked to an interview case "
                "by its number."
            ),
            (
                "No event rates, power ratings or energy adjustments "
                "are inferred from qualitative words."
            ),
            (
                "Survey field names are mapping references, "
                "not evidence of calibrated parameters."
            ),
            (
                "These scripted interviews provide no empirical "
                "prevalence or national estimates."
            ),
        ],
    }

    (
        output / "behaviour_rules.json"
    ).write_text(
        json.dumps(
            package,
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )

    print("Behaviour rule package complete.")
    print(f"rules: {len(linked_rules)}")

    print(
        "design_constraints: "
        f"{sum(rule['status'] == 'design_constraint' for rule in linked_rules)}"
    )
    print(
        "deferred_effects: "
        f"{sum(rule['status'] == 'deferred_effect' for rule in linked_rules)}"
    )
    print(
        "evidence_passages: "
        f"{len({rule['evidence_id'] for rule in linked_rules})}"
    )
    print(f"Output: {output.relative_to(ROOT)}")
    print(
        "Selected quotations verified; no numerical parameters fitted; "
        "raw data unchanged."
    )


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(
            f"Behaviour rule preparation failed: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1)