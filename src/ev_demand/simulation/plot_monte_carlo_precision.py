"""Export diagnostic figures from a verified synthetic precision assessment."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
from ev_demand.parameters.build_public_power_layer import load_json, sha256
from ev_demand.simulation.audit_session_run import read_csv, require
from ev_demand.simulation.diagnose_session_rejections import checked_path
from ev_demand.simulation.plot_session_profiles import GROUPS, numeric
from ev_demand.simulation.run_session_simulator import CONTEXTS, close

CASES = ("reduced", "rated", "difference")
COLORS = dict(
    T1="#2563eb", T2="#059669", T3="#d97706",
    T4="#7c3aed", TOTAL="#111827",
)


def load_precision(precision, root):
    folder = checked_path(root, precision, "data/processed/synthetic")
    path = folder / "precision_report.json"
    report = load_json(path)
    require(
        report.get("schema_version") == "synthetic_mean_precision_v1"
        and report.get("data_origin") == "SYNTHETIC"
        and bool(report["checks"])
        and all(v is True for v in report["checks"].values()),
        "Expected a verified synthetic precision report.",
    )
    watched = {path: sha256(path)}
    for section in ("input_sha256", "source_sha256"):
        for reference, digest in report[section].items():
            current = (root / reference).resolve()
            require(
                current.is_relative_to(root) and sha256(current) == digest,
                "Precision input or source changed: " + current.name,
            )
            watched[current] = digest
    for name, digest in report["output_sha256"].items():
        require(
            Path(name).name == name and sha256(folder / name) == digest,
            "Precision export changed: " + name,
        )
        watched[folder / name] = digest
    ns = report["checkpoints"]
    require(
        bool(ns) and all(type(k) is int and k >= 2 for k in ns)
        and ns == sorted(set(ns)) and ns[-1] == report["replicates"],
        "Invalid checkpoints.",
    )

    def table(name, hourly=False):
        path = folder / name
        require(path in watched, "Precision table is not covered by saved hashes.")
        rows = read_csv(path)
        lookup = {}
        for row in rows:
            k, c, case = int(row["n"]), row["context"], row["case"]
            key = (
                (k, c, case, int(row["hour_of_day"]))
                if hourly else (k, c, case)
            )
            require(key not in lookup, "Duplicated precision row.")
            mean, sd, se = (
                numeric(row, f)
                for f in ("mean", "sample_std", "mc_standard_error")
            )
            require(
                k >= 2 and sd >= 0 and se >= 0
                and close(se, sd / math.sqrt(k)),
                "Invalid standard error.",
            )
            require(case == "difference" or mean >= 0, "Negative scenario mean.")
            require(
                row["unit"] == ("kW" if hourly else "kWh/day"),
                "Invalid precision unit.",
            )
            if case != "difference" and mean > 0:
                require(
                    close(numeric(row, "relative_mcse_percent"), 100 * se / mean),
                    "Invalid relative standard error.",
                )
            else:
                require(
                    row["relative_mcse_percent"] == "",
                    "Unexpected relative error for signed or zero mean.",
                )
            lookup[key] = row
        expected = (
            {
                (k, c, case, h)
                for k in ns for c in GROUPS
                for case in CASES for h in range(24)
            }
            if hourly else {
                (k, c, case)
                for k in ns for c in GROUPS for case in CASES
            }
        )
        require(set(lookup) == expected, "Missing or unexpected precision rows.")
        return lookup

    daily = table("daily_energy_precision.csv")
    hourly = table("hourly_mean_precision.csv", True)
    for k in ns:
        for c in GROUPS:
            for case in CASES:
                require(
                    close(
                        math.fsum(
                            numeric(hourly[(k, c, case, h)], "mean")
                            for h in range(24)
                        ),
                        numeric(daily[(k, c, case)], "mean"),
                    ),
                    "Mean profile energy does not match daily mean.",
                )
            require(
                close(
                    numeric(daily[(k, c, "difference")], "mean"),
                    numeric(daily[(k, c, "rated")], "mean")
                    - numeric(daily[(k, c, "reduced")], "mean"),
                ),
                "Paired daily means disagree.",
            )
        for case in CASES:
            require(
                close(
                    math.fsum(
                        numeric(daily[(k, c, case)], "mean")
                        for c in CONTEXTS
                    ),
                    numeric(daily[(k, "TOTAL", case)], "mean"),
                ),
                "Context means do not sum to TOTAL.",
            )
    return report, daily, hourly, watched


def build_figures(precision, project_root=ROOT):
    root = project_root.resolve()
    report, daily, hourly, watched = load_precision(precision, root)
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({
        "font.family": "DejaVu Sans",
        "font.size": 10,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "svg.fonttype": "none",
    })
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    output = root / "data/processed/synthetic" / f"precision_figures_{stamp}"
    output.mkdir(parents=True, exist_ok=False)
    ns, n = report["checkpoints"], report["replicates"]
    subtitle = (
        f"{report['owners_simulated']} synthetic owners | "
        f"{report['study_days']} days per replicate | Baseline scheduling"
    )

    def save(fig, name, title, note):
        fig.suptitle(title, fontsize=15, y=0.97)
        fig.text(0.5, 0.91, subtitle, ha="center", fontsize=10)
        fig.text(0.08, 0.075, note, fontsize=9)
        fig.text(
            0.08, 0.035,
            "Fixed synthetic inputs; no convergence or empirical validation established.",
            fontsize=9, color="#4b5563",
        )
        fig.savefig(output / f"{name}.png", dpi=200, facecolor="white")
        fig.savefig(output / f"{name}.svg", facecolor="white")
        plt.close(fig)

    def checkpoint_axis(axis):
        axis.set_xscale("log")
        axis.set_xlabel("Replicates (log scale; nested prefixes)")
        axis.set_xticks(ns, [str(k) for k in ns])
        axis.minorticks_off()
        axis.grid(axis="y", alpha=0.25)

    fig, axes = plt.subplots(1, 2, figsize=(12, 6))
    fig.subplots_adjust(
        left=0.08, right=0.97, top=0.78, bottom=0.24, wspace=0.28,
    )
    for case, color in (
        ("reduced", "#2563eb"),
        ("rated", "#ea580c"),
        ("difference", "#7c3aed"),
    ):
        rows = [daily[(k, "TOTAL", case)] for k in ns]
        axis = axes[1] if case == "difference" else axes[0]
        axis.errorbar(
            ns, [numeric(r, "mean") for r in rows],
            yerr=[numeric(r, "mc_standard_error") for r in rows],
            marker="o", capsize=4, color=color, label=case.capitalize(),
        )
    for axis in axes:
        checkpoint_axis(axis)
        axis.set_ylabel("Daily energy (kWh/day)")
        axis.legend(frameon=False)
    axes[0].set_title("Total daily means")
    axes[1].set_title("Paired difference: rated minus reduced")
    save(
        fig, "01_checkpoint_means", "Monte Carlo daily-mean precision",
        "Error bars: +/- one Monte Carlo standard error; no confidence level is assigned. Prefixes share runs.",
    )

    fig, axes = plt.subplots(1, 2, figsize=(12, 6))
    fig.subplots_adjust(
        left=0.08, right=0.97, top=0.78, bottom=0.24, wspace=0.25,
    )
    for axis, case in zip(axes, CASES[:2]):
        for c in GROUPS:
            values = [
                float(daily[(k, c, case)]["relative_mcse_percent"])
                if daily[(k, c, case)]["relative_mcse_percent"]
                else float("nan")
                for k in ns
            ]
            axis.plot(ns, values, marker="o", color=COLORS[c], label=c)
        checkpoint_axis(axis)
        axis.set_title(case.capitalize() + " power")
        axis.set_ylabel("Relative standard error of daily mean (%)")
        axis.set_ylim(bottom=0)
        axis.legend(frameon=False, ncol=2)
    save(
        fig, "02_context_daily_precision",
        "Daily-energy precision by charging context",
        "Relative MCSE = 100 x standard error / mean. Zero-mean values are omitted. No stopping threshold is applied.",
    )

    fig, axes = plt.subplots(1, 3, figsize=(14, 6))
    fig.subplots_adjust(
        left=0.07, right=0.98, top=0.74, bottom=0.24, wspace=0.30,
    )
    for axis, case in zip(axes, CASES):
        for c in GROUPS:
            values = [
                numeric(hourly[(n, c, case, h)], "mc_standard_error")
                for h in range(24)
            ]
            axis.step(
                range(25), values + [values[-1]],
                where="post", color=COLORS[c], label=c,
            )
        axis.set_title(case.capitalize())
        axis.set_xlabel("Hour of day")
        axis.set_ylabel("Hourly mean standard error (kW)")
        axis.set_xlim(0, 24)
        axis.set_xticks(range(0, 25, 6))
        axis.set_ylim(bottom=0)
        axis.grid(axis="y", alpha=0.25)
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(
        handles, labels, loc="upper center",
        bbox_to_anchor=(0.5, 0.86), ncol=5, frameon=False,
    )
    save(
        fig, "03_hourly_precision",
        f"Pointwise hourly-mean precision at {n} replicates",
        "Paired errors use within-replicate differences. Context standard errors are not additive; panel scales vary.",
    )
    require(
        all(sha256(path) == digest for path, digest in watched.items()),
        "An input changed during plotting.",
    )
    result = dict(
        schema_version="synthetic_precision_figures_v1",
        data_origin="SYNTHETIC",
        replicates=n,
        precision_folder=checked_path(
            root, precision, "data/processed/synthetic",
        ).relative_to(root).as_posix(),
        checkpoints=ns,
        checks=dict(
            input_hashes_match=True,
            standard_errors_consistent=True,
            mean_energy_balances=True,
        ),
        input_sha256={
            p.relative_to(root).as_posix(): digest
            for p, digest in watched.items()
        },
        source_sha256={
            "src/ev_demand/simulation/plot_monte_carlo_precision.py":
                sha256(Path(__file__)),
        },
        output_sha256={p.name: sha256(p) for p in output.iterdir()},
        convergence_established=False,
        empirically_validated=False,
    )
    (output / "precision_figure_report.json").write_text(
        json.dumps(result, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return output, result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--precision", required=True)
    args = parser.parse_args()
    output, report = build_figures(Path(args.precision))
    print("Monte Carlo precision figures complete.")
    print(f"replicates: {report['replicates']}")
    print(f"checkpoints: {report['checkpoints']}")
    print("figures: 3 PNG + 3 SVG")
    print("All input, standard-error and mean-energy checks passed.")
    print(f"Output: {output.relative_to(ROOT)}")
    print("Precision figures only; no convergence or empirical validation established.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError, OverflowError, ImportError) as exc:
        print(f"Precision plotting failed: {exc}", file=sys.stderr)
        raise SystemExit(1)