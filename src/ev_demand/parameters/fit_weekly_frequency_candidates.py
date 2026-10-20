"""Compare grouped usual-week count models; preserve the count interpretation as an assumption."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys

import matplotlib
matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import scipy
from scipy import optimize, special, stats

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from ev_demand.parameters import build_simulator_parameters as baseline
from ev_demand.parameters.build_population_evidence import (
    interval_kind,
    require,
    write_csv,
)

# Reviewed response options, including options with zero observed reports.
COUNT_BINS = (
    (0, 1),
    (2, 3),
    (4, 5),
    (6, 7),
    (8, None),
)
LABELS = ("0-1", "2-3", "4-5", "6-7", "8+")

LOG_MU_GUARD = 15.0
ALPHA_GUARD = 1000.0
NLL_TOLERANCE = 1e-6


def prepare_frequency(rows):
    audit, groups = [], []

    for row in rows:
        require(
            row["unit"] == "sessions"
            and row["reported_period"] == "week",
            "Expected total weekly frequency in sessions/week.",
        )

        kind = interval_kind(
            row["raw_min"],
            row["raw_max"],
            row["raw_state"],
            "sessions",
        )
        require(
            kind == row["evidence_kind"],
            "Frequency evidence classification did not reproduce.",
        )

        group, reason = None, ""

        if kind in {
            "unresolved",
            "not_applicable",
            "invalid_encoding",
            "conflict_review",
        }:
            reason = kind
        else:
            require(
                row["raw_boundary"] in {"closed", "left_open"},
                "Unreviewed frequency boundary rule: "
                + row["raw_boundary"],
            )

            low = (
                float(row["raw_min"])
                if row["raw_min"].strip()
                else 0.0
            )
            high = (
                float(row["raw_max"])
                if row["raw_max"].strip()
                else None
            )

            require(
                low.is_integer()
                and (high is None or high.is_integer()),
                (
                    "Non-integer frequency bounds require "
                    "a different reporting model."
                ),
            )

            lo = int(low) + (
                1 if row["raw_boundary"] == "left_open" else 0
            )
            hi = int(high) if high is not None else None

            require(
                (lo, hi) in COUNT_BINS,
                "Frequency category differs from the reviewed response options.",
            )

            group = COUNT_BINS.index((lo, hi))
            groups.append(group)

        audit.append(
            dict(
                row,
                fit_included=group is not None,
                exclusion_reason=reason,
                count_bin=(
                    LABELS[group] if group is not None else ""
                ),
                integer_min=(
                    COUNT_BINS[group][0]
                    if group is not None
                    else ""
                ),
                integer_max=(
                    COUNT_BINS[group][1]
                    if group is not None
                    and COUNT_BINS[group][1] is not None
                    else ""
                ),
            )
        )

    return audit, groups


def count_log_probabilities(mu, alpha=0.0):
    """NB2 variance=mu+alpha*mu**2; alpha=0 is exactly Poisson."""
    require(
        math.isfinite(mu)
        and mu > 0
        and math.isfinite(alpha)
        and alpha >= 0,
        "Invalid count-distribution parameters.",
    )

    # Stable recurrence near the Poisson limit.
    masses = np.empty(8)
    masses[0] = (
        -mu
        if alpha == 0
        else -math.log1p(alpha * mu) / alpha
    )

    for k in range(1, 8):
        masses[k] = (
            masses[k - 1]
            + math.log(mu)
            + math.log1p(alpha * (k - 1))
            - math.log1p(alpha * mu)
            - math.log(k)
        )

    finite = [
        float(special.logsumexp(masses[lo:hi + 1]))
        for lo, hi in COUNT_BINS[:-1]
    ]

    if alpha == 0:
        tail_log = float(stats.poisson.logsf(7, mu))
    else:
        # P(K>=8)=I_q(8,r), r=1/alpha.
        q = alpha * mu / (1 + alpha * mu)
        tail = float(
            special.betainc(8, 1 / alpha, q)
        )
        tail_log = (
            math.log(tail) if tail > 0 else -math.inf
        )

    return np.array(finite + [tail_log])


def fit_counts(groups):
    require(
        len(groups) >= 3,
        "Too few informative frequency reports.",
    )

    counts = np.bincount(
        groups,
        minlength=len(COUNT_BINS),
    ).astype(float)

    require(
        np.count_nonzero(counts) >= 2,
        "Frequency evidence is too concentrated for this comparison.",
    )

    used = counts > 0
    total = len(groups)

    def nll(mu, alpha=0.0):
        logs = count_log_probabilities(mu, alpha)

        if not np.all(np.isfinite(logs[used])):
            return 1e100

        return -float(
            np.dot(counts[used], logs[used])
        )

    poisson = optimize.minimize_scalar(
        lambda value: nll(math.exp(value)),
        method="bounded",
        bounds=(-LOG_MU_GUARD, LOG_MU_GUARD),
        options=dict(
            xatol=1e-10,
            maxiter=1000,
        ),
    )

    require(
        poisson.success
        and abs(poisson.x) < LOG_MU_GUARD - 0.01
        and poisson.fun < 1e90,
        "Poisson optimisation failed or reached a numerical guard.",
    )

    mu_p = math.exp(float(poisson.x))
    nll_p = float(poisson.fun)

    attempts = [
        dict(
            family="poisson",
            start_id=1,
            initial_alpha="",
            accepted=True,
            final_mu=mu_p,
            final_alpha=0.0,
            negative_log_likelihood=nll_p,
            message=str(poisson.message),
        )
    ]

    def objective(theta):
        log_mu, alpha = theta

        if (
            not np.all(np.isfinite(theta))
            or abs(log_mu) > LOG_MU_GUARD
            or not 0 <= alpha <= ALPHA_GUARD
        ):
            return 1e100

        return nll(
            math.exp(float(log_mu)),
            float(alpha),
        )

    successes = []

    for start_id, initial_alpha in enumerate(
        (0.0, 0.1, 1.0), 1
    ):
        result = optimize.minimize(
            objective,
            [math.log(mu_p), initial_alpha],
            method="Powell",
            bounds=(
                (-LOG_MU_GUARD, LOG_MU_GUARD),
                (0, ALPHA_GUARD),
            ),
            options=dict(
                maxiter=3000,
                xtol=1e-10,
                ftol=1e-12,
            ),
        )

        ok = bool(
            result.success
            and math.isfinite(float(result.fun))
            and result.fun < 1e90
            and abs(result.x[0]) < LOG_MU_GUARD - 0.01
            and result.x[1] < ALPHA_GUARD - 0.01
        )

        attempts.append(
            dict(
                family="negative_binomial",
                start_id=start_id,
                initial_alpha=initial_alpha,
                accepted=ok,
                final_mu=(
                    math.exp(float(result.x[0]))
                    if ok
                    else ""
                ),
                final_alpha=(
                    float(result.x[1]) if ok else ""
                ),
                negative_log_likelihood=(
                    float(result.fun) if ok else ""
                ),
                message=str(result.message),
            )
        )

        if ok:
            successes.append(
                (float(result.fun), result.x)
            )

    require(
        len(successes) >= 2,
        "Too few successful negative-binomial optimiser starts.",
    )

    best_nll, theta = min(
        successes,
        key=lambda value: value[0],
    )

    require(
        best_nll <= nll_p + NLL_TOLERANCE,
        "NB search failed to reproduce its nested Poisson limit.",
    )

    limit = nll_p - best_nll <= NLL_TOLERANCE
    target_nll = nll_p if limit else best_nll

    require(
        sum(
            abs(value[0] - target_nll) <= NLL_TOLERANCE
            for value in successes
        ) >= 2,
        (
            "Negative-binomial optimiser starts disagree; "
            "review the likelihood search."
        ),
    )

    mu_nb = (
        mu_p
        if limit
        else math.exp(float(theta[0]))
    )
    alpha_nb = (
        0.0 if limit else float(theta[1])
    )

    candidates, comparison = [], []

    configurations = (
        (
            "poisson",
            mu_p,
            0.0,
            1,
            "interior_candidate",
            True,
        ),
        (
            "negative_binomial",
            mu_nb,
            alpha_nb,
            2,
            (
                "poisson_limit_at_numeric_resolution"
                if limit
                else "interior_candidate"
            ),
            not limit,
        ),
    )

    for (
        family,
        mu,
        alpha,
        parameters,
        status,
        rankable,
    ) in configurations:
        logs = count_log_probabilities(mu, alpha)
        probabilities = np.exp(logs)

        require(
            np.all(np.isfinite(logs[used]))
            and math.isclose(
                float(probabilities.sum()),
                1,
                abs_tol=1e-10,
                rel_tol=0,
            ),
            "Count probabilities do not form a complete distribution.",
        )

        ll = float(
            np.dot(counts[used], logs[used])
        )

        require(
            math.isclose(
                -ll,
                (
                    nll_p
                    if family == "poisson" or limit
                    else best_nll
                ),
                abs_tol=1e-6,
                rel_tol=0,
            ),
            "Saved count likelihood did not reproduce.",
        )

        shares = counts / total
        deviance = float(
            2 * np.dot(
                counts[used],
                np.log(shares[used]) - logs[used],
            )
        )

        require(
            deviance >= -1e-8,
            "Unexpected negative grouped deviance.",
        )

        candidates.append(
            dict(
                family=family,
                fit_status=status,
                n_owners_used=total,
                mean_usual_week_count=mu,
                variance_usual_week_count=(
                    mu + alpha * mu * mu
                ),
                dispersion_alpha=alpha,
                size_r=(
                    1 / alpha if alpha > 0 else ""
                ),
                success_probability=(
                    1 / (1 + alpha * mu)
                    if alpha > 0
                    else ""
                ),
                n_family_parameters=parameters,
                log_likelihood=ll,
                aic=(
                    2 * parameters - 2 * ll
                    if rankable
                    else None
                ),
                bic=(
                    parameters * math.log(total) - 2 * ll
                    if rankable
                    else None
                ),
                separately_ranked=rankable,
                binned_deviance=max(0.0, deviance),
                binned_total_variation=float(
                    0.5 * np.abs(
                        shares - probabilities
                    ).sum()
                ),
                max_share_error_percentage_points=float(
                    100 * np.abs(
                        shares - probabilities
                    ).max()
                ),
            )
        )

        for index, (lo, hi) in enumerate(COUNT_BINS):
            comparison.append(
                dict(
                    family=family,
                    count_bin=LABELS[index],
                    integer_min=lo,
                    integer_max=(
                        hi if hi is not None else ""
                    ),
                    observed_owners=int(counts[index]),
                    n_informative_owners=total,
                    empirical_probability=float(shares[index]),
                    fitted_probability=float(probabilities[index]),
                    expected_owners=float(
                        total * probabilities[index]
                    ),
                    reported_minus_fitted_percentage_points=float(
                        100 * (
                            shares[index]
                            - probabilities[index]
                        )
                    ),
                )
            )

    lowest = min(
        row["aic"]
        for row in candidates
        if row["separately_ranked"]
    )

    for row in candidates:
        row["delta_aic"] = (
            row["aic"] - lowest
            if row["separately_ranked"]
            else None
        )

    return candidates, comparison, attempts


def make_figure(output, candidates, comparison, retained):
    counts = [
        row["observed_owners"]
        for row in comparison
        if row["family"] == "poisson"
    ]
    total = sum(counts)

    with plt.rc_context(
        {
            "font.family": "DejaVu Sans",
            "font.size": 10,
            "svg.fonttype": "none",
        }
    ):
        fig, ax = plt.subplots(
            figsize=(9.5, 5.2)
        )

        x = np.arange(len(COUNT_BINS))
        width = 0.25

        ax.bar(
            x - width,
            100 * np.array(counts) / total,
            width,
            color="#50565C",
            label="Reported",
        )

        for index, candidate in enumerate(candidates):
            values = [
                row["fitted_probability"]
                for row in comparison
                if row["family"] == candidate["family"]
            ]

            label = (
                "Poisson"
                if index == 0
                else "NB (Poisson limit)"
                if not candidate["separately_ranked"]
                else "Negative binomial"
            )

            ax.bar(
                x + index * width,
                100 * np.array(values),
                width,
                label=label,
                color=("#C87924", "#24729B")[index],
            )

        ax.set_xticks(x, LABELS)
        ax.set(
            xlabel="Reported usual-week count category",
            ylabel="Share of informative owner reports (%)",
            title="Weekly-frequency candidate comparison",
        )
        ax.grid(axis="y", alpha=0.2)
        ax.set_axisbelow(True)
        ax.spines[["top", "right"]].set_visible(False)
        ax.legend(frameon=False)

        fig.text(
            0.5,
            0.025,
            (
                f"SYNTHETIC inputs | "
                f"Informative owners: {total}/{retained} | "
                "Usual-week count interpretation; "
                "empirical validation pending"
            ),
            ha="center",
            fontsize=9,
        )

        fig.tight_layout(rect=(0, 0.07, 1, 1))

        for extension in ("png", "svg"):
            fig.savefig(
                output / (
                    "weekly_frequency_candidates."
                    + extension
                ),
                dpi=180,
                facecolor="white",
            )

        plt.close(fig)


def build_fits(evidence_folder, project_root=ROOT):
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
        "src/ev_demand/parameters/fit_weekly_frequency_candidates.py",
    ):
        watch(name)

    folder = (root / evidence_folder).resolve()
    require(
        folder.is_relative_to(root / "data/processed/synthetic")
        and folder.is_dir(),
        "Use the completed synthetic population-evidence folder.",
    )

    spec = json.loads(
        watch(folder / "behaviour_population_spec.json").read_text(
            encoding="utf-8-sig"
        )
    )

    require(
        spec.get("schema_version")
        == "behaviour_population_evidence_v1"
        and spec.get("status") == "evidence_prepared"
        and spec.get("data_origin") == "SYNTHETIC",
        "Unexpected population-evidence specification.",
    )

    require(
        bool(spec.get("checks"))
        and all(
            value is True
            for value in spec["checks"].values()
        ),
        "An upstream population-evidence check did not pass.",
    )

    expected = {
        "numeric_evidence.csv",
        "numeric_coverage.csv",
        "context_windows.csv",
        "categorical_answer_counts.csv",
        "linked_owner_evidence.jsonl",
    }

    require(
        set(spec["output_sha256"]) == expected,
        "Unexpected evidence manifest.",
    )

    for name in sorted(expected):
        path = watch(folder / name)
        require(
            watched[path] == spec["output_sha256"][name],
            "Evidence export hash mismatch: " + name,
        )

    donors = [
        json.loads(line)
        for line in (
            folder / "linked_owner_evidence.jsonl"
        ).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    ids = {row["owner_id"] for row in donors}
    require(
        len(donors) == len(ids) == spec["owners_retained"]
        and "" not in ids,
        "Invalid linked-owner accounting.",
    )

    all_rows = baseline.read_csv(
        folder / "numeric_evidence.csv"
    )
    require(
        len(all_rows) == spec["numeric_evidence_rows"],
        "Numeric evidence row count mismatch.",
    )

    rows = [
        row
        for row in all_rows
        if row["parameter_id"] == "sessions_per_week"
        and row["legacy_context"] == ""
    ]

    require(
        len(rows) == len(ids)
        and {row["owner_id"] for row in rows} == ids
        and all(
            row["data_origin"] == "SYNTHETIC"
            for row in rows
        ),
        "Incomplete frequency-owner coverage.",
    )

    sources = {
        row["owner_id"]: row["owner_features"]
        for row in donors
    }

    for row in rows:
        for field, source_field in (
            ("raw_min", "sessions_per_week_min"),
            ("raw_max", "sessions_per_week_max"),
            ("raw_state", "sessions_per_week_state"),
            ("raw_boundary", "sessions_per_week_boundary"),
            ("raw_answer", "weekly_frequency_answer"),
        ):
            require(
                row[field]
                == sources[row["owner_id"]][source_field],
                "Frequency evidence differs from owner features.",
            )

    audit, groups = prepare_frequency(rows)
    candidates, comparison, attempts = fit_counts(groups)

    require(
        all(
            baseline.sha256(path) == old
            for path, old in watched.items()
        ),
        "Input changed during fitting.",
    )

    stamp = datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%S_%fZ"
    )
    output = root / "data/processed/synthetic" / (
        "weekly_frequency_candidates_" + stamp
    )
    output.mkdir(parents=True, exist_ok=False)

    for name, table in (
        ("weekly_frequency_evidence_audit.csv", audit),
        ("weekly_frequency_candidate_fits.csv", candidates),
        ("weekly_frequency_bin_comparison.csv", comparison),
        ("weekly_frequency_optimizer_attempts.csv", attempts),
    ):
        write_csv(output / name, table)

    make_figure(
        output,
        candidates,
        comparison,
        len(rows),
    )

    report = dict(
        schema_version="weekly_frequency_count_candidates_v1",
        status="candidates_for_review",
        data_origin="SYNTHETIC",
        parameter_id="sessions_per_week",
        owners_retained=len(rows),
        owners_used=len(groups),
        owners_excluded=len(rows) - len(groups),
        exclusion_reasons=dict(
            Counter(
                row["exclusion_reason"]
                for row in audit
                if not row["fit_included"]
            )
        ),
        target=(
            "Across-owner distribution of grouped reported usual-week "
            "charging counts, under a declared count interpretation"
        ),
        reviewed_count_bins=[
            dict(
                label=label,
                lower=lo,
                upper=hi,
            )
            for label, (lo, hi) in zip(LABELS, COUNT_BINS)
        ],
        candidates=candidates,
        lowest_aic_ranked_candidate=min(
            (
                row
                for row in candidates
                if row["separately_ranked"]
            ),
            key=lambda row: row["aic"],
        )["family"],
        assumptions=[
            (
                "One usual-frequency answer per owner is treated as a "
                "grouped nonnegative integer count for a representative week."
            ),
            (
                "This is a reporting-model assumption, not an observed "
                "calendar-week count series or a fitted distribution "
                "of continuous long-run owner rates."
            ),
            (
                "1 time or less covers counts 0 and 1; more than 7 covers "
                "8 and above, with no upper tail cap."
            ),
            (
                "Known response options with no informative reports have "
                "zero observed counts; unresolved owner answers remain "
                "excluded, not assigned zero."
            ),
            (
                "Category probabilities are fitted directly; no category "
                "midpoint or exact count is assigned to an owner."
            ),
            (
                "Negative-binomial variance is mu+alpha*mu^2; alpha=0 "
                "is the nested Poisson limit, not an independently "
                "identified NB candidate."
            ),
            (
                "At the Poisson limit, separate NB AIC/BIC ranking "
                "is omitted; the mean and likelihood are recorded "
                "as equivalent to Poisson."
            ),
            (
                "AIC and in-sample category residuals do not establish "
                "model adequacy, empirical validation or a complete "
                "population model."
            ),
            (
                "Missingness may be informative; the fitted cohort "
                "is conditional on owners supplying an informative "
                "usual-frequency answer."
            ),
            (
                "Across-owner report variation does not identify "
                "within-owner weekly variation, daily arrival counts "
                "or serial dependence."
            ),
            (
                "Continuous-rate reporting, completed-versus-attempted "
                "frequency and accepted-session reproduction remain "
                "simulator-integration review items."
            ),
        ],
        numerical_controls=dict(
            log_mean_guard=LOG_MU_GUARD,
            alpha_guard=ALPHA_GUARD,
            poisson_limit_nll_tolerance=NLL_TOLERANCE,
            note=(
                "Parameter search guards, not limits on possible weekly counts"
            ),
        ),
        software=dict(
            numpy=np.__version__,
            scipy=scipy.__version__,
            matplotlib=matplotlib.__version__,
        ),
        model_selected_for_simulation=False,
        parameter_uncertainty_estimated=False,
        goodness_of_fit_test_performed=False,
        owner_eligibility_changed=False,
        new_simulations=0,
        district_demand_ready=False,
        checks=dict(
            evidence_hashes_match=True,
            owner_links_match=True,
            integer_boundaries_respected=True,
            count_probabilities_sum_to_one=True,
            likelihoods_reproduced=True,
            optimiser_search_checked=True,
            owner_accounting_complete=True,
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

    require(
        all(
            baseline.sha256(path) == old
            for path, old in watched.items()
        ),
        "Input changed during export.",
    )

    (output / "weekly_frequency_fit_report.json").write_text(
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
    parser.add_argument("--evidence", required=True)
    args = parser.parse_args()

    output, report = build_fits(args.evidence)

    print("Weekly-frequency candidate fitting complete.")

    for key in (
        "owners_retained",
        "owners_used",
        "owners_excluded",
        "exclusion_reasons",
    ):
        print(f"{key}: {report[key]}")

    for row in report["candidates"]:
        aic = (
            f"{row['aic']:.3f}"
            if row["aic"] is not None
            else "not separately ranked"
        )

        print(
            f"{row['family']}: "
            f"status={row['fit_status']}, "
            f"mean={row['mean_usual_week_count']:.6f}, "
            f"alpha={row['dispersion_alpha']:.6f}, "
            f"AIC={aic}, "
            f"deviance={row['binned_deviance']:.4f}, "
            f"max_share_error="
            f"{row['max_share_error_percentage_points']:.3f} "
            "percentage points"
        )

    print(
        "All source-integrity, integer-boundary, likelihood "
        "and probability checks passed."
    )
    print("Figures: 1 PNG + 1 SVG")
    print(f"Output: {output.relative_to(ROOT)}")
    print(
        "Synthetic usual-week count candidates; reporting interpretation "
        "and simulator integration remain provisional."
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
            f"Weekly-frequency fitting failed: {error}",
            file=sys.stderr,
        )
        raise SystemExit(1)