"""Run a small Poisson/overlap control pilot using screened owner parameters."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from random import Random
from statistics import mean, stdev
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from ev_demand.parameters import build_simulator_parameters as baseline
from ev_demand.parameters.build_population_evidence import CONTEXTS, require
from ev_demand.parameters.assess_owner_time_budget import (
    inverse_power_bounds, weekly_time_bounds,
)
from ev_demand.parameters.build_public_power_layer import sample_session_power
from ev_demand.simulation.run_session_simulator import (
    SESSION_FIELDS, ENERGY_FIELDS, allocate_energy, close,
    poisson_count, schedule_owner, write_csv,
)

MODEL_FILES = {
    "population_owner_case_audit.csv",
    "population_context_audit.csv",
    "population_case_summary.csv",
    "joint_owner_parameter_test_draws.csv",
}
SCREEN_FILES = {
    "time_screen_block_audit.csv",
    "time_screen_owner_audit.csv",
    "time_screen_group_summary.csv",
    "parameter_distribution_changes.csv",
    "screened_parameter_draws.csv",
}
CASES = (
    "bounded_uniform_test",
    "conditional_gamma_test",
    "conditional_lognormal_test",
)
GROUPS = (*CONTEXTS, "TOTAL")


def stream_seed(seed, replicate, owner_id, stream):
    return int.from_bytes(
        hashlib.sha256(
            f"joint-pilot-v1|{seed}|{replicate}|{owner_id}|{stream}".encode()
        ).digest()[:8],
        "big",
    )


def simulate_selection(selection, days, warmup, seed, replicate):
    horizon = 24 * days
    hourly = {c: [0.0] * horizon for c in CONTEXTS}
    accepted, rejected, diagnostics = [], [], []

    for owner in selection:
        oid = owner["owner_id"]
        rates = owner["rates"]
        energy = owner["energy"]

        rng = Random(stream_seed(seed, replicate, oid, "proposals"))
        power_rng = Random(stream_seed(seed, replicate, oid, "power"))
        codes = [c for c in CONTEXTS if rates[c] > 0]
        candidates = []

        for day in range(-warmup, days):
            for _ in range(poisson_count(owner["total"] / 7, rng)):
                code = rng.choices(
                    codes, weights=[rates[c] for c in codes], k=1
                )[0]
                context = owner["contexts"][code]
                window = context["timing_rule"]
                clock = (
                    window["start_hour"]
                    + rng.random() * window["length_hours"]
                ) % 24

                power = sample_session_power(
                    {"power_model": context["power_model"]},
                    power_rng,
                )
                duration = energy / power["effective_average_power_kw"]
                start = day * 24 + clock

                require(
                    math.isfinite(duration) and duration > 0
                    and (clock - window["start_hour"]) % 24
                    < window["length_hours"] + 1e-9,
                    "Invalid duration or start-window draw.",
                )

                candidates.append(dict(
                    session_id=(
                        f"JP_{replicate:03d}_{oid}_"
                        f"{len(candidates) + 1:06d}"
                    ),
                    owner_id=oid,
                    data_origin="SYNTHETIC",
                    context=code,
                    charger_code=context["charger_code"],
                    start_phase="warmup" if day < 0 else "study",
                    start_day_index=day,
                    start_hour_of_day=clock,
                    start_hour_absolute=start,
                    end_hour_absolute=start + duration,
                    active_duration_hours=duration,
                    session_energy_kwh=energy,
                    **power,
                ))

        kept, dropped = schedule_owner(candidates)
        require(
            len(kept) + len(dropped) == len(candidates)
            and all(
                kept[i]["start_hour_absolute"]
                >= kept[i - 1]["end_hour_absolute"]
                for i in range(1, len(kept))
            ),
            "Candidate accounting or no-overlap check failed.",
        )

        for row in kept:
            row.update(allocate_energy(row, horizon, hourly))

        accepted.extend(
            dict(
                replicate=replicate,
                output_origin="SIMULATED",
                behaviour_input_origin="SYNTHETIC",
                **row,
            )
            for row in kept
        )
        rejected.extend(
            dict(
                replicate=replicate,
                output_origin="SIMULATED",
                behaviour_input_origin="SYNTHETIC",
                **row,
            )
            for row in dropped
        )

        kept_counts = Counter(
            row["context"] for row in kept
            if row["start_phase"] == "study"
        )
        dropped_counts = Counter(
            row["context"] for row in dropped
            if row["start_phase"] == "study"
        )

        for code in GROUPS:
            rate = owner["total"] if code == "TOTAL" else rates[code]
            k = (
                sum(kept_counts.values())
                if code == "TOTAL" else kept_counts[code]
            )
            d = (
                sum(dropped_counts.values())
                if code == "TOTAL" else dropped_counts[code]
            )
            diagnostics.append(dict(
                replicate=replicate,
                owner_id=oid,
                context=code,
                target_weekly_completed_rate=rate,
                proposal_weekly_rate=rate,
                target_expected_study_starts=rate * days / 7,
                study_candidate_starts=k + d,
                study_accepted_starts=k,
                study_rejected_starts=d,
                realised_weekly_candidates=(k + d) * 7 / days,
                realised_weekly_accepted=k * 7 / days,
            ))

    hourly_energy = math.fsum(
        value for series in hourly.values() for value in series
    )
    inside = math.fsum(r["energy_in_study_kwh"] for r in accepted)
    full = math.fsum(r["session_energy_kwh"] for r in accepted)
    partition = math.fsum(
        row[key] for row in accepted for key in ENERGY_FIELDS
    )
    require(
        close(hourly_energy, inside) and close(full, partition),
        "Pilot energy balance failed.",
    )

    profiles = []

    for code in GROUPS:
        series = (
            [
                math.fsum(hourly[c][h] for c in CONTEXTS)
                for h in range(horizon)
            ]
            if code == "TOTAL" else hourly[code]
        )
        profiles.extend(
            dict(
                replicate=replicate,
                context=code,
                hour_of_day=hour,
                output_origin="SIMULATED",
                behaviour_input_origin="SYNTHETIC",
                mean_power_kw=math.fsum(series[hour::24]) / days,
            )
            for hour in range(24)
        )

    require(
        close(
            math.fsum(
                r["mean_power_kw"] for r in profiles
                if r["context"] == "TOTAL"
            ),
            hourly_energy / days,
        ),
        "Replicate mean profile does not conserve daily energy.",
    )

    total_rows = [
        r for r in diagnostics if r["context"] == "TOTAL"
    ]
    summary = dict(
        replicate=replicate,
        owners_simulated=len(selection),
        study_candidate_starts=sum(
            r["study_candidate_starts"] for r in total_rows
        ),
        study_accepted_starts=sum(
            r["study_accepted_starts"] for r in total_rows
        ),
        study_rejected_starts=sum(
            r["study_rejected_starts"] for r in total_rows
        ),
        target_expected_study_starts=math.fsum(
            r["target_expected_study_starts"] for r in total_rows
        ),
        energy_in_study_kwh=hourly_energy,
        mean_daily_energy_kwh=hourly_energy / days,
        no_owner_overlaps=True,
        energy_conserved=True,
    )
    return accepted, rejected, diagnostics, profiles, summary


def run_pilot(
    model_folder,
    screen_folder,
    energy_case="bounded_uniform_test",
    power_scenario="reduced_test",
    cohort="common_energy_cases",
    days=28,
    warmup=7,
    replicates=10,
    base_seed=20261007,
    project_root=ROOT,
):
    require(
        type(days) is int and 1 <= days <= 56
        and type(warmup) is int and 0 <= warmup <= 60,
        "Use 1-56 study days and 0-60 warm-up days.",
    )
    require(
        type(replicates) is int and 2 <= replicates <= 20
        and type(base_seed) is int and base_seed >= 0,
        "Use 2-20 pilot replicates and a nonnegative base seed.",
    )
    require(
        cohort in {"common_energy_cases", "open_upper_extension"}
        and energy_case in CASES
        and (
            cohort != "open_upper_extension"
            or energy_case != CASES[0]
        ),
        "Invalid cohort/energy case.",
    )

    root, watched = Path(project_root).resolve(), {}
    allowed = (root / "data/processed/synthetic").resolve()

    def watch(value):
        path = (root / value).resolve()
        require(
            path.is_relative_to(root) and path.is_file(),
            "Missing project input: " + str(value),
        )
        digest = baseline.sha256(path)
        require(
            path not in watched or watched[path] == digest,
            "Input changed while being read.",
        )
        watched[path] = digest
        return path

    def package(value, report_name, schema, status, files):
        folder = (root / value).resolve()
        require(
            folder.is_relative_to(allowed) and folder.is_dir(),
            "Use completed synthetic input folders.",
        )
        report = json.loads(
            watch(folder / report_name).read_text(
                encoding="utf-8-sig"
            )
        )
        require(
            report.get("schema_version") == schema
            and report.get("status") == status
            and report.get("data_origin") == "SYNTHETIC",
            "Unexpected input: " + report_name,
        )
        require(
            bool(report.get("checks"))
            and all(v is True for v in report["checks"].values()),
            "An upstream check failed: " + report_name,
        )
        require(
            set(report["output_sha256"]) == files,
            "Unexpected export manifest: " + report_name,
        )
        for name in sorted(files):
            path = watch(folder / name)
            require(
                watched[path] == report["output_sha256"][name],
                "Export hash mismatch: " + name,
            )
        return folder, report

    for name in (
        "docs/research_pipeline_specification.md",
        "src/ev_demand/parameters/build_simulator_parameters.py",
        "src/ev_demand/parameters/build_population_evidence.py",
        "src/ev_demand/parameters/assess_owner_time_budget.py",
        "src/ev_demand/parameters/build_public_power_layer.py",
        "src/ev_demand/simulation/run_session_simulator.py",
        "src/ev_demand/simulation/run_joint_parameter_pilot.py",
    ):
        watch(name)

    model, population = package(
        model_folder,
        "behaviour_population_model.json",
        "behaviour_population_model_v1",
        "components_assembled_for_review",
        MODEL_FILES,
    )
    screen, screened = package(
        screen_folder,
        "time_screen_sensitivity.json",
        "paired_time_screen_sensitivity_v1",
        "screening_sensitivity_prepared",
        SCREEN_FILES,
    )

    for name in MODEL_FILES | {"behaviour_population_model.json"}:
        path = model / name
        require(
            screened["input_sha256"].get(
                path.relative_to(root).as_posix()
            ) == watched[path],
            "Screen uses a different population snapshot: " + name,
        )

    require(
        power_scenario in population["power_scenarios"],
        "Unknown power scenario.",
    )
    key = (
        "all_energy_cases_common"
        if cohort == "common_energy_cases"
        else "conditional_open_upper_extension"
    )
    ids = sorted(population["comparison_cohort_ids"][key])
    require(
        bool(ids) and len(ids) == len(set(ids)),
        "Selected cohort has no valid owner IDs.",
    )

    owners = {r["owner_id"]: r for r in population["owners"]}
    require(
        len(owners) == len(population["owners"])
        == population["owners_retained"],
        "Invalid population owner grid.",
    )

    records = [
        r for r in screened["owner_screens"]
        if r["cohort"] == cohort
    ]
    indices = {
        r["owner_id"]: json.loads(
            r["admitted_parameter_draws_json"]
        )
        for r in records
    }
    require(
        len(indices) == len(records) == len(ids)
        and set(indices) == set(ids)
        and all(
            values and values == sorted(set(values))
            for values in indices.values()
        ),
        "The full selected cohort requires admitted indices "
        "for every owner.",
    )

    rows = baseline.read_csv(
        screen / "screened_parameter_draws.csv"
    )
    indexed = {
        (
            r["owner_id"],
            r["energy_case"],
            int(r["parameter_draw"]),
        ): r
        for r in rows
    }
    require(
        len(indexed) == len(rows)
        == screened["retained_parameter_rows"],
        "Invalid screened parameter row grid.",
    )

    bank = {}

    for oid in ids:
        require(
            oid in owners
            and owners[oid]["case_component_status"][
                energy_case
            ]["components_available"],
            "Selected owner lacks required components.",
        )
        for index in indices[oid]:
            row = indexed[(oid, energy_case, index)]
            require(
                row["screen_cohort"] == cohort
                and math.isclose(
                    float(row["within_owner_draw_probability"]),
                    1 / len(indices[oid]),
                    abs_tol=1e-12, rel_tol=1e-10,
                ),
                "Within-owner index probabilities differ.",
            )
            bank[(oid, index)] = row

    threshold = (
        float(screened["threshold_hours"])
        - float(screened["numerical_tolerance_hours"])
    )
    require(
        0 < threshold <= 168,
        "Unexpected screen threshold.",
    )

    output = root / "data/simulated/synthetic" / (
        "joint_parameter_pilot_"
        + datetime.now(timezone.utc).strftime(
            "%Y%m%dT%H%M%S_%fZ"
        )
    )
    output.mkdir(parents=True, exist_ok=False)
    print(f"Pilot output: {output.relative_to(root)}", flush=True)

    accepted_all, rejected_all, diagnostics_all = [], [], []
    profiles_all, summaries, selected_rows = [], [], []

    for replicate in range(1, replicates + 1):
        require(
            all(
                baseline.sha256(path) == old
                for path, old in watched.items()
            ),
            "Input changed during pilot.",
        )

        selection = []

        for oid in ids:
            index = Random(
                stream_seed(
                    base_seed, replicate, oid, "parameters"
                )
            ).choice(indices[oid])

            row = bank[(oid, index)]
            energy = float(row["usual_energy_kwh"])
            total = float(row["total_weekly_rate"])
            rates = {
                c: float(row[c + "_weekly_rate"])
                for c in CONTEXTS
            }

            require(
                math.isfinite(energy) and energy > 0
                and math.isfinite(total) and total > 0
                and all(
                    math.isfinite(v) and v >= 0
                    for v in rates.values()
                )
                and close(math.fsum(rates.values()), total),
                "Selected energy/rate vector is invalid.",
            )

            home = (
                float(row["home_power_proxy_kw"])
                if row["home_power_proxy_kw"].strip()
                else None
            )

            contexts, inverses = {}, {}

            for context in owners[oid]["contexts"]:
                code = context["legacy_context"]

                if context["use_status"] == "reported_nonuse":
                    require(
                        rates[code] == 0,
                        "Reported nonuse has a positive selected rate.",
                    )
                    inverses[code] = (0.0, 0.0, 0.0)
                    continue

                require(
                    context["power_status"] == "supported_test"
                    and context["timing_rule"] is not None,
                    "Selected owner has an unsupported context.",
                )
                power_model = dict(
                    context["power_models"][power_scenario]
                )
                inverses[code] = inverse_power_bounds(
                    power_model, home
                )

                if code == "T2":
                    power_model["effective_average_kw_min"] = home
                    power_model["effective_average_kw_max"] = home

                contexts[code] = dict(
                    timing_rule=context["timing_rule"],
                    power_model=power_model,
                    charger_code=context[
                        "source_context"
                    ]["charger_code"],
                )

            (_, expected_hours, _), _ = weekly_time_bounds(
                energy, rates, inverses
            )
            require(
                expected_hours < threshold,
                "Selected vector fails its recorded time screen.",
            )

            selection.append(dict(
                owner_id=oid,
                rates=rates,
                total=total,
                energy=energy,
                contexts=contexts,
            ))
            selected_rows.append(dict(
                replicate=replicate,
                **row,
                expected_weekly_charging_hours=expected_hours,
                maximum_possible_session_hours=(
                    energy * max(
                        inverses[c][2]
                        for c in CONTEXTS
                        if rates[c] > 0
                    )
                ),
            ))

        kept, dropped, diagnostics, profiles, summary = (
            simulate_selection(
                selection, days, warmup,
                base_seed, replicate,
            )
        )

        accepted_all.extend(kept)
        rejected_all.extend(dropped)
        diagnostics_all.extend(diagnostics)
        profiles_all.extend(profiles)
        summaries.append(summary)

        print(
            f"Pilot replicate {replicate}/{replicates}: "
            "accounting=PASS",
            flush=True,
        )

    frequency_summary = []

    for code in GROUPS:
        records = [
            r for r in diagnostics_all
            if r["context"] == code
        ]
        target = math.fsum(
            r["target_expected_study_starts"] for r in records
        )
        kept = sum(r["study_accepted_starts"] for r in records)
        dropped = sum(r["study_rejected_starts"] for r in records)
        candidates = kept + dropped

        frequency_summary.append(dict(
            context=code,
            target_expected_study_starts=target,
            study_candidate_starts=candidates,
            study_accepted_starts=kept,
            study_rejected_starts=dropped,
            candidate_to_target_ratio=(
                candidates / target if target else None
            ),
            accepted_to_target_ratio=(
                kept / target if target else None
            ),
            rejection_percent=(
                100 * dropped / candidates
                if candidates else None
            ),
        ))

    mean_profile = []

    for code in GROUPS:
        for hour in range(24):
            values = [
                r["mean_power_kw"] for r in profiles_all
                if r["context"] == code
                and r["hour_of_day"] == hour
            ]
            require(
                len(values) == replicates,
                "Incomplete replicate profile grid.",
            )
            mean_profile.append(dict(
                context=code,
                hour_of_day=hour,
                n_replicates=replicates,
                output_origin="SIMULATED",
                behaviour_input_origin="SYNTHETIC",
                mean_power_kw=mean(values),
                mcse_kw=stdev(values) / math.sqrt(replicates),
            ))

    daily = mean(
        r["mean_daily_energy_kwh"] for r in summaries
    )
    require(
        close(
            math.fsum(
                r["mean_power_kw"] for r in mean_profile
                if r["context"] == "TOTAL"
            ),
            daily,
        ),
        "Ensemble mean profile does not conserve mean daily energy.",
    )

    for hour in range(24):
        values = {
            r["context"]: r["mean_power_kw"]
            for r in mean_profile
            if r["hour_of_day"] == hour
        }
        require(
            close(
                math.fsum(values[c] for c in CONTEXTS),
                values["TOTAL"],
            ),
            "Mean context profiles do not sum to TOTAL.",
        )

    write_csv(
        output / "sessions.csv",
        accepted_all,
        (
            "replicate", "output_origin", "behaviour_input_origin",
            *SESSION_FIELDS, *ENERGY_FIELDS,
        ),
    )
    write_csv(
        output / "rejected_sessions.csv",
        rejected_all,
        (
            "replicate", "output_origin", "behaviour_input_origin",
            *SESSION_FIELDS,
            "rejection_reason", "conflicting_session_id",
        ),
    )

    for name, data in (
        ("selected_owner_parameters.csv", selected_rows),
        ("owner_frequency_diagnostics.csv", diagnostics_all),
        ("replicate_summaries.csv", summaries),
        ("frequency_summary.csv", frequency_summary),
        ("replicate_profiles.csv", profiles_all),
        ("mean_profile_24h.csv", mean_profile),
    ):
        write_csv(output / name, data, tuple(data[0]))

    require(
        all(
            baseline.sha256(path) == old
            for path, old in watched.items()
        ),
        "Input changed during pilot export.",
    )

    report = dict(
        schema_version="joint_parameter_scheduling_pilot_v1",
        status="complete",
        data_origin="SYNTHETIC",
        behaviour_input_origin="SYNTHETIC",
        output_origin="SIMULATED",
        cohort=cohort,
        owners_simulated=len(ids),
        owner_ids=ids,
        energy_case=energy_case,
        power_scenario=power_scenario,
        study_days=days,
        warmup_days=warmup,
        replicates=replicates,
        base_seed=base_seed,
        mean_daily_energy_kwh=daily,
        frequency_summary=frequency_summary,
        maximum_selected_session_duration_support_hours=max(
            r["maximum_possible_session_hours"]
            for r in selected_rows
        ),
        model_folder=model.relative_to(root).as_posix(),
        screen_folder=screen.relative_to(root).as_posix(),
        policy=[
            (
                "One modelled vehicle per selected owner; choose an "
                "admitted index uniformly within each owner "
                "per replicate."
            ),
            (
                "Owner energy, joint weekly rates and home power "
                "are fixed within a replicate. Non-home rating/power "
                "is sampled per candidate session."
            ),
            (
                "Control baseline: daily Poisson proposal intensity "
                "equals the target completed weekly rate divided "
                "by seven. Accepted rates are measured, not forced "
                "or calibrated."
            ),
            (
                "Frequency diagnostics count accepted session starts "
                "inside the study. Their charging may finish after "
                "the study boundary; end timestamps and carry-out "
                "energy remain in the traces."
            ),
            (
                "Contexts follow the fixed joint rates; starts are "
                "uniform inside reported circular windows, with "
                "no additional TOU shift."
            ),
            (
                "Chronological owner-overlap rejection; no "
                "rescheduling, replacement, station queues "
                "or dwell constraints."
            ),
            (
                "Rejected candidate energy is hypothetical "
                "proposal energy, not observed unserved demand."
            ),
            (
                "Parameter, proposal and power streams are separate "
                "owner/replicate hashes. Case labels do not enter "
                "their seeds; cross-run pairing still requires "
                "verification."
            ),
            (
                "Warm-up carry-in, study energy and carry-out "
                "are retained; initialization stationarity "
                "is not established."
            ),
            (
                "Energy and power are the existing common-boundary "
                "constant-power test proxy; no conversion loss, "
                "battery clipping or extra SOC/distance energy."
            ),
            (
                "MCSE describes pilot replicate sampling over the "
                "finite parameter bank and process model; no "
                "input-fit uncertainty or production precision "
                "target is established."
            ),
        ],
        completed_frequency_agreement_established=False,
        warmup_stationarity_established=False,
        empirical_validation=False,
        national_forecast_ready=False,
        checks=dict(
            source_manifests_match=True,
            selected_owner_indices_verified=True,
            selected_time_budgets_checked=True,
            no_owner_overlaps=True,
            session_and_hourly_energy_conserved=True,
            replicate_and_mean_profile_energy_checked=True,
            input_hashes_unchanged=True,
        ),
        input_sha256={
            path.relative_to(root).as_posix(): old
            for path, old in watched.items()
        },
        output_sha256={
            path.name: baseline.sha256(path)
            for path in sorted(output.iterdir())
        },
    )

    (output / "joint_parameter_pilot_report.json").write_text(
        json.dumps(
            report, indent=2,
            ensure_ascii=False, allow_nan=False,
        ) + "\n",
        encoding="utf-8",
    )
    return output, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--screen", required=True)
    parser.add_argument(
        "--energy-case", choices=CASES, default=CASES[0]
    )
    parser.add_argument(
        "--power-scenario",
        choices=("reduced_test", "rated_ceiling_test"),
        default="reduced_test",
    )
    parser.add_argument(
        "--cohort",
        choices=("common_energy_cases", "open_upper_extension"),
        default="common_energy_cases",
    )
    parser.add_argument("--days", type=int, default=28)
    parser.add_argument("--warmup-days", type=int, default=7)
    parser.add_argument("--replicates", type=int, default=10)
    parser.add_argument("--base-seed", type=int, default=20261007)
    args = parser.parse_args()

    output, report = run_pilot(
        args.model, args.screen,
        args.energy_case, args.power_scenario, args.cohort,
        args.days, args.warmup_days,
        args.replicates, args.base_seed,
    )

    print("Joint-parameter scheduling control pilot complete.")
    print(f"owners_simulated: {report['owners_simulated']}")
    print(f"replicates: {report['replicates']}")
    print(
        f"mean_daily_energy_kwh: "
        f"{report['mean_daily_energy_kwh']:.6f}"
    )

    for row in report["frequency_summary"]:
        candidate = (
            f"{row['candidate_to_target_ratio']:.4f}"
            if row["candidate_to_target_ratio"] is not None
            else "NA"
        )
        accepted = (
            f"{row['accepted_to_target_ratio']:.4f}"
            if row["accepted_to_target_ratio"] is not None
            else "NA"
        )
        rejected = (
            f"{row['rejection_percent']:.2f}%"
            if row["rejection_percent"] is not None
            else "NA"
        )
        print(
            f"{row['context']}: candidate/target={candidate}, "
            f"accepted/target={accepted}, rejection={rejected}"
        )

    print(
        "Index-selection, scheduling and "
        "energy-accounting checks passed."
    )
    print(f"Output: {output.relative_to(ROOT)}")
    print(
        "Control pilot only; completed-frequency agreement "
        "and empirical validation are not established."
    )


if __name__ == "__main__":
    try:
        main()
    except (
        ValueError, OSError, KeyError, TypeError, RuntimeError
    ) as exc:
        print(
            f"Joint-parameter pilot failed: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1)