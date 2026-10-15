"""Encode survey intervals and timing; preserve original answers."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]


# Rule: lower bound, upper bound, boundary convention, evidence state.
def interval(lower, upper, boundary="closed", state="reported_range"):
    return lower, upper, boundary, state


EMPTY = (None, None, "none", "missing")
UNKNOWN = (None, None, "none", "unknown")
VARIABLE = (None, None, "none", "variable_or_uncertain")
NOT_USED = (None, None, "none", "not_used")

# Continuous integer-labelled bins use an exclusive next-integer edge.
BATTERY = {
    "Less than 20 kWh": interval(0, 20, "open", "upper_bounded"),
    **{
        f"{a}–{b} kWh": interval(
            a, b + 1, "left_closed_right_open"
        )
        for a, b in [(20, 39), (40, 59), (60, 79)]
    },
    "80 kWh or more": interval(
        80, None, "left_closed", "open_upper"
    ),
    "I do not know": UNKNOWN,
}

WEEKLY = {
    "1 time or less": interval(0, 1),
    **{
        f"{a}–{b} times": interval(a, b)
        for a, b in [(2, 3), (4, 5), (6, 7)]
    },
    "More than 7 times": interval(
        7, None, "left_open", "open_upper"
    ),
    "It varies a lot": VARIABLE,
}

DISTANCE = {
    "Less than 25 km": interval(
        0, 25, "left_closed_right_open", "upper_bounded"
    ),
    **{
        f"{a}–{b} km": interval(
            a, b + 1, "left_closed_right_open"
        )
        for a, b in [
            (25, 49),
            (50, 99),
            (100, 149),
            (150, 199),
        ]
    },
    "200 km or more": interval(
        200, None, "left_closed", "open_upper"
    ),
    "I do not know / it varies a lot": VARIABLE,
}

START_SOC = {
    "Less than 10%": interval(
        0, 0.10, "left_closed_right_open", "upper_bounded"
    ),
    **{
        f"{a}–{b}%": interval(
            a / 100,
            (b + 1) / 100,
            "left_closed_right_open",
        )
        for a, b in [
            (10, 19),
            (20, 29),
            (30, 39),
            (40, 49),
        ]
    },
    "50% or more": interval(
        0.50, 1, "closed", "physical_upper_bound"
    ),
    "It varies a lot": VARIABLE,
    "I do not know": UNKNOWN,
}

TARGET_SOC = {
    "Less than 60%": interval(
        0, 0.60, "left_closed_right_open", "upper_bounded"
    ),
    **{
        f"{a}–{b}%": interval(
            a / 100,
            (b + 1) / 100,
            "left_closed_right_open",
        )
        for a, b in [(60, 79), (80, 89), (90, 99)]
    },
    "100%": interval(1, 1, "closed", "reported_point"),
    "It depends on the trip": VARIABLE,
    "I do not know": UNKNOWN,
}

ENERGY = {
    "Less than 5 kWh": interval(
        0, 5, "open", "upper_bounded"
    ),
    **{
        label: interval(a, b, "left_closed_right_open")
        for label, a, b in [
            ("5–9.9 kWh", 5, 10),
            ("10–19.9 kWh", 10, 20),
            ("20–39.9 kWh", 20, 40),
            ("40–59.9 kWh", 40, 60),
        ]
    },
    "60 kWh or more": interval(
        60, None, "left_closed", "open_upper"
    ),
    "It varies a lot": VARIABLE,
    "I do not know": UNKNOWN,
    "I did not charge in the last 2–3 days": NOT_USED,
}

HOME_POWER = {
    "Standard socket / portable charger, around 1–3 kW":
        interval(1, 3, "closed", "approximate_range"),

    "Dedicated AC wall charger, around 3–7 kW":
        interval(3, 7, "closed", "approximate_range"),

    "Dedicated AC wall charger, more than 7 kW":
        interval(7, None, "left_open", "open_upper"),

    "I do not charge at home": NOT_USED,
    "I know the equipment but not the power level": UNKNOWN,
    "I do not know": UNKNOWN,
    "Other": UNKNOWN,
}

DURATION = {
    "Less than 30 minutes": interval(
        0, 0.5, "open", "upper_bounded"
    ),
    "30–59 minutes": interval(
        0.5, 1, "left_closed_right_open"
    ),
    "1–2 hours": interval(
        1, 2, "closed", "approximate_range"
    ),
    "2–4 hours": interval(
        2, 4, "closed", "approximate_range"
    ),
    "More than 4 hours": interval(
        4, None, "left_open", "open_upper"
    ),
    "I did not charge in the last 2–3 days": NOT_USED,
    "I do not remember": UNKNOWN,
}

CONTEXT_FREQUENCY = {
    "Never": (
        *interval(0, 0, "closed", "reported_zero"),
        "week",
    ),
    "Less than once a month": (
        *interval(0, 1, "open", "upper_bounded"),
        "month",
    ),
    "1–3 times a month": (
        *interval(1, 3),
        "month",
    ),
    "1–2 times a week": (
        *interval(1, 2),
        "week",
    ),
    "3 or more times a week": (
        *interval(3, None, "left_closed", "open_upper"),
        "week",
    ),
}

WINDOWS = {
    "Morning: 6 AM–10 AM":
        (6, 10, 4, False, "reported_window"),

    "Midday: 10 AM–2 PM":
        (10, 14, 4, False, "reported_window"),

    "Afternoon: 2 PM–6 PM":
        (14, 18, 4, False, "reported_window"),

    "Evening: 6 PM–10 PM":
        (18, 22, 4, False, "reported_window"),

    "Night: 10 PM–6 AM":
        (22, 6, 8, True, "reported_window"),

    "I do not use this charging situation":
        (None, None, None, None, "not_used"),
}

CHARGERS = {
    "Standard socket / portable charger": "SOCKET_PORTABLE",
    "AC charger": "AC",
    "DC fast charger": "DC",
    "I use different types": "MIXED",
    "I do not charge in this situation": "NOT_USED",
    "I do not know": "UNKNOWN",
}

OWNER_RULES = {
    "battery_capacity_answer": ("battery_kwh", BATTERY),
    "weekly_frequency_answer": ("sessions_per_week", WEEKLY),
    "distance_since_charge_answer": ("distance_km", DISTANCE),
    "starting_soc_answer": ("start_soc_fraction", START_SOC),
    "target_soc_answer": ("target_soc_fraction", TARGET_SOC),
    "usual_energy_answer": ("usual_energy_kwh", ENERGY),
    "home_equipment_answer": ("home_power_kw", HOME_POWER),
}


def lookup(table, answer, field):
    if not answer.strip():
        return None

    if answer not in table:
        raise ValueError(
            f"Unrecognised answer in {field}: {answer!r}. "
            "Review the rules."
        )

    return table[answer]


def add_bounds(row, prefix, rule):
    lower, upper, boundary, state = (
        rule if rule is not None else EMPTY
    )

    row.update({
        prefix + "_min": lower,
        prefix + "_max": upper,
        prefix + "_boundary": boundary,
        prefix + "_state": state,
    })


def read_csv(path):
    with path.open(
        encoding="utf-8-sig",
        newline="",
    ) as handle:
        reader = csv.DictReader(handle)
        return list(reader), list(reader.fieldnames or [])


def write_csv(path, rows, fields):
    with path.open(
        "x",
        encoding="utf-8-sig",
        newline="",
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def encode(owners, contexts, recent):
    for row in owners:
        for column, (prefix, rules) in OWNER_RULES.items():
            add_bounds(
                row,
                prefix,
                lookup(rules, row[column], column),
            )

    for row in contexts:
        rule = lookup(
            CONTEXT_FREQUENCY,
            row["frequency_answer"],
            "frequency_answer",
        )

        add_bounds(
            row,
            "context_frequency",
            rule[:4] if rule else None,
        )

        row["context_frequency_period"] = (
            rule[4] if rule else None
        )

        row["charger_code"] = (
            lookup(
                CHARGERS,
                row["charger_answer"],
                "charger_answer",
            )
            or "MISSING"
        )

        window = lookup(
            WINDOWS,
            row["start_window_answer"],
            "start_window_answer",
        )

        start, end, length, crossing, state = (
            window
            or (None, None, None, None, "missing")
        )

        row.update({
            "window_start_hour": start,
            "window_end_hour": end,
            "window_length_hours": length,
            "window_crosses_midnight": crossing,
            "window_state": state,
        })

    for row in recent:
        add_bounds(
            row,
            "recent_energy_kwh",
            lookup(
                ENERGY,
                row["energy_answer"],
                "energy_answer",
            ),
        )

        add_bounds(
            row,
            "reported_duration_hours",
            lookup(
                DURATION,
                row["duration_answer"],
                "duration_answer",
            ),
        )

        value = row["start_time_local"]

        parsed = (
            datetime.strptime(value, "%H:%M:%S")
            if value else None
        )

        row["recent_start_hour"] = (
            parsed.hour
            + parsed.minute / 60
            + parsed.second / 3600
            if parsed else None
        )

    return owners, contexts, recent


def main():
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument(
        "--input",
        required=True,
        help="Folder created by prepare_survey.py",
    )

    args = parser.parse_args()

    try:
        supplied = Path(args.input)

        source = (
            supplied if supplied.is_absolute()
            else ROOT / supplied
        ).resolve()

        allowed = (
            ROOT / "data/interim/synthetic"
        ).resolve()

        if not source.is_relative_to(allowed):
            raise ValueError(
                "This development step expects an input "
                "below data/interim/synthetic."
            )

        input_hashes = {
            name: hashlib.sha256(
                (source / name).read_bytes()
            ).hexdigest()
            for name in [
                "owners.csv",
                "owner_contexts.csv",
                "recent_sessions.csv",
                "preparation_report.json",
            ]
        }

        previous = json.loads(
            (source / "preparation_report.json").read_text(
                encoding="utf-8"
            )
        )

        if previous.get("data_origin") != "SYNTHETIC":
            raise ValueError(
                "Expected a synthetic preparation report."
            )

        owners, owner_fields = read_csv(
            source / "owners.csv"
        )
        contexts, context_fields = read_csv(
            source / "owner_contexts.csv"
        )
        recent, recent_fields = read_csv(
            source / "recent_sessions.csv"
        )

        counts = {
            "owners": len(owners),
            "owner_contexts": len(contexts),
            "recent_sessions": len(recent),
        }

        if not owners:
            raise ValueError(
                "No eligible owner rows; "
                "there is no dataset to encode."
            )

        if any(
            count != previous["counts"][name]
            for name, count in counts.items()
        ):
            raise ValueError(
                "Input row counts differ "
                "from the preparation report."
            )

        ids = {row["owner_id"] for row in owners}

        if len(ids) != len(owners):
            raise ValueError("Duplicate owner IDs.")

        owner_contexts = {
            owner_id: [] for owner_id in ids
        }

        for row in contexts:
            if row["owner_id"] not in ids:
                raise ValueError(
                    "Context row has no matching owner."
                )

            owner_contexts[row["owner_id"]].append(
                row["context"]
            )

        if any(
            sorted(codes) != ["T1", "T2", "T3", "T4"]
            for codes in owner_contexts.values()
        ):
            raise ValueError(
                "Each owner must have exactly "
                "one row for each context."
            )

        if (
            len({row["owner_id"] for row in recent})
            != len(recent)
            or any(
                row["owner_id"] not in ids
                for row in recent
            )
        ):
            raise ValueError(
                "Recent-session owner relationships "
                "are invalid."
            )

        for row in owners + contexts + recent:
            if (
                row["data_origin"] != "SYNTHETIC"
                or row["source_sha256"]
                != previous["source_sha256"]
            ):
                raise ValueError(
                    "Input provenance does not match "
                    "the preparation report."
                )

        owners, contexts, recent = encode(
            owners,
            contexts,
            recent,
        )

        if any(
            hashlib.sha256(
                (source / name).read_bytes()
            ).hexdigest() != digest
            for name, digest in input_hashes.items()
        ):
            raise ValueError(
                "An input table changed during processing; "
                "rerun before exporting."
            )

        stamp = datetime.now(timezone.utc).strftime(
            "%Y%m%dT%H%M%S_%fZ"
        )

        output = (
            ROOT
            / "data/processed/synthetic"
            / f"features_{stamp}"
        )

        output.mkdir(parents=True, exist_ok=False)

        datasets = [
            ("owners", owners, owner_fields),
            ("owner_contexts", contexts, context_fields),
            ("recent_sessions", recent, recent_fields),
        ]

        for name, rows, original in datasets:
            if rows:
                fields = list(rows[0])
            else:
                fields = list(original)

                if name == "recent_sessions":
                    fields.extend([
                        prefix + suffix
                        for prefix in [
                            "recent_energy_kwh",
                            "reported_duration_hours",
                        ]
                        for suffix in [
                            "_min",
                            "_max",
                            "_boundary",
                            "_state",
                        ]
                    ])
                    fields.append("recent_start_hour")

            write_csv(
                output / f"{name}_features.csv",
                rows,
                fields,
            )

        report = {
            "stage": "survey_feature_encoding",
            "rules_version": "1.0",
            "data_origin": "SYNTHETIC",
            "input_folder": str(source.relative_to(ROOT)),
            "counts": counts,
            "source_sha256": previous["source_sha256"],
            "input_table_sha256": input_hashes,
            "inherited_quality_counts":
                previous["quality_counts"],
            "simulation_ready": False,
            "full_option_validation_completed": False,
            "notes": [
                "Original answer and provenance columns retained. No midpoint imputation.",
                "Missing bounds are empty CSV cells, not zero.",
                "Integer-labelled continuous bins use the next integer as an exclusive edge; energy bins use the next tenth. This is an encoding convention.",
                "Weekly count ranges retain inclusive integer endpoints. Month and week frequency units remain separate.",
                "Overlapping duration/power answer ranges remain approximate; no exact measured value is inferred.",
                "Public charger code does not establish power. No public kW defaults applied.",
                "A reported charging start window is not shifted again for TOU.",
                "Only encoded answer fields are option-validated here; other categorical labels remain original.",
                "Interviews, app logs, parameter distributions and scenario assumptions are still separate work.",
            ],
        }

        with (output / "feature_report.json").open(
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

        print("Survey feature encoding complete.")

        for name, count in counts.items():
            print(f"{name}: {count}")

        print("Output:", output.relative_to(ROOT))
        print(
            "Bounds preserved; unknowns remain empty; "
            "no simulator assumptions fitted."
        )

        return 0

    except (OSError, ValueError, KeyError) as error:
        print(
            "Feature encoding stopped:",
            error,
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())