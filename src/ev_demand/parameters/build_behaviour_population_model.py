"""Assemble linked parameter components and common comparison cohorts."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from ev_demand.parameters import build_simulator_parameters as baseline
from ev_demand.parameters.build_population_evidence import (
    CONTEXTS, interval_kind, require, window_kind, write_csv,
)
from ev_demand.parameters.build_context_behaviour_candidates import use_status
from ev_demand.parameters.build_joint_frequency_model import (
    endpoint_membership, solve_owner,
)
from ev_demand.parameters.build_usual_energy_model import (
    CASES, EVIDENCE_FILES, FREQUENCY_FILES,
)
from ev_demand.parameters.build_charger_power_model import (
    SCENARIOS, CONTEXT_FILES, power_rule,
)
from ev_demand.parameters.build_public_power_layer import compile_power_config
from ev_demand.parameters.fit_usual_energy_candidates import prepare_energy

ENERGY_FILES = {
    "energy_owner_case_audit.csv",
    "usual_energy_test_draws.csv",
    "energy_case_summary.csv",
}
POWER_FILES = {
    "charger_power_context_audit.csv",
    "charger_power_coverage.csv",
    "charger_power_owner_coverage.csv",
    "charger_power_test_draws.csv",
}
JOIN_FIELDS = (
    "owner_id", "energy_case", "parameter_draw", "total_weekly_rate",
    *(c + "_weekly_rate" for c in CONTEXTS),
    "usual_energy_kwh", "home_power_proxy_kw",
    "exceeds_reported_battery_upper",
)


def indexed(rows, fields):
    keys = [tuple(row[field] for field in fields) for row in rows]
    require(
        len(keys) == len(set(keys)),
        "Duplicate linked records: " + ", ".join(fields),
    )
    return dict(zip(keys, rows))


def build_model(
    evidence_folder, frequency_folder, energy_folder,
    context_folder, power_folder, project_root=ROOT,
):
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
            "Use a completed synthetic input folder.",
        )
        report = json.loads(
            watch(folder / report_name).read_text(encoding="utf-8-sig")
        )
        require(
            report.get("schema_version") == schema
            and report.get("status") == status
            and report.get("data_origin") == "SYNTHETIC",
            "Unexpected package: " + report_name,
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

    def same_snapshot(report, folder, names):
        for name in names:
            path = folder / name
            require(
                report["input_sha256"].get(
                    path.relative_to(root).as_posix()
                ) == watched[path],
                "Component snapshot differs: " + name,
            )

    for name in (
        "docs/research_pipeline_specification.md",
        *(
            "src/ev_demand/parameters/" + module + ".py"
            for module in (
                "build_simulator_parameters",
                "build_population_evidence",
                "build_context_behaviour_candidates",
                "build_joint_frequency_model",
                "build_usual_energy_model",
                "fit_usual_energy_candidates",
                "build_charger_power_model",
                "build_public_power_layer",
                "build_behaviour_population_model",
            )
        ),
    ):
        watch(name)

    evidence, spec = package(
        evidence_folder, "behaviour_population_spec.json",
        "behaviour_population_evidence_v1",
        "evidence_prepared", EVIDENCE_FILES,
    )
    frequency, freq = package(
        frequency_folder, "joint_frequency_model.json",
        "joint_frequency_rate_model_v2",
        "parameter_sampling_test_complete", FREQUENCY_FILES,
    )
    energy, ener = package(
        energy_folder, "usual_energy_model.json",
        "usual_energy_sampling_model_v1",
        "parameter_sampling_test_complete", ENERGY_FILES,
    )
    contexts, timing = package(
        context_folder, "context_candidate_model.json",
        "context_behaviour_candidates_v1",
        "candidates_prepared", CONTEXT_FILES,
    )
    power, powr = package(
        power_folder, "charger_power_model.json",
        "charger_power_sampling_model_v1",
        "parameter_sampling_test_complete", POWER_FILES,
    )

    for report in (spec, timing, powr):
        require(
            report.get("context_labels") == CONTEXTS,
            "Context definitions differ.",
        )

    for report in (freq, ener, timing, powr):
        same_snapshot(
            report, evidence,
            EVIDENCE_FILES | {"behaviour_population_spec.json"},
        )

    same_snapshot(
        ener, frequency,
        FREQUENCY_FILES | {"joint_frequency_model.json"},
    )
    same_snapshot(
        powr, contexts,
        CONTEXT_FILES | {"context_candidate_model.json"},
    )

    n = freq["draws_per_eligible_owner"]
    require(
        type(n) is int and n > 0
        and n == ener["draws_per_eligible_owner"]
        == powr["draws_per_supported_context_per_case"],
        "Component draw counts differ.",
    )
    require(
        powr["power_scenarios"] == list(SCENARIOS),
        "Unexpected power cases.",
    )
    pools = {
        s: compile_power_config(powr["config_snapshot"], s)
        for s in SCENARIOS
    }

    donors = [
        json.loads(line)
        for line in (
            evidence / "linked_owner_evidence.jsonl"
        ).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    ids = {r["owner_id"] for r in donors}
    require(
        bool(ids) and "" not in ids
        and len(ids) == len(donors) == spec["owners_retained"],
        "Invalid source-owner grid.",
    )

    models = {
        name: indexed(report["owners"], ("owner_id",))
        for name, report in (
            ("frequency", freq), ("energy", ener), ("power", powr)
        )
    }
    require(
        all(set(rows) == {(oid,) for oid in ids}
            for rows in models.values()),
        "Owner sets differ.",
    )
    require(
        all(report["owners_retained"] == len(ids)
            for report in (freq, ener, timing, powr)),
        "Retained-owner totals differ.",
    )

    expected_contexts = {
        (oid, code) for oid in ids for code in CONTEXTS
    }
    timing_rows = indexed(
        baseline.read_csv(contexts / "context_report_audit.csv"),
        ("owner_id", "legacy_context"),
    )
    require(
        set(timing_rows) == expected_contexts,
        "Timing context grid differs.",
    )

    numeric = baseline.read_csv(evidence / "numeric_evidence.csv")
    numbers = indexed(
        numeric, ("owner_id", "parameter_id", "legacy_context")
    )
    require(
        len(numeric) == spec["numeric_evidence_rows"],
        "Numeric evidence count differs.",
    )
    energy_audit, _ = prepare_energy([
        numbers[(oid, "usual_energy_kwh", "")]
        for oid in sorted(ids)
    ])
    energy_info = {r["owner_id"]: r for r in energy_audit}

    fdraws = indexed(
        baseline.read_csv(
            frequency / "joint_frequency_test_draws.csv"
        ),
        ("owner_id", "test_draw"),
    )
    edraws = indexed(
        baseline.read_csv(energy / "usual_energy_test_draws.csv"),
        ("owner_id", "case", "parameter_draw"),
    )
    require(
        len(fdraws) == freq["parameter_test_draws"]
        and len(edraws) == ener["parameter_test_draws"],
        "Parameter draw totals differ.",
    )

    home_rows, power_count = [], 0
    with (
        power / "charger_power_test_draws.csv"
    ).open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            power_count += 1
            if row["legacy_context"] == "T2":
                home_rows.append(row)

    require(
        power_count == powr["parameter_test_draws"],
        "Power draw total differs.",
    )
    hdraws = indexed(
        home_rows, ("owner_id", "power_scenario", "test_draw")
    )

    expected_f, expected_e, expected_h = set(), set(), set()
    owner_audit, context_audit, owners, joined = [], [], [], []
    covered = {case: set() for case in CASES}
    flagged = defaultdict(Counter)

    for (oid, case, draw), row in edraws.items():
        require(
            oid in ids and case in CASES
            and row["exceeds_reported_battery_upper"]
            in {"", "True", "False"},
            "Invalid energy draw identity or battery flag.",
        )
        flagged[(oid, case)]["comparisons"] += int(
            row["exceeds_reported_battery_upper"] != ""
        )
        flagged[(oid, case)]["above"] += int(
            row["exceeds_reported_battery_upper"] == "True"
        )

    for donor in sorted(donors, key=lambda r: r["owner_id"]):
        oid = donor["owner_id"]
        source = donor["owner_features"]
        original = donor["original_readiness"]
        f, e, p = (
            models[name][(oid,)]
            for name in ("frequency", "energy", "power")
        )

        require(
            all(
                r["owner_id"] == oid
                and r["data_origin"] == "SYNTHETIC"
                for r in (donor, source, original)
            ),
            "Source-owner origin/link differs.",
        )
        require(
            all(r["original_readiness"] == original
                for r in (f, e, p)),
            "Original readiness links differ.",
        )
        require(
            e["vehicle_class_answer"] == p["vehicle_class_answer"]
            == source["vehicle_class_answer"],
            "Vehicle-class owner links differ.",
        )

        battery_kind = interval_kind(
            source["battery_kwh_min"],
            source["battery_kwh_max"],
            source["battery_kwh_state"],
            "kWh",
        )
        battery_upper = (
            float(source["battery_kwh_max"])
            if battery_kind in {
                "finite_interval", "point", "upper_bound_only"
            }
            and source["battery_kwh_max"].strip()
            and float(source["battery_kwh_max"]) > 0
            else None
        )
        require(
            e["battery_evidence_kind"] == battery_kind
            and e["reported_battery_upper_kwh"] == battery_upper,
            "Battery review bounds differ from source owner.",
        )
        require(
            f["source_total"]
            == numbers[(oid, "sessions_per_week", "")]
            and e["source_energy"]
            == numbers[(oid, "usual_energy_kwh", "")]
            and p["source_home_power"]
            == numbers[(oid, "home_power_kw", "")],
            "Numeric owner links differ.",
        )
        require(
            f["source_contexts"] == {
                c: numbers[(oid, "context_frequency", c)]
                for c in CONTEXTS
            },
            "Context frequency links differ.",
        )
        require(
            f["joint_region"]
            == solve_owner(f["source_total"], f["source_contexts"]),
            "Joint frequency constraints did not reproduce.",
        )

        frequency_ok = f["joint_region"]["sample_allowed"]
        require(
            e["joint_frequency_available"] is frequency_ok,
            "Energy/frequency availability differs.",
        )

        if frequency_ok:
            expected_f.update(
                (oid, str(i)) for i in range(1, n + 1)
            )
            for i in range(1, n + 1):
                row = fdraws[(oid, str(i))]
                total = float(row["total_weekly_rate"])
                values = {
                    c: float(row[c + "_weekly_rate"])
                    for c in CONTEXTS
                }
                require(
                    math.isfinite(total) and total > 0
                    and all(
                        math.isfinite(v) and v >= 0
                        for v in values.values()
                    )
                    and math.isclose(
                        math.fsum(values.values()), total,
                        abs_tol=1e-8, rel_tol=1e-10,
                    )
                    and endpoint_membership(
                        f["source_total"], total
                    ) is True
                    and all(
                        endpoint_membership(
                            f["source_contexts"][c], values[c]
                        ) is True
                        for c in CONTEXTS
                    ),
                    "A saved frequency draw violates its source constraints.",
                )

        sources = indexed(donor["context_features"], ("context",))
        power_contexts = indexed(
            p["contexts"], ("legacy_context",)
        )
        require(
            set(sources) == set(power_contexts)
            == {(c,) for c in CONTEXTS},
            "Owner context set differs.",
        )

        timing_problems, power_problems, linked_contexts = [], [], []
        active = []

        for code, label in CONTEXTS.items():
            raw = sources[(code,)]
            saved = power_contexts[(code,)]
            require(
                raw["owner_id"] == oid
                and raw["data_origin"] == "SYNTHETIC"
                and saved["source_context"] == raw,
                "Power/source context link differs.",
            )

            use, kind = use_status(raw), window_kind(raw)
            status, reason, power_models = power_rule(
                source, raw, pools
            )
            require(
                (
                    saved["sampling_status"],
                    saved["deferral_reason"],
                    saved["power_models"],
                ) == (status, reason, power_models),
                "Power rule did not reproduce.",
            )

            t = timing_rows[(oid, code)]
            expected_timing = (
                use == "use_category"
                and kind == "reported_circular_window"
            )
            require(
                t["use_status"] == use
                and t["charger_code_raw"] == raw["charger_code"]
                and t["window_evidence_kind"] == kind
                and t["timing_candidate_included"]
                == str(expected_timing)
                and all(
                    t[a] == raw[b]
                    for a, b in (
                        ("raw_start_hour", "window_start_hour"),
                        ("raw_end_hour", "window_end_hour"),
                        ("raw_length_hours", "window_length_hours"),
                    )
                ),
                "Timing rules differ from source context.",
            )

            if use != "reported_nonuse":
                if not expected_timing:
                    timing_problems.append(code)
                if status != "supported_test":
                    power_problems.append(code)
                if use == "use_category":
                    active.append(code)

            timing_rule = (
                dict(
                    kind="uniform_circular_start_window_test",
                    start_hour=float(raw["window_start_hour"]),
                    end_hour=float(raw["window_end_hour"]),
                    length_hours=float(raw["window_length_hours"]),
                )
                if expected_timing else None
            )

            linked_contexts.append(dict(
                legacy_context=code,
                charging_context=label,
                use_status=use,
                source_context=raw,
                timing_rule=timing_rule,
                power_status=status,
                power_deferral_reason=reason,
                power_models=power_models,
            ))
            context_audit.append(dict(
                owner_id=oid,
                legacy_context=code,
                charging_context=label,
                use_status=use,
                timing_component_available=(
                    expected_timing or use == "reported_nonuse"
                ),
                power_component_status=status,
                power_deferral_reason=reason,
                window_evidence_kind=kind,
                charger_code=raw["charger_code"],
            ))

            if code == "T2" and status == "supported_test":
                expected_h.update(
                    (oid, s, str(i))
                    for s in SCENARIOS
                    for i in range(1, n + 1)
                )
                for i in range(1, n + 1):
                    a, b = (
                        hdraws[(oid, s, str(i))]
                        for s in SCENARIOS
                    )
                    low, high = (
                        power_models[SCENARIOS[0]][
                            "effective_average_kw_" + suffix
                        ]
                        for suffix in ("min", "max")
                    )
                    require(
                        a["effective_average_power_kw"]
                        == b["effective_average_power_kw"]
                        and low
                        <= float(a["effective_average_power_kw"])
                        <= high,
                        "Home power pairing/support differs.",
                    )

        timing_ok = bool(active) and not timing_problems
        power_ok = bool(active) and not power_problems
        rules = {}
        info = energy_info[oid]

        for case, family in CASES.items():
            reason = info["exclusion_reason"]
            low = (
                float(info["raw_min"])
                if info["raw_min"].strip() else 0.0
            )
            high = (
                float(info["raw_max"])
                if info["raw_max"].strip() else math.inf
            )

            if (
                not reason and low == high
                and info["raw_boundary"] != "closed"
            ):
                reason = "point_boundary_requires_review"
            if (
                not reason and family is None
                and not math.isfinite(high)
            ):
                reason = "uniform_upper_bound_unresolved"

            energy_ok = not bool(reason)
            require(
                e["case_rules"][case] == dict(
                    sample_allowed=energy_ok,
                    deferral_reason=reason,
                ),
                "Energy sampling rule did not reproduce.",
            )

            if energy_ok:
                expected_e.update(
                    (oid, case, str(i))
                    for i in range(1, n + 1)
                )

            blockers = (
                ["frequency:" + f["joint_region"]["status"]]
                if not frequency_ok else []
            )
            blockers += (
                ["energy:" + reason] if not energy_ok else []
            )
            blockers += [
                "timing:" + c for c in timing_problems
            ] + [
                "power:" + c for c in power_problems
            ]
            if not active:
                blockers.append("no_reported_use_context")

            available = (
                frequency_ok and energy_ok
                and timing_ok and power_ok
            )
            if available:
                covered[case].add(oid)

            rules[case] = dict(
                components_available=available,
                component_blockers=blockers,
            )
            counts = flagged[(oid, case)]

            owner_audit.append(dict(
                owner_id=oid,
                original_readiness=original["status"],
                energy_case=case,
                frequency_available=frequency_ok,
                energy_available=energy_ok,
                timing_available=timing_ok,
                power_available=power_ok,
                components_available=available,
                battery_comparison_draws=counts["comparisons"],
                battery_upper_flags=counts["above"],
                component_blockers_json=json.dumps(blockers),
            ))

            for i in range(1, n + 1) if energy_ok else ():
                row = edraws[(oid, case, str(i))]
                value = float(row["usual_energy_kwh"])
                u = float(row["common_quantile"])

                require(
                    math.isfinite(value) and value > 0
                    and low <= value <= high
                    and 0 < u < 1,
                    "Energy draw support/quantile differs.",
                )
                require(
                    row["joint_frequency_available"]
                    == str(frequency_ok)
                    and row["total_weekly_rate"] == (
                        fdraws[(oid, str(i))]["total_weekly_rate"]
                        if frequency_ok else ""
                    ),
                    "Energy/frequency draw link differs.",
                )

                cap = e["reported_battery_upper_kwh"]
                require(
                    row["exceeds_reported_battery_upper"]
                    == (str(value > cap) if cap is not None else ""),
                    "Battery review flag differs.",
                )

                if not available:
                    continue

                fr = fdraws[(oid, str(i))]
                home = (
                    hdraws[
                        (oid, SCENARIOS[0], str(i))
                    ]["effective_average_power_kw"]
                    if "T2" in active else ""
                )
                joined.append(dict(
                    owner_id=oid,
                    energy_case=case,
                    parameter_draw=i,
                    total_weekly_rate=fr["total_weekly_rate"],
                    **{
                        c + "_weekly_rate": fr[c + "_weekly_rate"]
                        for c in CONTEXTS
                    },
                    usual_energy_kwh=value,
                    home_power_proxy_kw=home,
                    exceeds_reported_battery_upper=(
                        row["exceeds_reported_battery_upper"]
                    ),
                ))

        for i in range(1, n + 1):
            quantiles = {
                edraws[
                    (oid, case, str(i))
                ]["common_quantile"]
                for case in CASES
                if e["case_rules"][case]["sample_allowed"]
            }
            require(
                len(quantiles) <= 1,
                "Energy case common quantiles differ.",
            )

        owners.append(dict(
            owner_id=oid,
            original_readiness=original,
            source_owner=source,
            frequency_model=f,
            energy_model=e,
            contexts=linked_contexts,
            case_component_status=rules,
        ))

    require(
        set(fdraws) == expected_f
        and set(edraws) == expected_e
        and set(hdraws) == expected_h,
        "Incomplete, extra or incorrectly numbered parameter draw grid.",
    )
    require(
        len(joined) == n * sum(map(len, covered.values())),
        "Joined parameter draw accounting failed.",
    )

    common = set.intersection(*covered.values())
    conditional = (
        covered["conditional_gamma_test"]
        & covered["conditional_lognormal_test"]
    )
    cohort_ids = dict(
        all_energy_cases_common=sorted(common),
        conditional_cases_common=sorted(conditional),
        conditional_open_upper_extension=sorted(
            conditional - common
        ),
    )

    summaries = [
        dict(
            energy_case=case,
            owners_retained=len(ids),
            frequency_available=sum(
                r["frequency_available"] for r in owner_audit
                if r["energy_case"] == case
            ),
            energy_available=sum(
                r["energy_available"] for r in owner_audit
                if r["energy_case"] == case
            ),
            timing_available=sum(
                r["timing_available"] for r in owner_audit
                if r["energy_case"] == case
            ),
            power_available=sum(
                r["power_available"] for r in owner_audit
                if r["energy_case"] == case
            ),
            all_components_available=len(covered[case]),
            component_incomplete=len(ids) - len(covered[case]),
            common_all_energy_cases=len(common),
            joined_parameter_test_draws=n * len(covered[case]),
        )
        for case in CASES
    ]

    require(
        all(
            baseline.sha256(p) == old
            for p, old in watched.items()
        ),
        "Input changed during assembly.",
    )

    output = allowed / (
        "behaviour_population_model_"
        + datetime.now(timezone.utc).strftime(
            "%Y%m%dT%H%M%S_%fZ"
        )
    )
    output.mkdir(parents=True, exist_ok=False)

    tables = {
        "population_owner_case_audit.csv": owner_audit,
        "population_context_audit.csv": context_audit,
        "population_case_summary.csv": summaries,
    }
    for name, rows in tables.items():
        write_csv(output / name, rows)

    with (
        output / "joint_owner_parameter_test_draws.csv"
    ).open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=JOIN_FIELDS
        )
        writer.writeheader()
        writer.writerows(joined)

    report = dict(
        schema_version="behaviour_population_model_v1",
        status="components_assembled_for_review",
        data_origin="SYNTHETIC",
        behaviour_input_origin="SYNTHETIC",
        output_origin="COMPUTED_PARAMETER_MODEL",
        owners_retained=len(ids),
        original_ready_owners=spec["original_ready_owners"],
        original_deferred_owners=spec["original_deferred_owners"],
        context_labels=CONTEXTS,
        draws_per_supported_owner_case=n,
        joined_parameter_test_draws=len(joined),
        new_random_draws=0,
        new_demand_simulations=0,
        case_summary=summaries,
        comparison_cohort_ids=cohort_ids,
        power_scenarios=list(SCENARIOS),
        owners=owners,
        component_base_seeds=dict(
            frequency=freq["base_seed"],
            energy=ener["base_seed"],
            power=powr["base_seed"],
        ),
        component_assumptions=dict(
            frequency=freq["assumptions"],
            energy=ener["assumptions"],
            timing=timing["assumptions"],
            power=powr["assumptions"],
        ),
        remaining_decisions=[
            (
                "Specify an explicit shared energy/power boundary "
                "and treatment of battery-upper review flags "
                "before demand activation."
            ),
            (
                "Define supported vehicle/equipment scope; body "
                "class alone does not verify battery type, "
                "connectors or vehicle acceptance power."
            ),
            (
                "Demonstrate realised completed charging frequency "
                "under the session generator and overlap scheduler."
            ),
            (
                "Specify coverage-limited donor generation/weighting "
                "or reviewed missing-data sensitivities; never treat "
                "this completeness subset as national fleet shares."
            ),
            (
                "Review interview-rule activation; unreviewed codes "
                "must not supply numerical probabilities."
            ),
            (
                "District/place allocation, fleet stock scenarios "
                "and production precision targets remain separate "
                "pending stages."
            ),
        ],
        limits=[
            (
                "Component availability is not simulator eligibility, "
                "physical compatibility, empirical validation or "
                "approval to scale nationally."
            ),
            (
                "All retained owner records remain linked, including "
                "incomplete records. No values are imputed or clipped."
            ),
            (
                "The joined rows reuse saved parameter tests; they "
                "are not new owners, sessions, independent observations "
                "or demand replicates."
            ),
            (
                "Compare energy families on the same cohort. "
                "The conditional open-upper extension is a "
                "separate cohort analysis."
            ),
            (
                "One usual-energy value and home-power value are "
                "retained per future owner realisation. Non-home "
                "power models remain per-session samplers."
            ),
            (
                "Matching draw indices implements conditional-independent "
                "parameter streams within an owner; it does not "
                "estimate within-owner dependence."
            ),
            (
                "Reported start windows receive no additional TOU "
                "shift. No energy is added from distance or SOC."
            ),
            (
                "Battery flags are retained per draw and do not "
                "establish a physical pass when absent; unknown "
                "battery upper bounds remain unevaluable."
            ),
        ],
        simulator_activation=False,
        owner_eligibility_changed=False,
        population_generation_ready=False,
        district_demand_ready=False,
        empirical_validation=False,
        checks=dict(
            component_export_hashes_match=True,
            shared_source_snapshots_verified=True,
            owner_context_links_verified=True,
            frequency_constraints_reproduced=True,
            timing_and_power_rules_reproduced=True,
            parameter_draw_joins_verified=True,
            matched_energy_quantiles_checked=True,
            original_readiness_preserved=True,
            input_hashes_unchanged=True,
        ),
        input_sha256={
            p.relative_to(root).as_posix(): old
            for p, old in watched.items()
        },
        output_sha256={
            p.name: baseline.sha256(p)
            for p in sorted(output.iterdir())
        },
    )

    require(
        all(
            baseline.sha256(p) == old
            for p, old in watched.items()
        ),
        "Input changed during export.",
    )
    (output / "behaviour_population_model.json").write_text(
        json.dumps(
            report, indent=2,
            ensure_ascii=False, allow_nan=False,
        ) + "\n",
        encoding="utf-8",
    )
    return output, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "evidence", "frequency", "energy", "contexts", "power"
    ):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()

    output, report = build_model(
        args.evidence, args.frequency, args.energy,
        args.contexts, args.power,
    )

    print("Linked behavioural population parameter package complete.")
    print(f"owners_retained: {report['owners_retained']}")

    for row in report["case_summary"]:
        print(
            f"{row['energy_case']}: "
            f"all_components={row['all_components_available']}, "
            f"component_incomplete={row['component_incomplete']}, "
            f"joined_draws={row['joined_parameter_test_draws']}"
        )

    for name, ids in report["comparison_cohort_ids"].items():
        print(f"{name}: {len(ids)} owners")

    print(
        f"joined_parameter_test_draws: "
        f"{report['joined_parameter_test_draws']}"
    )
    print(
        "Source-snapshot, owner/context, component-rule "
        "and parameter-join checks passed."
    )
    print(f"Output: {output.relative_to(ROOT)}")
    print(
        "Assembly only; no new random draws, demand simulations, "
        "imputation or eligibility changes."
    )


if __name__ == "__main__":
    try:
        main()
    except (
        ValueError, OSError, KeyError, TypeError, RuntimeError
    ) as exc:
        print(
            f"Population parameter assembly failed: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1)