"""Review energy candidates against their informative survey intervals."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
import textwrap

import matplotlib
matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import scipy

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from ev_demand.parameters import build_simulator_parameters as baseline
from ev_demand.parameters.build_population_evidence import (
    require,
    write_csv,
)
from ev_demand.parameters.fit_usual_energy_candidates import (
    FAMILIES,
    log_contributions,
    prepare_energy,
)


def partition_review(intervals):
    bins = sorted(set(intervals))
    reasons = []

    if any(low == high for low, high in bins):
        reasons.append(
            "Exact points use densities and cannot be treated as probability bins."
        )
    if bins[0][0] != 0:
        reasons.append(
            "Observed intervals do not cover the lower end of positive support."
        )
    if not math.isinf(bins[-1][1]):
        reasons.append(
            "Observed intervals do not cover the open upper tail."
        )

    for previous, current in zip(bins, bins[1:]):
        if current[0] < previous[1]:
            reasons.append("Observed intervals overlap.")
        elif current[0] > previous[1]:
            reasons.append("Observed intervals contain gaps.")

    return dict(
        complete_nonoverlapping_partition=not reasons,
        reasons=sorted(set(reasons)),
    )


def diagnose(intervals, candidates):
    require(bool(intervals), "No informative energy intervals.")

    partition = partition_review(intervals)
    counts = Counter(intervals)
    bins = sorted(counts)

    low = np.array([lo for lo, hi in bins])
    high = np.array([hi for lo, hi in bins])
    observed = np.array(
        [counts[key] for key in bins],
        dtype=float,
    )

    total = len(intervals)
    shares = observed / total
    comparisons, summaries = [], []

    for candidate in candidates:
        family = candidate["family"]
        shape = candidate["shape"]
        scale = candidate["scale_kwh"]

        logs = log_contributions(
            family, shape, scale, low, high
        )
        require(
            np.all(np.isfinite(logs)),
            "Nonfinite diagnostic likelihood.",
        )

        ll = float(np.dot(observed, logs))
        require(
            math.isclose(
                ll,
                candidate["log_likelihood"],
                rel_tol=0,
                abs_tol=1e-6,
            ),
            "Saved candidate likelihood does not reproduce.",
        )

        require(
            candidate["n_owners_used"] == total
            and candidate["n_free_parameters"] == 2
            and candidate["fixed_location_kwh"] == 0
            and math.isclose(
                candidate["aic"],
                4 - 2 * ll,
                rel_tol=0,
                abs_tol=1e-6,
            )
            and math.isclose(
                candidate["bic"],
                2 * math.log(total) - 2 * ll,
                rel_tol=0,
                abs_tol=1e-6,
            ),
            (
                "Candidate sample count, parameter count "
                "or information criteria mismatch."
            ),
        )

        probabilities = np.exp(logs)
        valid = partition["complete_nonoverlapping_partition"]

        if valid:
            require(
                math.isclose(
                    float(probabilities.sum()),
                    1,
                    rel_tol=0,
                    abs_tol=1e-10,
                ),
                (
                    "Fitted bin probabilities do not sum to one; "
                    "no renormalisation is applied."
                ),
            )

        deviance = (
            float(
                2 * np.dot(
                    observed,
                    np.log(shares) - logs,
                )
            )
            if valid
            else None
        )
        require(
            deviance is None or deviance >= -1e-8,
            "Unexpected negative binned deviance.",
        )

        summaries.append(
            dict(
                family=family,
                n_informative_owners=total,
                category_diagnostics_available=valid,
                aic=candidate["aic"],
                mean_kwh=candidate["mean_kwh"],
                binned_deviance=(
                    max(0.0, deviance) if valid else None
                ),
                binned_total_variation=(
                    float(
                        0.5 * np.abs(
                            shares - probabilities
                        ).sum()
                    )
                    if valid
                    else None
                ),
                max_absolute_share_error_percentage_points=(
                    float(
                        100 * np.abs(
                            shares - probabilities
                        ).max()
                    )
                    if valid
                    else None
                ),
                minimum_expected_owner_count=(
                    float((total * probabilities).min())
                    if valid
                    else None
                ),
            )
        )

        for index, (lower, upper) in enumerate(bins):
            comparisons.append(
                dict(
                    family=family,
                    lower_kwh=lower,
                    upper_kwh=(
                        upper if math.isfinite(upper) else ""
                    ),
                    n_informative_owners=total,
                    observed_owner_records=counts[(lower, upper)],
                    observed_report_fraction=float(shares[index]),
                    contribution_type=(
                        "density_per_kwh"
                        if lower == upper
                        else "probability"
                    ),
                    fitted_contribution=float(probabilities[index]),
                    log_contribution=float(logs[index]),
                    expected_owner_count=(
                        float(total * probabilities[index])
                        if valid
                        else ""
                    ),
                    observed_minus_fitted_percentage_points=(
                        float(
                            100 * (
                                shares[index]
                                - probabilities[index]
                            )
                        )
                        if valid
                        else ""
                    ),
                )
            )

    return comparisons, summaries, partition


def make_figure(output, intervals, candidates, partition, retained):
    counts = Counter(intervals)
    bins = sorted(counts)
    total = len(intervals)

    colours = {
        "lognormal": "#C87924",
        "gamma": "#24729B",
    }

    with plt.rc_context(
        {
            "font.family": "DejaVu Sans",
            "font.size": 10,
            "svg.fonttype": "none",
        }
    ):
        fig, axes = plt.subplots(
            1, 2, figsize=(12, 5.3)
        )

        if partition["complete_nonoverlapping_partition"]:
            positions = np.arange(len(bins))
            width = 0.24

            axes[0].bar(
                positions - width,
                [100 * counts[key] / total for key in bins],
                width,
                label="Reported",
                color="#50565C",
            )

            for index, row in enumerate(candidates):
                logs = log_contributions(
                    row["family"],
                    row["shape"],
                    row["scale_kwh"],
                    np.array([lo for lo, hi in bins]),
                    np.array([hi for lo, hi in bins]),
                )

                axes[0].bar(
                    positions + index * width,
                    100 * np.exp(logs),
                    width,
                    label=row["family"].capitalize(),
                    color=colours[row["family"]],
                )

            labels = [
                (
                    f"{lower:g}-{upper:g}"
                    if math.isfinite(upper)
                    else f">={lower:g}"
                )
                for lower, upper in bins
            ]

            axes[0].set_xticks(
                positions,
                labels,
                rotation=25,
                ha="right",
            )
            axes[0].set_xlabel(
                "Reported energy interval (kWh)"
            )
            axes[0].set_ylabel(
                "Share of informative owner reports (%)"
            )
            axes[0].legend(frameon=False)

        else:
            axes[0].axis("off")
            axes[0].text(
                0.02,
                0.92,
                "Category comparison needs review",
                fontsize=12,
                weight="bold",
                va="top",
                transform=axes[0].transAxes,
            )
            axes[0].text(
                0.02,
                0.80,
                "\n\n".join(
                    textwrap.fill(reason, 50)
                    for reason in partition["reasons"]
                ),
                fontsize=10,
                va="top",
                transform=axes[0].transAxes,
            )

        axes[0].set_title(
            "Reported intervals and fitted probabilities",
            pad=13,
        )

        finite_edges = [
            value
            for pair in bins
            for value in pair
            if math.isfinite(value)
        ]

        xmax = max(
            finite_edges
            + [
                float(
                    FAMILIES[row["family"]].ppf(
                        0.99,
                        row["shape"],
                        scale=row["scale_kwh"],
                    )
                )
                for row in candidates
            ]
        )

        require(
            math.isfinite(xmax) and xmax > 0,
            "Invalid CDF display range.",
        )

        x = np.linspace(0, xmax, 600)

        for row in candidates:
            cdf = FAMILIES[row["family"]].cdf(
                x,
                row["shape"],
                scale=row["scale_kwh"],
            )
            axes[1].plot(
                x,
                cdf,
                lw=2,
                color=colours[row["family"]],
                label=row["family"].capitalize(),
            )

        if partition["complete_nonoverlapping_partition"]:
            cumulative = 0
            edges, fractions = [], []

            for lower, upper in bins:
                cumulative += counts[(lower, upper)]

                if math.isfinite(upper):
                    edges.append(upper)
                    fractions.append(cumulative / total)

            axes[1].scatter(
                edges,
                fractions,
                s=35,
                color="#333333",
                zorder=4,
                label="Reported bin-edge fractions",
            )

        axes[1].set(
            xlabel="Usual energy (kWh)",
            ylabel="Cumulative proportion",
            ylim=(-0.02, 1.02),
        )
        axes[1].set_title("Candidate CDFs", pad=13)
        axes[1].legend(
            frameon=False,
            fontsize=9,
            loc="lower right",
        )

        for axis in axes:
            if axis.axison:
                axis.grid(axis="y", alpha=0.2)
                axis.set_axisbelow(True)
                axis.spines[["top", "right"]].set_visible(False)

        fig.suptitle(
            "Usual-energy candidate review",
            fontsize=15,
            weight="bold",
            y=0.98,
        )
        fig.text(
            0.5,
            0.035,
            (
                f"SYNTHETIC survey inputs | "
                f"Informative owners: {total}/{retained} | "
                "Conditional in-sample diagnostics; "
                "empirical validation pending"
            ),
            ha="center",
            fontsize=9,
        )

        fig.tight_layout(rect=(0, 0.09, 1, 0.93))

        for extension in ("png", "svg"):
            fig.savefig(
                output / (
                    "usual_energy_candidate_review."
                    + extension
                ),
                dpi=180,
                facecolor="white",
            )

        plt.close(fig)


def build_review(fits_folder, project_root=ROOT):
    root = Path(project_root).resolve()
    watched = {}

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

    for name in (
        "docs/research_pipeline_specification.md",
        "src/ev_demand/parameters/build_simulator_parameters.py",
        "src/ev_demand/parameters/build_population_evidence.py",
        "src/ev_demand/parameters/fit_usual_energy_candidates.py",
        "src/ev_demand/parameters/review_usual_energy_candidates.py",
    ):
        watch(name)

    folder = (root / fits_folder).resolve()
    require(
        folder.is_relative_to(root / "data/processed/synthetic")
        and folder.is_dir(),
        "Use a completed synthetic usual-energy candidate folder.",
    )

    parent = json.loads(
        watch(folder / "usual_energy_fit_report.json").read_text(
            encoding="utf-8-sig"
        )
    )

    require(
        parent.get("schema_version")
        == "usual_energy_candidate_fits_v1"
        and parent.get("status") == "candidates_for_review"
        and parent.get("data_origin") == "SYNTHETIC"
        and parent.get("parameter_id") == "usual_energy_kwh",
        "Unexpected candidate-fit report.",
    )
    require(
        bool(parent.get("checks"))
        and all(
            value is True
            for value in parent["checks"].values()
        ),
        "An upstream fitting check did not pass.",
    )

    expected = {
        "energy_evidence_audit.csv",
        "energy_candidate_fits.csv",
        "optimizer_attempts.csv",
        "likelihood_group_checks.csv",
    }
    require(
        set(parent["output_sha256"]) == expected,
        "Unexpected fit export manifest.",
    )

    for name in sorted(expected):
        path = watch(folder / name)
        require(
            watched[path] == parent["output_sha256"][name],
            "Fit export hash mismatch: " + name,
        )

    audit = baseline.read_csv(
        folder / "energy_evidence_audit.csv"
    )

    require(
        len(audit)
        == len({row["owner_id"] for row in audit})
        == parent["owners_retained"]
        and all(
            row["owner_id"]
            and row["data_origin"] == "SYNTHETIC"
            for row in audit
        ),
        "Invalid energy-owner accounting.",
    )

    reproduced, intervals = prepare_energy(audit)

    for old, new in zip(audit, reproduced):
        require(
            old["fit_included"] in {"True", "False"}
            and (old["fit_included"] == "True")
            == new["fit_included"]
            and old["likelihood_type"] == new["likelihood_type"]
            and old["exclusion_reason"] == new["exclusion_reason"],
            "Energy inclusion audit did not reproduce.",
        )

    require(
        len(intervals) == parent["owners_used"]
        and len(audit) - len(intervals)
        == parent["owners_excluded"]
        and dict(
            Counter(
                row["likelihood_type"]
                for row in reproduced
                if row["fit_included"]
            )
        ) == parent["included_likelihood_types"]
        and dict(
            Counter(
                row["exclusion_reason"]
                for row in reproduced
                if not row["fit_included"]
            )
        ) == parent["exclusion_reasons"],
        "Saved energy coverage does not reproduce.",
    )

    candidates = parent["candidates"]
    table = baseline.read_csv(
        folder / "energy_candidate_fits.csv"
    )

    require(
        len(candidates) == len(table) == len(FAMILIES)
        and {
            row["family"] for row in candidates
        } == set(FAMILIES),
        "Candidate family coverage mismatch.",
    )

    by_family = {
        row["family"]: row
        for row in table
    }

    require(
        len(by_family) == len(table)
        and all(
            by_family[row["family"]]
            == {
                key: str(value)
                for key, value in row.items()
            }
            for row in candidates
        ),
        "Candidate CSV differs from its report.",
    )

    comparisons, summaries, partition = diagnose(
        intervals,
        candidates,
    )

    require(
        all(
            baseline.sha256(path) == old
            for path, old in watched.items()
        ),
        "An input changed during review.",
    )

    stamp = datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%S_%fZ"
    )
    output = root / "data/processed/synthetic" / (
        "usual_energy_review_" + stamp
    )
    output.mkdir(parents=True, exist_ok=False)

    write_csv(
        output / "energy_interval_comparison.csv",
        comparisons,
    )
    write_csv(
        output / "energy_fit_diagnostics.csv",
        summaries,
    )

    make_figure(
        output,
        intervals,
        candidates,
        partition,
        len(audit),
    )

    report = dict(
        schema_version="usual_energy_review_v1",
        status=(
            "diagnostics_ready"
            if partition["complete_nonoverlapping_partition"]
            else "category_partition_review_required"
        ),
        data_origin="SYNTHETIC",
        owners_retained=len(audit),
        owners_used=len(intervals),
        owners_excluded=len(audit) - len(intervals),
        partition_review=partition,
        candidate_diagnostics=summaries,
        software=dict(
            numpy=np.__version__,
            scipy=scipy.__version__,
            matplotlib=matplotlib.__version__,
        ),
        checks=dict(
            source_export_hashes_match=True,
            inclusion_accounting_reproduced=True,
            candidate_tables_match=True,
            candidate_likelihoods_and_information_criteria_reproduced=True,
            input_hashes_unchanged=True,
        ),
        interpretation=[
            (
                "The same informative owner reports are used for fitting "
                "and these in-sample diagnostics."
            ),
            (
                "Category expectations and residuals are computed only "
                "for a complete non-overlapping interval partition "
                "of positive support."
            ),
            (
                "Gaps or overlapping intervals require review; "
                "no missing bins are filled and no probabilities "
                "are renormalised."
            ),
            (
                "Deviance and total variation describe binned mismatch; "
                "no goodness-of-fit p-value or pass/fail threshold "
                "is supplied."
            ),
            (
                "Excluded and unresolved reports remain outside the "
                "informative denominator; no missingness correction "
                "is applied."
            ),
            (
                "CDF dots use cumulative reported bin counts at known "
                "edges; no category midpoints are treated as observations."
            ),
            (
                "Smooth curves between edges and beyond observed finite "
                "bounds are parametric model assumptions."
            ),
            (
                "CDF display ends at the larger fitted 99th percentile "
                "or observed edge; fitted distributions are not truncated."
            ),
            (
                "These are owner-level usual-energy candidates, "
                "not within-owner session variation or empirical "
                "Sri Lankan demand validation."
            ),
        ],
        model_selected_for_simulation=False,
        goodness_of_fit_test_performed=False,
        parameter_uncertainty_estimated=False,
        new_simulations=0,
        district_demand_ready=False,
        input_sha256={
            path.relative_to(root).as_posix(): old
            for path, old in watched.items()
        },
        output_sha256={
            path.name: baseline.sha256(path)
            for path in sorted(output.iterdir())
        },
    )

    require(
        all(
            baseline.sha256(path) == old
            for path, old in watched.items()
        ),
        "An input changed during export.",
    )

    (output / "usual_energy_review_report.json").write_text(
        json.dumps(
            report,
            indent=2,
            allow_nan=False,
        ) + "\n",
        encoding="utf-8",
    )

    return output, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fits", required=True)
    args = parser.parse_args()

    output, report = build_review(args.fits)

    print("Usual-energy candidate review complete.")

    for key in (
        "owners_retained",
        "owners_used",
        "owners_excluded",
        "status",
    ):
        print(f"{key}: {report[key]}")

    for row in report["candidate_diagnostics"]:
        if row["category_diagnostics_available"]:
            print(
                f"{row['family']}: "
                f"deviance={row['binned_deviance']:.4f}, "
                f"total_variation={row['binned_total_variation']:.4f}, "
                f"max_share_error="
                f"{row['max_absolute_share_error_percentage_points']:.3f} "
                "percentage points"
            )

    for reason in report["partition_review"]["reasons"]:
        print("Partition review: " + reason)

    print(
        "All source-integrity, inclusion "
        "and likelihood checks passed."
    )
    print("Figures: 1 PNG + 1 SVG")
    print(f"Output: {output.relative_to(ROOT)}")
    print(
        "Conditional synthetic fit diagnostics; model activation, "
        "parameter uncertainty and empirical validation pending."
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
    ) as error:
        print(
            f"Usual-energy review failed: {error}",
            file=sys.stderr,
        )
        raise SystemExit(1)