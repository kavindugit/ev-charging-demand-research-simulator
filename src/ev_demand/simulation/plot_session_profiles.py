"""Export figures from a verified synthetic public-power comparison."""
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
from ev_demand.simulation.run_session_simulator import CONTEXTS, close, write_csv

GROUPS = CONTEXTS + ("TOTAL",)
LABELS = {
    "T1": "T1 - Workplace / routine non-home",
    "T2": "T2 - Home", "T3": "T3 - Destination", "T4": "T4 - En-route",
    "TOTAL": "Total ready-owner pool",
}
BLUE, ORANGE, PURPLE = "#2563eb", "#ea580c", "#7c3aed"


def numeric(row, field):
    value = float(row[field])
    require(math.isfinite(value), "Nonfinite plot value: " + field)
    return value


def keyed(rows):
    result = {(row["context"], int(row["hour_of_day"])): row for row in rows}
    require(len(rows) == len(result) == 120
            and set(result) == {(code, hour) for code in GROUPS for hour in range(24)},
            "Expected one row for every context and hour.")
    return result


def load_plot_data(comparison, root):
    folder = checked_path(root, comparison, "data/processed/synthetic")
    path = folder / "public_power_comparison_report.json"
    report = load_json(path)
    require(report.get("schema_version") == "paired_public_power_comparison_v1"
            and report.get("data_origin") == "SYNTHETIC" and all(report["checks"].values()),
            "Expected a verified synthetic public-power comparison.")
    watched = {path: sha256(path)}
    for reference, digest in report["input_sha256"].items():
        current = (root / reference).resolve()
        require(current.is_relative_to(root) and sha256(current) == digest,
                "Comparison source changed: " + current.name)
        watched[current] = digest
    for name, digest in report["output_sha256"].items():
        require(Path(name).name == name and sha256(folder / name) == digest,
                "Comparison export changed: " + name)
        watched[folder / name] = digest
    reduced = checked_path(root, report["reduced_batch"], "data/simulated/synthetic")
    band_path = reduced / "profile_summary.csv"
    require(band_path in watched, "Baseline profile summary is not covered by the comparison hashes.")
    bands = keyed(read_csv(band_path))
    means = keyed(read_csv(folder / "mean_profile_comparison.csv"))
    summaries = {row["context"]: row for row in report["context_comparison"]}
    require(set(summaries) == set(GROUPS), "Missing context summaries.")
    data = []
    for code in GROUPS:
        for hour in range(24):
            band, mean = bands[(code, hour)], means[(code, hour)]
            require(band["data_origin"] == "SYNTHETIC" and int(band["n"]) == report["replicates"],
                    "Baseline band origin or replicate count differs.")
            a, b = numeric(mean, "reduced_mean_power_kw"), numeric(mean, "rated_mean_power_kw")
            low, median, high = (numeric(band, key) for key in ("p05", "p50", "p95"))
            delta = numeric(mean, "mean_difference_kw")
            d_low, d_high = numeric(mean, "difference_p05_kw"), numeric(mean, "difference_p95_kw")
            require(a >= 0 and b >= 0 and 0 <= low <= median <= high and d_low <= d_high,
                    "Invalid power or percentile ordering.")
            require(close(a, numeric(band, "mean")) and close(delta, b - a),
                    "Profile means or paired differences disagree.")
            data.append(dict(
                context=code, hour_of_day=hour, data_origin="SYNTHETIC",
                reduced_mean_power_kw=a, rated_mean_power_kw=b,
                reduced_p05_kw=low, reduced_p50_kw=median, reduced_p95_kw=high,
                mean_difference_kw=delta, difference_p05_kw=d_low, difference_p95_kw=d_high,
            ))
        rows = [row for row in data if row["context"] == code]
        for case in ("reduced", "rated"):
            require(close(math.fsum(row[f"{case}_mean_power_kw"] for row in rows),
                          summaries[code][f"{case}_mean_daily_energy_kwh"]),
                    "Plot profile does not conserve mean daily energy.")
    lookup = {(row["context"], row["hour_of_day"]): row for row in data}
    for hour in range(24):
        for case in ("reduced", "rated"):
            require(close(math.fsum(lookup[(code, hour)][f"{case}_mean_power_kw"] for code in CONTEXTS),
                          lookup[("TOTAL", hour)][f"{case}_mean_power_kw"]),
                    "Context means do not sum to TOTAL.")
    return folder, report, data, watched


def paint_figures(output, report, data):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise ValueError("Matplotlib is required. Install it with your venv Python: -m pip install matplotlib") from exc
    plt.rcParams.update({
        "font.family": "DejaVu Sans", "font.size": 10, "axes.spines.top": False,
        "axes.spines.right": False, "axes.titleweight": "bold", "svg.fonttype": "none",
    })
    subtitle = (f"{report['owners_simulated']} synthetic owners | {report['replicates']} replicates | "
                f"{report['study_days']} days per replicate | Baseline scheduling")
    disclaimer = "Synthetic delivered-energy load proxy; no district/national scaling or empirical validation."

    def values(code, field):
        rows = sorted((row for row in data if row["context"] == code), key=lambda row: row["hour_of_day"])
        y = [row[field] for row in rows]
        return y + [y[-1]]

    x = list(range(25))

    def style(axis, difference=False):
        axis.set_xlim(0, 24)
        axis.set_xticks(range(0, 25, 3), [f"{hour:02d}:00" for hour in range(0, 25, 3)])
        axis.set_xlabel("Hour of day (hourly bins)")
        axis.set_ylabel("Rated minus reduced (kW)" if difference else "Mean hourly power (kW)")
        axis.grid(axis="y", color="#d1d5db", linewidth=0.6, alpha=0.7)
        axis.set_axisbelow(True)
        if not difference:
            axis.set_ylim(bottom=0)

    def power_panel(axis, code):
        axis.fill_between(x, values(code, "reduced_p05_kw"), values(code, "reduced_p95_kw"),
                          step="post", color=BLUE, alpha=0.13, label="Reduced P05-P95")
        axis.step(x, values(code, "reduced_mean_power_kw"), where="post", color=BLUE,
                  linewidth=2, label="Reduced mean")
        axis.step(x, values(code, "rated_mean_power_kw"), where="post", color=ORANGE,
                  linewidth=1.8, linestyle="--", label="Rated mean")
        style(axis)

    def save(figure, name):
        figure.savefig(output / f"{name}.png", dpi=200, facecolor="white")
        figure.savefig(output / f"{name}.svg", facecolor="white")
        plt.close(figure)

    figure, axis = plt.subplots(figsize=(12, 6))
    figure.subplots_adjust(left=0.085, right=0.97, top=0.78, bottom=0.20)
    figure.suptitle("Synthetic 24-hour charging profile", fontsize=16, y=0.97)
    figure.text(0.5, 0.90, subtitle, ha="center", fontsize=10)
    power_panel(axis, "TOTAL")
    axis.legend(loc="upper left", frameon=False)
    figure.text(0.085, 0.07, "Shading: P05-P95 across reduced-power replicate mean profiles; not a confidence interval.", fontsize=9)
    figure.text(0.085, 0.035, disclaimer, fontsize=9, color="#4b5563")
    save(figure, "01_total_profile")

    figure, axes = plt.subplots(2, 2, figsize=(12, 8), sharex=True)
    figure.subplots_adjust(left=0.085, right=0.97, top=0.78, bottom=0.16, hspace=0.46, wspace=0.25)
    figure.suptitle("Charging profiles by context", fontsize=16, y=0.97)
    figure.text(0.5, 0.92, subtitle, ha="center", fontsize=10)
    for axis, code in zip(axes.flat, CONTEXTS):
        power_panel(axis, code)
        axis.set_title(LABELS[code], fontsize=11)
        axis.tick_params(axis="x", labelbottom=True)
    handles, labels = axes.flat[0].get_legend_handles_labels()
    figure.legend(handles, labels, loc="upper center", bbox_to_anchor=(0.5, 0.89), ncol=3, frameon=False)
    figure.text(0.085, 0.07, "Panel scales vary. Shading describes replicate mean profiles; context percentile bands are not additive.", fontsize=9)
    figure.text(0.085, 0.035, disclaimer, fontsize=9, color="#4b5563")
    save(figure, "02_context_profiles")

    figure, axis = plt.subplots(figsize=(12, 6))
    figure.subplots_adjust(left=0.085, right=0.97, top=0.78, bottom=0.20)
    figure.suptitle("Paired hourly power difference: rated minus reduced", fontsize=16, y=0.97)
    figure.text(0.5, 0.90, subtitle, ha="center", fontsize=10)
    axis.fill_between(x, values("TOTAL", "difference_p05_kw"), values("TOTAL", "difference_p95_kw"),
                      step="post", color=PURPLE, alpha=0.15, label="Paired difference P05-P95")
    axis.step(x, values("TOTAL", "mean_difference_kw"), where="post", color=PURPLE,
              linewidth=2, label="Mean paired difference")
    axis.axhline(0, color="#4b5563", linewidth=0.8)
    style(axis, difference=True)
    axis.legend(loc="upper left", frameon=False)
    figure.text(0.085, 0.07, "Shading: pointwise P05-P95 of matched replicate differences; not a confidence interval.", fontsize=9)
    figure.text(0.085, 0.035, disclaimer, fontsize=9, color="#4b5563")
    save(figure, "03_paired_power_difference")
    return matplotlib.__version__


def build_plots(comparison, project_root=ROOT):
    root = project_root.resolve()
    folder, source_report, data, watched = load_plot_data(comparison, root)
    source = root / "src/ev_demand/simulation/plot_session_profiles.py"
    require(sha256(source) == sha256(Path(__file__)), "Plotting source does not match this running file.")
    watched[source] = sha256(source)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    output = root / "data/processed/synthetic" / f"profile_figures_{stamp}"
    output.mkdir(parents=True, exist_ok=False)
    version = paint_figures(output, source_report, data)
    require(all(sha256(path) == digest for path, digest in watched.items()), "An input changed during plotting.")
    write_csv(output / "plot_data.csv", data, tuple(data[0]))
    report = {
        "schema_version": "synthetic_profile_figures_v1", "data_origin": "SYNTHETIC",
        "comparison_folder": folder.relative_to(root).as_posix(),
        "owners_simulated": source_report["owners_simulated"], "replicates": source_report["replicates"],
        "study_days": source_report["study_days"], "matplotlib_version": version,
        "figures": ["01_total_profile", "02_context_profiles", "03_paired_power_difference"],
        "checks": dict(source_hashes_match=True, mean_daily_energy_balances=True, context_means_sum_to_total=True),
        "input_sha256": {path.relative_to(root).as_posix(): digest for path, digest in watched.items()},
        "output_sha256": {path.name: sha256(path) for path in output.iterdir() if path.is_file()},
        "interpretation": "Step plots show hourly averages. Bands describe replicate mean profiles or paired differences, not daily extremes or empirical confidence intervals.",
        "empirically_validated": False, "national_forecast_ready": False, "convergence_established": False,
    }
    (output / "figure_report.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return output, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--comparison", required=True)
    args = parser.parse_args()
    output, report = build_plots(Path(args.comparison))
    print("Synthetic profile figures complete.")
    print(f"owners_simulated: {report['owners_simulated']}")
    print(f"replicates: {report['replicates']}")
    print("figures: 3 PNG + 3 SVG")
    print("All source and profile-accounting checks passed.")
    print(f"Output: {output.relative_to(ROOT)}")
    print("Synthetic development figures; no convergence or empirical validation established.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError, OverflowError) as exc:
        print(f"Profile plotting failed: {exc}", file=sys.stderr)
        raise SystemExit(1)