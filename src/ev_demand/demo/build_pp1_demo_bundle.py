"""Package verified synthetic reference results for an offline PP1 demo."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from ev_demand.parameters.build_public_power_layer import (
    load_json,
    sha256,
    validate_readiness,
)
from ev_demand.simulation.audit_session_run import read_csv, require
from ev_demand.simulation.diagnose_session_rejections import checked_path
from ev_demand.simulation.plot_session_profiles import (
    LABELS,
    load_plot_data,
    numeric,
)
from ev_demand.simulation.plot_monte_carlo_precision import load_precision
from ev_demand.simulation.run_session_simulator import close


def save_json(path, value):
    path.write_text(
        json.dumps(value, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def build_bundle(comparison, precision, trace_replicate=1, root=ROOT):
    root = Path(root).resolve()

    folder, comparison_report, profiles, watched = load_plot_data(
        comparison, root
    )
    precision_report, daily, hourly, precision_watched = load_precision(
        precision, root
    )

    comparison_path = folder / "public_power_comparison_report.json"

    require(
        precision_report["comparison_folder"]
        == folder.relative_to(root).as_posix()
        and comparison_path in precision_watched,
        "Precision assessment does not cover the selected comparison.",
    )

    for path, digest in precision_watched.items():
        require(
            path not in watched or watched[path] == digest,
            "Conflicting input hashes.",
        )
        watched[path] = digest

    n = comparison_report["replicates"]

    require(
        precision_report["replicates"] == n,
        "Precision replicate count differs.",
    )
    require(
        type(trace_replicate) is int and 1 <= trace_replicate <= n,
        "trace_replicate must select an existing replicate.",
    )

    for name in (
        "demo/build_pp1_demo_bundle.py",
        "simulation/plot_session_profiles.py",
        "simulation/plot_monte_carlo_precision.py",
    ):
        path = root / "src/ev_demand" / name
        watched[path] = sha256(path)

    def covered(path):
        require(
            path in watched and sha256(path) == watched[path],
            "Demo input is not covered by verified hashes: " + path.name,
        )
        return path

    trace, packages, batches = {}, {}, {}

    for case in ("reduced", "rated"):
        batch_folder = checked_path(
            root,
            comparison_report[case + "_batch"],
            "data/simulated/synthetic",
        )
        batch = load_json(
            covered(batch_folder / "repeated_test_report.json")
        )
        entries = read_csv(
            covered(batch_folder / "replicate_summaries.csv")
        )

        require(
            len(entries) == n and batch["replicates_completed"] == n,
            "Unexpected batch replicate count.",
        )

        entry = entries[trace_replicate - 1]

        require(
            int(entry["replicate"]) == trace_replicate,
            "Unexpected trace replicate index.",
        )

        run_folder = checked_path(
            root, entry["run_folder"], "data/simulated/synthetic"
        )
        saved = load_json(
            covered(run_folder / "simulation_report.json")
        )

        require(
            saved["summary"]["seed"] == int(entry["seed"])
            and saved["summary"]["study_days"]
            == comparison_report["study_days"],
            "Trace settings differ from the comparison.",
        )

        parameter_folder = checked_path(
            root,
            batch["parameter_folder"],
            "data/processed/synthetic",
        )
        package = load_json(
            covered(parameter_folder / "simulator_parameters.json")
        )
        readiness = validate_readiness(
            covered(parameter_folder / "owner_readiness.csv"),
            package,
        )

        require(
            len(package["owner_profiles"])
            == comparison_report["owners_simulated"],
            "Ready owner count differs from the comparison.",
        )

        trace[case] = dict(
            run_folder=run_folder.relative_to(root).as_posix(),
            replicate=trace_replicate,
            seed=int(entry["seed"]),
            warmup_days=saved["summary"]["warmup_days"],
            accepted_sessions=read_csv(
                covered(run_folder / "sessions.csv")
            ),
            rejected_sessions=read_csv(
                covered(run_folder / "rejected_sessions.csv")
            ),
            owner_rates=read_csv(
                covered(run_folder / "owner_rates.csv")
            ),
        )

        batches[case], packages[case] = batch, package

        if case == "reduced":
            readiness_rows = readiness
            readiness_bytes = (
                parameter_folder / "owner_readiness.csv"
            ).read_bytes()
        else:
            require(
                (
                    parameter_folder / "owner_readiness.csv"
                ).read_bytes() == readiness_bytes,
                "Readiness differs between scenarios.",
            )

    require(
        trace["reduced"]["seed"] == trace["rated"]["seed"]
        and trace["reduced"]["warmup_days"]
        == trace["rated"]["warmup_days"],
        "Trace seed or warm-up differs between scenarios.",
    )

    for row in comparison_report["context_comparison"]:
        for case in ("reduced", "rated"):
            require(
                close(
                    numeric(daily[(n, row["context"], case)], "mean"),
                    row[case + "_mean_daily_energy_kwh"],
                ),
                "Precision means do not match the selected comparison.",
            )

    daily_rows = []

    for (count, context, case), row in sorted(daily.items()):
        daily_rows.append(
            dict(
                n=count,
                context=context,
                case=case,
                mean=numeric(row, "mean"),
                mcse=numeric(row, "mc_standard_error"),
                relative_mcse_percent=(
                    float(row["relative_mcse_percent"])
                    if row["relative_mcse_percent"]
                    else None
                ),
            )
        )

    hourly_rows = [
        dict(
            context=context,
            case=case,
            hour_of_day=hour,
            mean=numeric(row, "mean"),
            mcse=numeric(row, "mc_standard_error"),
        )
        for (count, context, case, hour), row
        in sorted(hourly.items())
        if count == n
    ]

    first_reasons = Counter(
        row["first_deferral_reason"]
        for row in readiness_rows
        if row["status"] == "deferred_for_review"
    )

    bundle = dict(
        schema_version="pp1_synthetic_demo_v1",
        data_origin="SYNTHETIC",
        title="EV charging demand simulator - PP1 development demo",
        settings=dict(
            study_days=comparison_report["study_days"],
            warmup_days=trace["reduced"]["warmup_days"],
            replicates_per_case=n,
            owners_simulated=comparison_report["owners_simulated"],
            owners_retained=len(readiness_rows),
            owners_deferred=sum(first_reasons.values()),
            base_seed=batches["reduced"]["base_seed"],
            scenarios={
                case: batches[case]["power_scenario"]
                for case in batches
            },
        ),
        context_labels=LABELS,
        comparison=comparison_report["context_comparison"],
        ensemble_mean_peaks=comparison_report["ensemble_mean_peaks"],
        matched_candidates_including_warmup=comparison_report[
            "matched_candidates_including_warmup"
        ],
        profiles=profiles,
        daily_precision=daily_rows,
        hourly_precision=hourly_rows,
        readiness=readiness_rows,
        first_deferral_counts=dict(first_reasons),
        owner_profiles=packages["reduced"]["owner_profiles"],
        example_replicate=trace,
        interpretation=[
            "All owner records and numerical results in this demo "
            "are synthetic.",
            "Aggregate profiles use all replicates; the session "
            "explorer shows one selected paired replicate.",
            "Bands describe replicate study-period mean profiles, "
            "not the distribution of individual daily peaks.",
            "MCSE describes simulation mean precision, not "
            "real-world forecasting accuracy.",
            "Session rejection is an owner-overlap model outcome, "
            "not observed station failure or unmet demand.",
            "Charging contexts are not established district "
            "or land-use classifications.",
            "No real-data calibration, district/national scaling, "
            "trained AI forecast or station queues are included.",
        ],
        empirical_validation=False,
        national_forecast_ready=False,
        ml_model_trained=False,
        source_folders=dict(
            comparison=folder.relative_to(root).as_posix(),
            precision=checked_path(
                root, precision, "data/processed/synthetic"
            ).relative_to(root).as_posix(),
        ),
    )

    def unchanged():
        require(
            all(
                sha256(path) == digest
                for path, digest in watched.items()
            ),
            "A verified input or source changed during packaging.",
        )

    unchanged()

    stamp = datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%S_%fZ"
    )
    output = root / "outputs/synthetic" / ("pp1_demo_" + stamp)
    output.mkdir(parents=True, exist_ok=False)

    report_path = output / "demo_bundle_report.json"

    report = dict(
        schema_version="pp1_demo_bundle_report_v1",
        data_origin="SYNTHETIC",
        status="in_progress",
        settings=bundle["settings"],
        source_folders=bundle["source_folders"],
        input_and_source_sha256={
            path.relative_to(root).as_posix(): digest
            for path, digest in watched.items()
        },
        empirical_validation=False,
        national_forecast_ready=False,
        ml_model_trained=False,
    )

    save_json(report_path, report)
    save_json(output / "demo_bundle.json", bundle)

    unchanged()

    report.update(
        status="complete",
        checks=dict(
            verified_sources_unchanged=True,
            comparison_precision_agree=True,
            readiness_preserved=True,
            trace_settings_match=True,
        ),
        output_sha256={
            "demo_bundle.json": sha256(output / "demo_bundle.json")
        },
    )

    save_json(report_path, report)
    return output, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--comparison", required=True)
    parser.add_argument("--precision", required=True)
    parser.add_argument("--trace-replicate", type=int, default=1)
    args = parser.parse_args()

    output, report = build_bundle(
        Path(args.comparison),
        Path(args.precision),
        args.trace_replicate,
    )

    print("PP1 synthetic demo bundle complete.")

    for key, value in report["settings"].items():
        print(f"{key}: {value}")

    print(
        "All source, comparison, precision and readiness "
        "checks passed."
    )
    print("Files: demo_bundle.json, demo_bundle_report.json")
    print(f"Output: {output.relative_to(ROOT)}")
    print(
        "Packaging only; no new simulation, parameter "
        "fitting or AI training."
    )


if __name__ == "__main__":
    try:
        main()
    except (
        ValueError,
        OSError,
        KeyError,
        TypeError,
        OverflowError,
    ) as error:
        print(
            f"PP1 demo packaging failed: {error}",
            file=sys.stderr,
        )
        raise SystemExit(1)