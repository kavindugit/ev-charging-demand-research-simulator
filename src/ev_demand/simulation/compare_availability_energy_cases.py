"""Compare three energy assumptions using completed, hashed power-pair audits."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from itertools import combinations
import json
import math
from pathlib import Path
from statistics import mean, stdev
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from ev_demand.parameters import build_simulator_parameters as baseline
from ev_demand.parameters.build_population_evidence import CONTEXTS, require
from ev_demand.simulation.run_session_simulator import (
    DRAW_FIELDS,
    SESSION_FIELDS,
    close,
    write_csv,
)

GROUPS = (*CONTEXTS, "TOTAL")
GENERATORS = ("poisson_control", "availability_aware")

FILES = {
    "sessions.csv",
    "control_rejected_sessions.csv",
    "selected_owner_parameters.csv",
    "owner_calibration.csv",
    "owner_generator_diagnostics.csv",
    "replicate_comparison.csv",
    "replicate_profiles.csv",
    "mean_profile_24h.csv",
    "generator_summary.csv",
}

CASES = {
    "uniform": "bounded_uniform_test",
    "gamma": "conditional_gamma_test",
    "lognormal": "conditional_lognormal_test",
}

POWERS = {
    "reduced": "reduced_test",
    "rated": "rated_ceiling_test",
}

EXPORTS = {
    "paired_replicate_metrics.csv",
    "paired_hourly_profiles.csv",
    "context_power_summary.csv",
    "mean_profile_comparison.csv",
    "owner_calibration_comparison.csv",
}

ENERGY_FIELDS_ALLOWED_TO_DIFFER = {
    "energy_case",
    "usual_energy_kwh",
    "exceeds_reported_battery_upper",
    "expected_weekly_charging_hours",
}


def paired_stats(values):
    require(
        len(values) >= 2 and all(math.isfinite(v) for v in values),
        "Invalid replicate values.",
    )
    return mean(values), stdev(values) / math.sqrt(len(values))


def indexed(rows, fields, expected=None):
    result = {
        tuple(row[k] for k in fields): row
        for row in rows
    }
    require(
        len(result) == len(rows)
        and (expected is None or set(result) == expected),
        "Duplicate or incomplete grid: " + ", ".join(fields),
    )
    return result


def check_event(first, second, control):
    if control:
        fields = set(SESSION_FIELDS) - {
            "session_energy_kwh",
            "active_duration_hours",
            "end_hour_absolute",
        }
    else:
        fields = {
            "session_id",
            "owner_id",
            "data_origin",
            "context",
            "charger_code",
            *DRAW_FIELDS,
        }

    require(
        all(first[k] == second[k] for k in fields),
        "Energy-case event pairing differs: " + first["session_id"],
    )


def compare(uniform, gamma, lognormal, project_root=ROOT):
    root = Path(project_root).resolve()
    watched = {}

    def watch(reference, expected=None):
        path = (root / reference).resolve()
        require(
            path.is_relative_to(root) and path.is_file(),
            "Missing project input: " + str(reference),
        )

        digest = watched.get(path)
        if digest is None:
            digest = baseline.sha256(path)
            watched[path] = digest

        require(
            expected is None or digest == expected,
            "Input hash mismatch: " + str(reference),
        )
        return path

    def read_json(path):
        return json.loads(
            watch(path).read_text(encoding="utf-8-sig")
        )

    def audit_report(reference, label):
        folder = (root / reference).resolve()
        require(
            folder.is_relative_to(root / "data/processed/synthetic"),
            "Use synthetic comparison folders.",
        )

        report = read_json(
            folder / "availability_power_comparison_report.json"
        )

        require(
            report.get("schema_version")
            == "paired_availability_power_comparison_v1"
            and report.get("status") == "complete"
            and report.get("data_origin") == "SYNTHETIC"
            and report.get("output_origin") == "SIMULATED"
            and report.get("behaviour_input_origin") == "SYNTHETIC"
            and report.get("energy_case") == CASES[label]
            and report.get("cohort") == "common_energy_cases"
            and report.get("checks")
            and all(v is True for v in report["checks"].values()),
            "Unexpected completed power comparison for " + label,
        )

        require(
            set(report["output_sha256"]) == EXPORTS,
            "Unexpected power-comparison exports.",
        )

        for name, digest in report["output_sha256"].items():
            watch(folder / name, digest)

        for name, digest in report["input_sha256"].items():
            watch(name, digest)

        return folder, report

    audits = {
        label: audit_report(path, label)
        for label, path in zip(CASES, (uniform, gamma, lognormal))
    }

    pilots = {}
    reference = None

    for label, (folder, audit) in audits.items():
        for power, scenario in POWERS.items():
            source = (root / audit[power + "_pilot"]).resolve()
            require(
                source.is_relative_to(root / "data/simulated/synthetic"),
                "Unexpected source pilot folder.",
            )

            report_path = source / "availability_generator_report.json"
            relative = report_path.relative_to(root).as_posix()

            require(
                relative in audit["input_sha256"],
                "Source report is absent from completed audit.",
            )

            report = read_json(
                watch(report_path, audit["input_sha256"][relative])
            )

            require(
                report.get("schema_version")
                == "availability_generator_comparison_v1"
                and report.get("status") == "complete"
                and report.get("data_origin") == "SYNTHETIC"
                and report.get("output_origin") == "SIMULATED"
                and report.get("behaviour_input_origin") == "SYNTHETIC"
                and report.get("energy_case") == CASES[label]
                and report.get("power_scenario") == scenario
                and report.get("checks")
                and all(v is True for v in report["checks"].values()),
                "Unexpected source pilot for " + label + "/" + power,
            )

            require(
                set(report["output_sha256"]) == FILES,
                "Unexpected pilot export manifest.",
            )

            for name, digest in report["output_sha256"].items():
                path = source / name
                require(
                    audit["input_sha256"].get(
                        path.relative_to(root).as_posix()
                    ) == digest,
                    "Pilot export differs from its completed audit.",
                )
                watch(path, digest)

            for name, digest in report["input_sha256"].items():
                require(
                    audit["input_sha256"].get(name) == digest,
                    "Pilot input differs from its completed audit.",
                )
                watch(name, digest)

            for field in (
                "replicates",
                "owners_simulated",
                "cohort",
                "energy_case",
                "study_days",
                "warmup_days",
            ):
                require(
                    report[field] == audit[field],
                    "Pilot/audit settings differ in " + field,
                )

            if reference is None:
                reference = report

            for field in (
                "parameters_folder",
                "owner_ids",
                "owners_simulated",
                "cohort",
                "study_days",
                "warmup_days",
                "replicates",
                "base_seed",
                "calibration_events_per_owner_selection",
                "verification_events_per_owner_selection",
                "calibration_proxy_relative_tolerance",
                "verification_development_band_percent",
                "policy",
                "input_sha256",
            ):
                require(
                    report[field] == reference[field],
                    "Energy experiments differ in " + field,
                )

            require(
                report["cohort"] == "common_energy_cases",
                "Keep the open-upper extension separate.",
            )

            pilots[(label, power)] = {
                "folder": source,
                "report": report,
            }

    n = reference["replicates"]
    days = reference["study_days"]
    ids = reference["owner_ids"]

    require(
        type(n) is int
        and n >= 2
        and type(days) is int
        and days > 0
        and ids
        and len(ids) == len(set(ids)) == reference["owners_simulated"],
        "Invalid source experiment grid.",
    )

    owner_grid = {
        (str(r), oid)
        for r in range(1, n + 1)
        for oid in ids
    }
    daily_grid = {
        (str(r), c)
        for r in range(1, n + 1)
        for c in GROUPS
    }
    profile_grid = {
        (*key, str(h))
        for key in daily_grid
        for h in range(24)
    }

    bank_folder = root / reference["parameters_folder"]
    bank = read_json(
        bank_folder / "window_screen_sensitivity.json"
    )

    require(
        bank["schema_version"] == "paired_window_screen_sensitivity_v1"
        and bank["status"] == "screening_sensitivity_prepared",
        "Unexpected source parameter bank.",
    )

    population = read_json(
        root
        / bank["source_population_folder"]
        / "behaviour_population_model.json"
    )

    require(
        ids == sorted(
            population["comparison_cohort_ids"]["all_energy_cases_common"]
        ),
        "The full common-owner cohort must be retained.",
    )

    owner_screens = [
        row
        for row in bank["owner_screens"]
        if row["cohort"] == "common_energy_cases"
    ]

    banks = {
        row["owner_id"]: json.loads(
            row["admitted_parameter_draws_json"]
        )
        for row in owner_screens
    }

    require(
        len(owner_screens) == len(banks) == len(ids)
        and set(banks) == set(ids)
        and all(
            values and values == sorted(set(values))
            for values in banks.values()
        ),
        "An owner has no admitted draw bank.",
    )

    original = indexed(
        baseline.read_csv(bank_folder / "screened_parameter_draws.csv"),
        ("owner_id", "energy_case", "parameter_draw"),
    )

    for (label, power), pilot in pilots.items():
        folder = pilot["folder"]
        report = pilot["report"]

        pilot["selected"] = indexed(
            baseline.read_csv(folder / "selected_owner_parameters.csv"),
            ("replicate", "owner_id"),
            owner_grid,
        )

        pilot["calibration"] = indexed(
            baseline.read_csv(folder / "owner_calibration.csv"),
            ("replicate", "owner_id"),
            owner_grid,
        )

        statuses = Counter(
            row["calibration_status"]
            for row in pilot["calibration"].values()
        )

        flagged = sum(
            abs(float(row["verification_relative_error_percent"]))
            > report["verification_development_band_percent"]
            for row in pilot["calibration"].values()
        )

        require(
            dict(statuses) == report["calibration_status_counts"]
            and flagged == report["verification_selections_outside_band"],
            "Calibration counts disagree.",
        )

        for (_, oid), row in pilot["selected"].items():
            key = (oid, CASES[label], row["parameter_draw"])

            require(
                int(row["parameter_draw"]) in banks[oid]
                and key in original
                and all(
                    row[k] == value
                    for k, value in original[key].items()
                ),
                "Selected row differs from the admitted source draw.",
            )

            require(
                close(
                    float(row["within_owner_draw_probability"]),
                    1 / len(banks[oid]),
                ),
                "Owner weighting differs from uniform within-owner sampling.",
            )

        pilot["daily"] = indexed(
            baseline.read_csv(folder / "replicate_comparison.csv"),
            ("replicate", "context"),
            daily_grid,
        )

        pilot["profiles"] = indexed(
            baseline.read_csv(folder / "replicate_profiles.csv"),
            ("replicate", "context", "hour_of_day"),
            profile_grid,
        )

        sessions = baseline.read_csv(folder / "sessions.csv")

        require(
            all(row["generator"] in GENERATORS for row in sessions),
            "Unexpected session generator.",
        )

        pilot["events"] = {
            generator: indexed(
                [
                    row
                    for row in sessions
                    if row["generator"] == generator
                ],
                ("session_id",),
            )
            for generator in GENERATORS
        }

        rejected = baseline.read_csv(
            folder / "control_rejected_sessions.csv"
        )

        require(
            all(
                row["generator"] == "poisson_control"
                for row in rejected
            ),
            "Unexpected rejected generator.",
        )

        pilot["control"] = indexed(
            [
                row
                for row in sessions
                if row["generator"] == "poisson_control"
            ] + rejected,
            ("session_id",),
        )

    owner_rows = []
    pairing = {}

    for power in POWERS:
        group = [pilots[(label, power)] for label in CASES]
        first = group[0]

        for key in sorted(owner_grid):
            a = first["selected"][key]

            out = {
                "power_case": POWERS[power],
                "replicate": int(key[0]),
                "owner_id": key[1],
                "parameter_draw": a["parameter_draw"],
                "total_weekly_rate": a["total_weekly_rate"],
                "home_power_proxy_kw": a["home_power_proxy_kw"],
                "within_owner_draw_probability":
                    a["within_owner_draw_probability"],
            }

            for label, pilot in zip(CASES, group):
                row = pilot["selected"][key]
                cal = pilot["calibration"][key]

                require(
                    row.keys() == a.keys()
                    and all(
                        row[k] == a[k]
                        for k in a
                        if k not in ENERGY_FIELDS_ALLOWED_TO_DIFFER
                    ),
                    "Owner frequency/power/index pairing differs.",
                )

                for field in (
                    "usual_energy_kwh",
                    "expected_weekly_charging_hours",
                    "exceeds_reported_battery_upper",
                ):
                    out[label + "_" + field] = row[field]

                for field in (
                    "idle_mean_hours",
                    "calibration_status",
                    "verification_relative_error_percent",
                ):
                    out[label + "_" + field] = cal[field]

            owner_rows.append(out)

        controls = [pilot["control"] for pilot in group]

        require(
            all(
                rows.keys() == controls[0].keys()
                for rows in controls[1:]
            ),
            "Control candidate IDs differ across energy cases.",
        )

        for key, event in controls[0].items():
            for rows in controls[1:]:
                check_event(event, rows[key], True)

        availability = [
            pilot["events"]["availability_aware"]
            for pilot in group
        ]

        common = set.intersection(
            *(set(rows) for rows in availability)
        )

        for key in common:
            for rows in availability[1:]:
                check_event(availability[0][key], rows[key], False)

        pairing[power] = {
            "owner_draw_triplets": len(owner_grid),
            "control_candidates": len(controls[0]),
            "common_availability_serial_events": len(common),
        }

    metrics = []
    profiles = []

    for label, (folder, audit) in audits.items():
        rows = baseline.read_csv(
            folder / "paired_replicate_metrics.csv"
        )

        grid = {
            (str(r), generator, context)
            for r in range(1, n + 1)
            for generator in GENERATORS
            for context in GROUPS
        }

        table = indexed(
            rows,
            ("replicate", "generator", "context"),
            grid,
        )

        for power in POWERS:
            pilot = pilots[(label, power)]

            for (rep, generator, context), row in table.items():
                field = (
                    "control"
                    if generator == "poisson_control"
                    else "availability"
                )

                energy = float(row[power + "_daily_energy_kwh"])

                require(
                    close(
                        energy,
                        float(
                            pilot["daily"][(rep, context)][
                                field + "_daily_energy_kwh"
                            ]
                        ),
                    ),
                    "Audited replicate energy differs from its pilot.",
                )

                points = [
                    float(
                        pilot["profiles"][(rep, context, str(h))][
                            field + "_mean_power_kw"
                        ]
                    )
                    for h in range(24)
                ]

                require(
                    all(math.isfinite(v) and v >= 0 for v in points)
                    and close(math.fsum(points), energy),
                    "Hourly profile does not conserve replicate daily energy.",
                )

                metrics.append({
                    "energy_case": CASES[label],
                    "power_case": POWERS[power],
                    "generator": generator,
                    "replicate": int(rep),
                    "context": context,
                    "target_expected_study_events":
                        float(row["target_expected_study_events"]),
                    "study_starts": int(row[power + "_starts"]),
                    "study_completions":
                        int(row[power + "_completions"]),
                    "daily_energy_kwh": energy,
                })

                profiles.extend({
                    "energy_case": CASES[label],
                    "power_case": POWERS[power],
                    "generator": generator,
                    "replicate": int(rep),
                    "context": context,
                    "hour_of_day": h,
                    "mean_power_kw": value,
                } for h, value in enumerate(points))

    metric_index = indexed(
        metrics,
        (
            "energy_case",
            "power_case",
            "generator",
            "context",
            "replicate",
        ),
    )

    profile_index = indexed(
        profiles,
        (
            "energy_case",
            "power_case",
            "generator",
            "context",
            "replicate",
            "hour_of_day",
        ),
    )

    case_summary = []
    contrasts = []
    profile_contrasts = []
    peaks = []

    for power in POWERS.values():
        for generator in GENERATORS:
            for context in GROUPS:
                for case in CASES.values():
                    rows = [
                        metric_index[
                            (case, power, generator, context, r)
                        ]
                        for r in range(1, n + 1)
                    ]

                    average, error = paired_stats([
                        row["daily_energy_kwh"]
                        for row in rows
                    ])

                    target = math.fsum(
                        row["target_expected_study_events"]
                        for row in rows
                    )

                    case_summary.append({
                        "energy_case": case,
                        "power_case": power,
                        "generator": generator,
                        "context": context,
                        "replicates": n,
                        "mean_daily_energy_kwh": average,
                        "daily_energy_mcse_kwh": error,
                        "pooled_target_events": target,
                        "completions_to_target": (
                            sum(
                                row["study_completions"]
                                for row in rows
                            ) / target
                            if target else None
                        ),
                    })

                    if context == "TOTAL":
                        values = [
                            mean(
                                profile_index[
                                    (
                                        case,
                                        power,
                                        generator,
                                        context,
                                        r,
                                        h,
                                    )
                                ]["mean_power_kw"]
                                for r in range(1, n + 1)
                            )
                            for h in range(24)
                        ]

                        peak = max(values)

                        peaks.append({
                            "energy_case": case,
                            "power_case": power,
                            "generator": generator,
                            "peak_of_mean_profile_kw": peak,
                            "peak_hours": [
                                h
                                for h, value in enumerate(values)
                                if close(value, peak)
                            ],
                        })

                for first, second in combinations(CASES.values(), 2):
                    pairs = [
                        (
                            metric_index[
                                (first, power, generator, context, r)
                            ],
                            metric_index[
                                (second, power, generator, context, r)
                            ],
                        )
                        for r in range(1, n + 1)
                    ]

                    require(
                        all(
                            close(
                                a["target_expected_study_events"],
                                b["target_expected_study_events"],
                            )
                            for a, b in pairs
                        ),
                        "Paired targets differ across energy cases.",
                    )

                    delta, error = paired_stats([
                        b["daily_energy_kwh"] - a["daily_energy_kwh"]
                        for a, b in pairs
                    ])

                    count_delta, count_error = paired_stats([
                        b["study_completions"] - a["study_completions"]
                        for a, b in pairs
                    ])

                    contrasts.append({
                        "reference_energy_case": first,
                        "comparison_energy_case": second,
                        "power_case": power,
                        "generator": generator,
                        "context": context,
                        "replicates": n,
                        "mean_daily_energy_difference_kwh": delta,
                        "paired_daily_energy_mcse_kwh": error,
                        "mean_completion_difference": count_delta,
                        "paired_completion_mcse": count_error,
                    })

                    hourly = []

                    for hour in range(24):
                        values = [
                            profile_index[
                                (
                                    second,
                                    power,
                                    generator,
                                    context,
                                    r,
                                    hour,
                                )
                            ]["mean_power_kw"]
                            - profile_index[
                                (
                                    first,
                                    power,
                                    generator,
                                    context,
                                    r,
                                    hour,
                                )
                            ]["mean_power_kw"]
                            for r in range(1, n + 1)
                        ]

                        effect, mcse = paired_stats(values)
                        hourly.append(effect)

                        profile_contrasts.append({
                            "reference_energy_case": first,
                            "comparison_energy_case": second,
                            "power_case": power,
                            "generator": generator,
                            "context": context,
                            "hour_of_day": hour,
                            "mean_difference_kw": effect,
                            "paired_difference_mcse_kw": mcse,
                        })

                    require(
                        close(math.fsum(hourly), delta),
                        "Mean contrast profile does not conserve energy.",
                    )

    require(
        all(
            baseline.sha256(path) == digest
            for path, digest in watched.items()
        ),
        "An input changed during comparison.",
    )

    watch(
        "src/ev_demand/simulation/compare_availability_energy_cases.py"
    )

    output = (
        root
        / "data/processed/synthetic"
        / (
            "availability_energy_comparison_"
            + datetime.now(timezone.utc).strftime(
                "%Y%m%dT%H%M%S_%fZ"
            )
        )
    )
    output.mkdir(parents=True, exist_ok=False)

    for name, rows in (
        ("energy_case_summary.csv", case_summary),
        ("paired_energy_contrasts.csv", contrasts),
        ("paired_hourly_energy_contrasts.csv", profile_contrasts),
        ("owner_energy_comparison.csv", owner_rows),
        ("replicate_energy_cases.csv", metrics),
        ("ensemble_mean_peaks.csv", peaks),
    ):
        write_csv(output / name, rows, tuple(rows[0]))

    require(
        all(
            baseline.sha256(path) == digest
            for path, digest in watched.items()
        ),
        "An input changed during export.",
    )

    result = {
        "schema_version": "paired_availability_energy_comparison_v1",
        "status": "complete",
        "data_origin": "SYNTHETIC",
        "behaviour_input_origin": "SYNTHETIC",
        "output_origin": "SIMULATED",
        "source_power_comparisons": {
            label: folder.relative_to(root).as_posix()
            for label, (folder, _) in audits.items()
        },
        "source_parameter_bank": reference["parameters_folder"],
        "owners_simulated": len(ids),
        "cohort": "common_energy_cases",
        "replicates": n,
        "study_days": days,
        "warmup_days": reference["warmup_days"],
        "base_seed": reference["base_seed"],
        "pairing_by_power_case": pairing,
        "energy_case_summary": case_summary,
        "paired_contrasts": contrasts,
        "calibration_status_counts": {
            label + "/" + power:
                pilot["report"]["calibration_status_counts"]
            for (label, power), pilot in pilots.items()
        },
        "verification_selections_outside_band": {
            label + "/" + power:
                pilot["report"]["verification_selections_outside_band"]
            for (label, power), pilot in pilots.items()
        },
        "availability_candidate_pairing": False,
        "new_random_draws": 0,
        "new_demand_simulations": 0,
        "model_selected_automatically": False,
        "empirical_validation": False,
        "policy": [
            "Differences are comparison minus reference, keeping each "
            "power case and generator separate.",

            "Completed power-pair audits and every source/output hash "
            "are rechecked. Their unchanged session audits are reused; "
            "no scheduler is replayed.",

            "Owner IDs, admitted draw indices, rates, home power and "
            "within-owner weights match. Energy and derived "
            "occupation/idle values may differ.",

            "Poisson proposals match including rejected and warm-up "
            "events. Availability common serial marks match; their "
            "calendar starts and counts may change.",

            "Availability idle parameters are recalibrated for each "
            "energy case. Effects include that policy response.",

            "The parameter bank was screened across all energy/power "
            "cases. Results condition on that shared screening, "
            "finite bank and common completeness cohort.",

            "MCSE uses whole-cohort paired replicate differences. "
            "Fixed fitted-distribution and input/model uncertainty "
            "are not included.",

            "All calibration review selections remain included. "
            "The open-upper extension is outside this common-cohort "
            "comparison.",

            "Near-target pooled frequency does not establish "
            "owner/context agreement, stationarity or empirical "
            "accuracy. Profile peak is peak of the ensemble mean.",
        ],
        "checks": {
            "completed_power_audit_hashes_rechecked": True,
            "all_six_pilot_hashes_rechecked": True,
            "full_common_owner_grids_checked": True,
            "source_draws_and_owner_weights_checked": True,
            "cross_energy_control_proposals_paired": True,
            "availability_common_serial_marks_checked": True,
            "replicate_profiles_and_daily_energy_checked": True,
            "paired_mean_profiles_conserve_energy": True,
            "input_files_unchanged": True,
        },
        "input_sha256": {
            path.relative_to(root).as_posix(): digest
            for path, digest in watched.items()
        },
        "output_sha256": {
            path.name: baseline.sha256(path)
            for path in sorted(output.iterdir())
        },
    }

    (
        output / "availability_energy_comparison_report.json"
    ).write_text(
        json.dumps(
            result,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        ) + "\n",
        encoding="utf-8",
    )

    return output, result


def main():
    parser = argparse.ArgumentParser(description=__doc__)

    for label in CASES:
        parser.add_argument(
            "--" + label,
            required=True,
            help="Completed power-comparison folder for " + label,
        )

    args = parser.parse_args()

    output, report = compare(
        args.uniform,
        args.gamma,
        args.lognormal,
    )

    print("Paired availability energy-assumption comparison complete.")

    for key in (
        "owners_simulated",
        "replicates",
        "study_days",
        "warmup_days",
        "pairing_by_power_case",
    ):
        print(f"{key}: {report[key]}")

    for row in report["paired_contrasts"]:
        if row["context"] == "TOTAL":
            print(
                f"{row['power_case']} | {row['generator']} | "
                f"{row['comparison_energy_case']} minus "
                f"{row['reference_energy_case']}: "
                f"daily_difference="
                f"{row['mean_daily_energy_difference_kwh']:.6f} "
                f"kWh/day, paired_mcse="
                f"{row['paired_daily_energy_mcse_kwh']:.6f}"
            )

    print(
        "Source hashes, common-owner selections, event pairing "
        "and profile-energy checks passed."
    )
    print(f"Output: {output.relative_to(ROOT)}")
    print(
        "Conditional energy/policy sensitivity only; "
        "no new simulations or empirical validation."
    )


if __name__ == "__main__":
    try:
        main()
    except (
        ValueError,
        OSError,
        KeyError,
        TypeError,
        RuntimeError,
    ) as exc:
        print(
            f"Availability-energy comparison failed: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1)