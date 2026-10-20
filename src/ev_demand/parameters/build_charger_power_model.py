"""Build linked charger/power test rules without resolving missing reports."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import random
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from ev_demand.parameters import build_simulator_parameters as baseline
from ev_demand.parameters.build_population_evidence import (
    CONTEXTS, interval_kind, require, write_csv,
)
from ev_demand.parameters.build_context_behaviour_candidates import (
    KNOWN_CHARGERS, use_status,
)
from ev_demand.parameters.build_public_power_layer import (
    compile_power_config, sample_session_power,
)

SCENARIOS = ("reduced_test", "rated_ceiling_test")

EVIDENCE_FILES = {
    "numeric_evidence.csv",
    "numeric_coverage.csv",
    "context_windows.csv",
    "categorical_answer_counts.csv",
    "linked_owner_evidence.jsonl",
}

CONTEXT_FILES = {
    "context_report_audit.csv",
    "context_coverage.csv",
    "charger_report_shares.csv",
    "start_hour_candidates.csv",
    "joint_window_charger_categories.csv",
}


def power_rule(owner, context, pools):
    """Return a reusable rule or an explicit reason."""
    use = use_status(context)

    if use == "reported_nonuse":
        return "reported_nonuse", "", {}

    reason, models = "", {}
    code, charger = context["context"], context["charger_code"]

    if use != "use_category":
        reason = "context_use_requires_review"
    elif charger == "MIXED":
        reason = "mixed_charger_proportions_unresolved"
    elif charger == "UNKNOWN" or not charger:
        reason = "charger_type_unknown"
    elif charger not in KNOWN_CHARGERS:
        reason = "charger_type_unsupported"
    elif code == "T2":
        equipment = owner["home_equipment_answer"]

        stated_type = (
            "AC" if equipment.startswith("Dedicated AC")
            else "SOCKET_PORTABLE"
            if equipment.startswith("Standard socket")
            else None
        )

        kind = interval_kind(
            owner["home_power_kw_min"],
            owner["home_power_kw_max"],
            owner["home_power_kw_state"],
            "kW",
        )

        if owner["home_access_answer"] == "No, I cannot charge at home":
            reason = "home_use_with_no_home_access"
        elif charger == "DC":
            reason = "home_dc_requires_review"
        elif stated_type is not None and stated_type != charger:
            reason = "home_equipment_type_conflict"
        elif kind == "lower_bound_only":
            reason = "home_power_upper_bound_unresolved"
        elif kind not in {"finite_interval", "point"}:
            reason = "home_power_bounds_unresolved_or_invalid"
        elif float(owner["home_power_kw_min"]) <= 0:
            reason = "home_power_not_strictly_positive"
        elif (
            kind == "point"
            and owner.get("home_power_kw_boundary", "") != "closed"
        ):
            reason = "home_power_point_boundary_requires_review"
        else:
            model = dict(
                kind="reported_home_uniform",
                effective_average_kw_min=float(owner["home_power_kw_min"]),
                effective_average_kw_max=float(owner["home_power_kw_max"]),
                evidence_status=(
                    "reported_range_used_as_constant_power_test_proxy"
                ),
            )
            models = {
                scenario: dict(model)
                for scenario in SCENARIOS
            }

    elif any(
        (code, charger) not in pools[scenario]
        for scenario in SCENARIOS
    ):
        reason = "nonhome_power_options_unavailable"
    else:
        models = {
            scenario: dict(
                kind="nonhome_rating_mixture",
                scenario=scenario,
                options=pools[scenario][(code, charger)],
                evidence_status="development_assumption",
            )
            for scenario in SCENARIOS
        }

    return (
        "deferred" if reason else "supported_test",
        reason,
        models,
    )


def build_model(
    evidence_folder,
    context_folder,
    config_path,
    draws_per_context=100,
    seed=20261017,
    project_root=ROOT,
):
    require(
        type(draws_per_context) is int and draws_per_context > 0,
        "Use a positive integer draw count.",
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

    for name in (
        "docs/research_pipeline_specification.md",
        "src/ev_demand/parameters/build_simulator_parameters.py",
        "src/ev_demand/parameters/build_population_evidence.py",
        "src/ev_demand/parameters/build_context_behaviour_candidates.py",
        "src/ev_demand/parameters/build_public_power_layer.py",
        "src/ev_demand/parameters/build_charger_power_model.py",
    ):
        watch(name)

    evidence, spec = package(
        evidence_folder,
        "behaviour_population_spec.json",
        "behaviour_population_evidence_v1",
        "evidence_prepared",
        EVIDENCE_FILES,
    )

    contexts, context_report = package(
        context_folder,
        "context_candidate_model.json",
        "context_behaviour_candidates_v1",
        "candidates_prepared",
        CONTEXT_FILES,
    )

    require(
        spec.get("context_labels")
        == context_report.get("context_labels")
        == CONTEXTS,
        "Context definitions differ.",
    )

    for name in EVIDENCE_FILES | {"behaviour_population_spec.json"}:
        path = evidence / name
        require(
            context_report["input_sha256"].get(
                path.relative_to(root).as_posix()
            ) == watched[path],
            "Context candidates must use this exact evidence snapshot: "
            + name,
        )

    config = json.loads(
        watch(config_path).read_text(encoding="utf-8-sig")
    )

    pools = {
        scenario: compile_power_config(config, scenario)
        for scenario in SCENARIOS
    }

    all_pool_keys = set().union(
        *(set(pool) for pool in pools.values())
    )

    for key in all_pool_keys:
        signatures = [
            tuple(
                (
                    row["rating_id"],
                    row["rated_output_kw"],
                    row["selection_probability"],
                )
                for row in pools[scenario].get(key, [])
            )
            for scenario in SCENARIOS
        ]
        require(
            signatures[0] == signatures[1],
            "Power cases need identical rating options and weights.",
        )

    donors = [
        json.loads(line)
        for line in (
            evidence / "linked_owner_evidence.jsonl"
        ).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    ids = {row["owner_id"] for row in donors}
    require(
        bool(ids)
        and "" not in ids
        and len(ids) == len(donors) == spec["owners_retained"],
        "Invalid retained-owner accounting.",
    )

    expected = {
        (owner_id, code)
        for owner_id in ids
        for code in CONTEXTS
    }

    saved_rows = baseline.read_csv(
        contexts / "context_report_audit.csv"
    )
    saved = {
        (row["owner_id"], row["legacy_context"]): row
        for row in saved_rows
    }

    require(
        len(saved) == len(saved_rows) == len(expected)
        and set(saved) == expected,
        "Incomplete or duplicate context audit.",
    )

    numeric = baseline.read_csv(evidence / "numeric_evidence.csv")
    power_rows = [
        row for row in numeric
        if row["parameter_id"] == "home_power_kw"
        and not row["legacy_context"]
    ]
    power_by_id = {
        row["owner_id"]: row
        for row in power_rows
    }

    require(
        len(numeric) == spec["numeric_evidence_rows"]
        and len(power_rows) == len(ids)
        and set(power_by_id) == ids,
        "Incomplete home-power owner links.",
    )

    audit, owner_models, owner_coverage, draws = [], [], [], []
    readiness_counts, reasons = Counter(), Counter()

    for donor in sorted(donors, key=lambda row: row["owner_id"]):
        oid = donor["owner_id"]
        owner = donor["owner_features"]
        readiness = donor["original_readiness"]

        require(
            all(
                row["owner_id"] == oid
                and row["data_origin"] == "SYNTHETIC"
                for row in (donor, owner, readiness)
            ),
            "Owner origin/link mismatch.",
        )

        readiness_counts[readiness["status"]] += 1
        power = power_by_id[oid]

        require(
            power["original_readiness"] == readiness["status"]
            and power["data_origin"] == "SYNTHETIC",
            "Home-power readiness/origin mismatch.",
        )

        fields = {
            "raw_min": "home_power_kw_min",
            "raw_max": "home_power_kw_max",
            "raw_state": "home_power_kw_state",
            "raw_boundary": "home_power_kw_boundary",
            "raw_answer": "home_equipment_answer",
        }

        require(
            all(power[key] == owner[value]
                for key, value in fields.items()),
            "Home-power source fields differ.",
        )
        require(
            power["evidence_kind"] == interval_kind(
                power["raw_min"],
                power["raw_max"],
                power["raw_state"],
                "kW",
            ),
            "Home-power evidence classification differs.",
        )

        sources = donor["context_features"]
        require(
            len(sources) == 4
            and {row["context"] for row in sources} == set(CONTEXTS),
            "Each owner needs four source context records.",
        )

        owner_contexts = []
        counts, unresolved = Counter(), []

        for source in sorted(sources, key=lambda row: row["context"]):
            code = source["context"]
            row = saved[(oid, code)]

            require(
                source["owner_id"] == oid
                and source["data_origin"] == "SYNTHETIC",
                "Context origin/link mismatch.",
            )

            reproduced = dict(
                original_readiness=readiness["status"],
                charging_context=CONTEXTS[code],
                use_status=use_status(source),
                charger_code_raw=source["charger_code"],
                raw_frequency_answer=source["frequency_answer"],
                home_access_answer=owner["home_access_answer"],
                home_equipment_answer=owner["home_equipment_answer"],
            )
            require(
                all(row[key] == value
                    for key, value in reproduced.items()),
                "Saved context source fields differ.",
            )

            status, reason, models = power_rule(
                owner, source, pools
            )
            counts[status] += 1

            if reason:
                reasons[reason] += 1
                unresolved.append(
                    dict(context=code, reason=reason)
                )

            audit.append(dict(
                owner_id=oid,
                original_readiness=readiness["status"],
                legacy_context=code,
                charging_context=CONTEXTS[code],
                use_status=row["use_status"],
                vehicle_class_answer=owner["vehicle_class_answer"],
                charger_code=source["charger_code"],
                raw_charger_answer=source.get("charger_answer", ""),
                raw_charger_state=source.get("charger_state", ""),
                home_equipment_answer=(
                    owner["home_equipment_answer"]
                    if code == "T2" else ""
                ),
                home_power_evidence_kind=(
                    power["evidence_kind"] if code == "T2" else ""
                ),
                home_power_min_kw=(
                    power["raw_min"] if code == "T2" else ""
                ),
                home_power_max_kw=(
                    power["raw_max"] if code == "T2" else ""
                ),
                sampling_status=status,
                deferral_reason=reason,
                power_model_kind=(
                    models[SCENARIOS[0]]["kind"] if models else ""
                ),
            ))

            owner_contexts.append(dict(
                legacy_context=code,
                charging_context=CONTEXTS[code],
                source_context=source,
                sampling_status=status,
                deferral_reason=reason,
                power_models=models,
            ))

            if not models:
                continue

            for i in range(1, draws_per_context + 1):
                draw_seed = int.from_bytes(
                    hashlib.sha256(
                        (
                            f"charger-power-v1|{seed}|"
                            f"{oid}|{code}|{i}"
                        ).encode()
                    ).digest()[:8],
                    "big",
                )

                pair = []

                for scenario in SCENARIOS:
                    value = sample_session_power(
                        {"power_model": models[scenario]},
                        random.Random(draw_seed),
                    )

                    require(
                        math.isfinite(
                            value["effective_average_power_kw"]
                        )
                        and value["effective_average_power_kw"] > 0,
                        "Invalid test power.",
                    )

                    pair.append(value)
                    draws.append(dict(
                        owner_id=oid,
                        legacy_context=code,
                        power_scenario=scenario,
                        test_draw=i,
                        charger_code=source["charger_code"],
                        **value,
                    ))

                require(
                    pair[0]["rating_id"] == pair[1]["rating_id"]
                    and pair[0]["rated_output_kw"]
                    == pair[1]["rated_output_kw"],
                    "Rating pairing failed.",
                )
                require(
                    code != "T2" or pair[0] == pair[1],
                    "Home power changed between non-home scenarios.",
                )

        complete = (
            counts["deferred"] == 0
            and counts["supported_test"] > 0
        )

        owner_coverage.append(dict(
            owner_id=oid,
            original_readiness=readiness["status"],
            supported_power_contexts=counts["supported_test"],
            reported_nonuse_contexts=counts["reported_nonuse"],
            deferred_contexts=counts["deferred"],
            all_context_power_components_available=complete,
            unresolved_contexts_json=json.dumps(unresolved),
        ))

        owner_models.append(dict(
            owner_id=oid,
            original_readiness=readiness,
            vehicle_class_answer=owner["vehicle_class_answer"],
            source_home_power=power,
            contexts=owner_contexts,
        ))

    require(
        readiness_counts == Counter(
            ready_for_bounded_test=spec["original_ready_owners"],
            deferred_for_review=spec["original_deferred_owners"],
        ),
        "Original readiness totals differ.",
    )

    require(
        len(audit) == len(expected)
        and len(draws) == (
            2 * draws_per_context
            * sum(
                row["sampling_status"] == "supported_test"
                for row in audit
            )
        ),
        "Power draw accounting failed.",
    )

    coverage = []

    for code, label in CONTEXTS.items():
        rows = [
            row for row in audit
            if row["legacy_context"] == code
        ]
        counts = Counter(
            row["sampling_status"] for row in rows
        )

        coverage.append(dict(
            legacy_context=code,
            charging_context=label,
            retained_contexts=len(rows),
            use_reports=sum(
                row["use_status"] == "use_category"
                for row in rows
            ),
            supported_power_contexts=counts["supported_test"],
            reported_nonuse_contexts=counts["reported_nonuse"],
            deferred_contexts=counts["deferred"],
        ))

    require(
        bool(draws),
        "No supported power test draws; review inputs before exporting.",
    )
    require(
        all(
            baseline.sha256(path) == old
            for path, old in watched.items()
        ),
        "Input changed during processing.",
    )

    output = allowed / (
        "charger_power_model_"
        + datetime.now(timezone.utc).strftime(
            "%Y%m%dT%H%M%S_%fZ"
        )
    )
    output.mkdir(parents=True, exist_ok=False)

    tables = {
        "charger_power_context_audit.csv": audit,
        "charger_power_coverage.csv": coverage,
        "charger_power_owner_coverage.csv": owner_coverage,
        "charger_power_test_draws.csv": draws,
    }

    for name, rows in tables.items():
        write_csv(output / name, rows)

    report = dict(
        schema_version="charger_power_sampling_model_v1",
        status="parameter_sampling_test_complete",
        data_origin="SYNTHETIC",
        behaviour_input_origin="SYNTHETIC",
        output_origin="COMPUTED_PARAMETER_MODEL",
        owners_retained=len(ids),
        context_records_reviewed=len(audit),
        context_labels=CONTEXTS,
        context_summary=coverage,
        deferral_reason_counts=dict(sorted(reasons.items())),
        owners_with_all_context_power_components=sum(
            row["all_context_power_components_available"]
            for row in owner_coverage
        ),
        parameter_test_draws=len(draws),
        draws_per_supported_context_per_case=draws_per_context,
        power_scenarios=list(SCENARIOS),
        base_seed=seed,
        config_snapshot=config,
        owners=owner_models,
        assumptions=dict(
            HOME_RANGE_PROXY_V1=(
                "Reported finite positive home ranges supply a "
                "uniform constant-power test proxy; actual delivered "
                "power is not measured."
            ),
            NONHOME_POWER_V1=(
                "Existing rating weights and average/rated fractions "
                "are development assumptions conditional on the "
                "known charger type."
            ),
            POWER_TEST_PAIRING_V1=(
                "Each owner/context/draw uses the same random stream "
                "across power cases. Home draws and selected non-home "
                "ratings are matched."
            ),
            DRAW_INTERPRETATION_V1=(
                "These are sampler checks, not sessions. Future home "
                "equipment power is retained per owner realisation; "
                "non-home equipment/power is sampled per session."
            ),
        ),
        limits=[
            (
                "Mixed types retain their raw answers; no charger "
                "subset or usage proportion is inferred. Unknown "
                "types are not imputed."
            ),
            (
                "Open-upper and missing home power remain deferred; "
                "non-home ratings never fill home-power gaps."
            ),
            (
                "Unknown equipment detail is not automatically "
                "a type contradiction."
            ),
            (
                "Availability of power components is not full owner "
                "readiness or approval of energy, timing or "
                "scheduling models."
            ),
            (
                "Deferral reasons identify the first blocking power "
                "rule per context; retained source fields can contain "
                "additional unresolved quantities."
            ),
            (
                "Vehicle acceptance limits, connectors, battery "
                "compatibility and the electrical energy boundary "
                "remain unresolved."
            ),
            (
                "Rating factors are not conversion efficiency; no "
                "losses, SOC taper, station sharing, queues or extra "
                "TOU delay are added."
            ),
            (
                "No station-inventory probabilities, fleet shares "
                "or district/place allocation are estimated."
            ),
            (
                "Paired copies and repeated draws are not independent "
                "owners or observations."
            ),
        ],
        electrical_boundary=(
            "Constant-power test proxy; meter-versus-battery "
            "interpretation remains unresolved."
        ),
        simulator_activation=False,
        owner_eligibility_changed=False,
        missing_values_imputed=False,
        population_generation_ready=False,
        district_demand_ready=False,
        empirical_validation=False,
        checks=dict(
            source_manifests_match=True,
            shared_evidence_snapshot_verified=True,
            owner_context_links_verified=True,
            original_readiness_preserved=True,
            power_case_rating_pairing_checked=True,
            home_power_case_invariance_checked=True,
            positive_test_power_checked=True,
            complete_draw_accounting=True,
            input_hashes_unchanged=True,
        ),
        input_sha256={
            path.relative_to(root).as_posix(): old
            for path, old in watched.items()
        },
        output_sha256={
            name: baseline.sha256(output / name)
            for name in tables
        },
    )

    require(
        all(
            baseline.sha256(path) == old
            for path, old in watched.items()
        ),
        "Input changed during export.",
    )

    (output / "charger_power_model.json").write_text(
        json.dumps(
            report,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        ) + "\n",
        encoding="utf-8",
    )

    return output, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--contexts", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--draws-per-context", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20261017)
    args = parser.parse_args()

    output, report = build_model(
        args.evidence,
        args.contexts,
        args.config,
        args.draws_per_context,
        args.seed,
    )

    print("Owner-linked charger/power parameter model complete.")

    for key in (
        "owners_retained",
        "context_records_reviewed",
        "owners_with_all_context_power_components",
    ):
        print(f"{key}: {report[key]}")

    for row in report["context_summary"]:
        print(
            f"{row['legacy_context']}: "
            f"use_reports={row['use_reports']}, "
            f"supported_power={row['supported_power_contexts']}, "
            f"deferred={row['deferred_contexts']}, "
            f"reported_nonuse={row['reported_nonuse_contexts']}"
        )

    print(
        f"deferral_reason_counts: "
        f"{report['deferral_reason_counts']}"
    )
    print(f"parameter_test_draws: {report['parameter_test_draws']}")
    print(
        "Source, owner-link, power-pairing and "
        "draw-accounting checks passed."
    )
    print(f"Output: {output.relative_to(ROOT)}")
    print(
        "Power assumptions only; no imputation, demand simulations "
        "or owner eligibility changes."
    )


if __name__ == "__main__":
    try:
        main()
    except (
        ValueError, OSError, KeyError, TypeError, RuntimeError
    ) as exc:
        print(
            f"Charger/power model preparation failed: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1)