"""Fit respondent-level usual-energy candidates with interval/censored likelihood."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import platform
import sys

import numpy as np
import scipy
from scipy import optimize, stats

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from ev_demand.parameters import build_simulator_parameters as baseline
from ev_demand.parameters.build_population_evidence import (
    interval_kind,
    require,
    write_csv,
)

FAMILIES = {
    "lognormal": stats.lognorm,
    "gamma": stats.gamma,
}

# Numerical parameter guard, never an energy cutoff.
LOG_GUARD = 30.0


def prepare_energy(rows):
    audit, intervals = [], []
    accepted = {
        "finite_interval",
        "point",
        "lower_bound_only",
        "upper_bound_only",
    }

    for row in rows:
        require(
            row["unit"] == "kWh" and row["reported_period"] == "",
            "Unexpected energy units.",
        )

        kind = interval_kind(
            row["raw_min"],
            row["raw_max"],
            row["raw_state"],
            "kWh",
        )
        require(
            kind == row["evidence_kind"],
            "Energy evidence classification did not reproduce.",
        )

        reason, likelihood = "", ""

        if kind not in accepted:
            reason = kind
        else:
            low = (
                float(row["raw_min"])
                if row["raw_min"].strip()
                else 0.0
            )
            high = (
                float(row["raw_max"])
                if row["raw_max"].strip()
                else math.inf
            )

            if high <= 0:
                reason = "nonpositive_energy_requires_review"
            elif low == 0 and math.isinf(high):
                reason = "uninformative_positive_support"
            else:
                intervals.append((low, high))
                likelihood = (
                    "point_density"
                    if low == high
                    else "right_censored"
                    if math.isinf(high)
                    else "left_censored"
                    if not row["raw_min"].strip()
                    else "interval_censored"
                )

        audit.append(
            dict(
                row,
                fit_included=not bool(reason),
                exclusion_reason=reason,
                likelihood_type=likelihood,
            )
        )

    return audit, intervals


def log_difference(log_a, log_b):
    # log(exp(log_a) - exp(log_b)).
    # expm1 retains precision when the two log probabilities are close.
    with np.errstate(
        divide="ignore",
        invalid="ignore",
        over="ignore",
    ):
        return log_a + np.log(-np.expm1(log_b - log_a))


def log_contributions(family, shape, scale, low, high):
    dist = FAMILIES[family]
    result = np.empty(len(low), dtype=float)

    point = low == high
    right = np.isinf(high)
    ranged = ~(point | right)

    result[point] = dist.logpdf(
        low[point], shape, loc=0, scale=scale
    )
    result[right] = dist.logsf(
        low[right], shape, loc=0, scale=scale
    )

    if np.any(ranged):
        lower, upper = low[ranged], high[ranged]
        upper_cdf = dist.logcdf(
            upper, shape, loc=0, scale=scale
        )

        use_cdf = upper_cdf < math.log(0.5)
        values = np.empty(len(lower), dtype=float)

        if np.any(use_cdf):
            values[use_cdf] = log_difference(
                upper_cdf[use_cdf],
                dist.logcdf(
                    lower[use_cdf],
                    shape,
                    loc=0,
                    scale=scale,
                ),
            )

        if np.any(~use_cdf):
            values[~use_cdf] = log_difference(
                dist.logsf(
                    lower[~use_cdf],
                    shape,
                    loc=0,
                    scale=scale,
                ),
                dist.logsf(
                    upper[~use_cdf],
                    shape,
                    loc=0,
                    scale=scale,
                ),
            )

        result[ranged] = values

    return result


def fit_candidates(intervals):
    require(
        len(intervals) >= 3,
        "Too few informative owners for two-parameter fitting.",
    )

    groups = Counter(intervals)
    require(
        len(groups) >= 2
        and any(math.isfinite(high) for low, high in groups),
        "Energy evidence does not constrain both candidate parameters.",
    )
    require(
        max(low for low, high in groups)
        > min(high for low, high in groups),
        (
            "Energy ranges share a common point; "
            "concentrated candidate parameters may be unidentified."
        ),
    )

    low = np.array([key[0] for key in sorted(groups)])
    high = np.array([key[1] for key in sorted(groups)])
    weights = np.array(
        [groups[key] for key in sorted(groups)],
        dtype=float,
    )

    # Starting scale only.
    # The likelihood uses complete intervals, not these endpoint values.
    anchor = float(
        np.median(
            [
                high if math.isfinite(high) else low
                for low, high in intervals
            ]
        )
    )
    require(anchor > 0, "No positive starting scale available.")

    candidates, attempts, checks = [], [], []

    for family, dist in FAMILIES.items():
        starts = (
            (0.4, 0.9, 1.5)
            if family == "lognormal"
            else (0.8, 2.0, 5.0)
        )

        def objective(theta):
            if (
                not np.all(np.isfinite(theta))
                or np.max(np.abs(theta)) >= LOG_GUARD
            ):
                return 1e100

            shape, scale = np.exp(theta)
            logs = log_contributions(
                family, shape, scale, low, high
            )

            if not np.all(np.isfinite(logs)):
                return 1e100

            return -float(np.dot(weights, logs))

        successful = []

        for start_id, initial_shape in enumerate(starts, 1):
            initial_scale = (
                anchor
                if family == "lognormal"
                else anchor / initial_shape
            )

            result = optimize.minimize(
                objective,
                np.log([initial_shape, initial_scale]),
                method="Nelder-Mead",
                options=dict(
                    maxiter=4000,
                    xatol=1e-8,
                    fatol=1e-8,
                ),
            )

            ok = (
                bool(result.success)
                and math.isfinite(float(result.fun))
                and result.fun < 1e90
                and np.max(np.abs(result.x)) < LOG_GUARD - 0.01
            )

            attempts.append(
                dict(
                    family=family,
                    start_id=start_id,
                    initial_shape=initial_shape,
                    initial_scale_kwh=initial_scale,
                    accepted=ok,
                    negative_log_likelihood=(
                        float(result.fun) if ok else ""
                    ),
                    iterations=int(result.nit),
                    message=str(result.message),
                )
            )

            if ok:
                successful.append(
                    (
                        float(result.fun),
                        start_id,
                        result.x,
                    )
                )

        require(
            bool(successful),
            "No interior converged candidate for " + family,
        )

        nll, best_start, theta = min(
            successful,
            key=lambda value: value[0],
        )
        shape, scale = map(float, np.exp(theta))

        agreement = sum(
            abs(value[0] - nll) <= 1e-6
            for value in successful
        )
        require(
            agreement >= 2,
            (
                "Optimizer starts disagree for "
                + family
                + "; review fitting before use."
            ),
        )

        logs = log_contributions(
            family, shape, scale, low, high
        )
        require(
            np.all(np.isfinite(logs))
            and math.isclose(
                -float(np.dot(weights, logs)),
                nll,
                abs_tol=1e-7,
            ),
            "Likelihood check failed.",
        )

        mean = float(dist.mean(shape, scale=scale))
        std = float(dist.std(shape, scale=scale))
        p05, median, p95 = map(
            float,
            dist.ppf(
                [0.05, 0.5, 0.95],
                shape,
                scale=scale,
            ),
        )

        require(
            all(
                math.isfinite(value) and value > 0
                for value in (mean, std, p05, median, p95)
            )
            and p05 <= median <= p95,
            "Nonfinite or invalid fitted summaries.",
        )

        candidates.append(
            dict(
                family=family,
                n_owners_used=len(intervals),
                n_free_parameters=2,
                shape=shape,
                scale_kwh=scale,
                fixed_location_kwh=0.0,
                mu_log_kwh=(
                    math.log(scale)
                    if family == "lognormal"
                    else ""
                ),
                log_likelihood=-nll,
                aic=4 + 2 * nll,
                bic=2 * math.log(len(intervals)) + 2 * nll,
                mean_kwh=mean,
                std_kwh=std,
                p05_kwh=p05,
                median_kwh=median,
                p95_kwh=p95,
                successful_starts=len(successful),
                agreeing_starts=agreement,
                selected_start=best_start,
            )
        )

        for index, (lower, upper) in enumerate(sorted(groups)):
            point = lower == upper
            checks.append(
                dict(
                    family=family,
                    lower_kwh=lower,
                    upper_kwh=(
                        upper if math.isfinite(upper) else ""
                    ),
                    n_owner_records=groups[(lower, upper)],
                    contribution_type=(
                        "density_per_kwh"
                        if point
                        else "probability"
                    ),
                    log_contribution=float(logs[index]),
                    contribution=float(math.exp(logs[index])),
                )
            )

    minimum = min(row["aic"] for row in candidates)
    for row in candidates:
        row["delta_aic"] = row["aic"] - minimum

    return candidates, attempts, checks


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
        "src/ev_demand/parameters/fit_usual_energy_candidates.py",
    ):
        watch(name)

    folder = (root / evidence_folder).resolve()
    require(
        folder.is_relative_to(root / "data/processed/synthetic")
        and folder.is_dir(),
        "Use a completed synthetic population-evidence folder.",
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
        "Expected the completed synthetic population-evidence specification.",
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
        "Unexpected evidence export manifest.",
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
        "Invalid linked-owner evidence IDs.",
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
        if row["parameter_id"] == "usual_energy_kwh"
        and row["legacy_context"] == ""
    ]

    require(
        len(rows) == len(ids)
        and {row["owner_id"] for row in rows} == ids
        and all(
            row["data_origin"] == "SYNTHETIC"
            for row in rows
        ),
        "Incomplete energy-owner coverage.",
    )

    source_owners = {
        row["owner_id"]: row["owner_features"]
        for row in donors
    }

    for row in rows:
        owner = source_owners[row["owner_id"]]

        for field, source_field in (
            ("raw_min", "usual_energy_kwh_min"),
            ("raw_max", "usual_energy_kwh_max"),
            ("raw_state", "usual_energy_kwh_state"),
            ("raw_boundary", "usual_energy_kwh_boundary"),
            ("raw_answer", "usual_energy_answer"),
        ):
            require(
                row[field] == owner[source_field],
                "Energy evidence differs from linked owner features.",
            )

    audit, intervals = prepare_energy(rows)
    candidates, attempts, checks = fit_candidates(intervals)

    require(
        all(
            baseline.sha256(path) == old
            for path, old in watched.items()
        ),
        "An input changed during fitting.",
    )

    stamp = datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%S_%fZ"
    )
    output = root / "data/processed/synthetic" / (
        "usual_energy_candidates_" + stamp
    )
    output.mkdir(parents=True, exist_ok=False)

    for name, table in (
        ("energy_evidence_audit.csv", audit),
        ("energy_candidate_fits.csv", candidates),
        ("optimizer_attempts.csv", attempts),
        ("likelihood_group_checks.csv", checks),
    ):
        write_csv(output / name, table)

    report = dict(
        schema_version="usual_energy_candidate_fits_v1",
        status="candidates_for_review",
        data_origin="SYNTHETIC",
        parameter_id="usual_energy_kwh",
        target=(
            "Between-owner distribution of reported usual session energy; "
            "one contribution per owner"
        ),
        conditioning_group=(
            "Pooled retained owners with informative usual-energy bounds"
        ),
        owners_retained=len(rows),
        owners_used=len(intervals),
        owners_excluded=len(rows) - len(intervals),
        included_likelihood_types=dict(
            Counter(
                row["likelihood_type"]
                for row in audit
                if row["fit_included"]
            )
        ),
        exclusion_reasons=dict(
            Counter(
                row["exclusion_reason"]
                for row in audit
                if not row["fit_included"]
            )
        ),
        method=(
            "Two-parameter positive continuous interval/censored maximum "
            "likelihood; location fixed at zero"
        ),
        lowest_aic_candidate=min(
            candidates,
            key=lambda row: row["aic"],
        )["family"],
        candidates=candidates,
        electrical_boundary=(
            "Reported kWh; meter-versus-battery boundary unresolved"
        ),
        software=dict(
            python=platform.python_version(),
            numpy=np.__version__,
            scipy=scipy.__version__,
        ),
        assumptions=[
            (
                "One owner-level usual-energy report is treated as a "
                "coarsened observation of a positive latent usual energy."
            ),
            (
                "These are marginal candidate fits; reporting intervals/"
                "censoring are assumed usable for this likelihood."
            ),
            (
                "Unresolved reports are excluded only from this fit; "
                "their owners remain in the evidence package."
            ),
            (
                "Missingness may be informative; fits do not identify "
                "the full population distribution without additional assumptions."
            ),
            (
                "Continuous interval endpoints have zero probability mass; "
                "exact positive points use probability densities."
            ),
            (
                "Lognormal shape is sigma and scale is exp(mu); "
                "Gamma shape is alpha and scale is theta in kWh."
            ),
            (
                "No midpoint substitution, imputation or energy-tail cap "
                "is applied."
            ),
            (
                "AIC/BIC are relative candidate diagnostics on the same "
                "owner evidence, not empirical validation."
            ),
            (
                "Fitted percentiles describe the candidate distribution, "
                "not confidence intervals or session-day extremes."
            ),
            (
                "Within-owner session variation, vehicle/battery compatibility "
                "and joint dependencies require separate modelling."
            ),
            (
                "No conversion losses are applied; "
                "the energy measurement boundary remains unresolved."
            ),
        ],
        optimizer=dict(
            method="Nelder-Mead",
            starts_per_family=3,
            agreement_tolerance_nll=1e-6,
            absolute_log_parameter_guard=LOG_GUARD,
            note=(
                "Numerical shape/scale guard; boundary solutions rejected; "
                "not an energy cap"
            ),
        ),
        model_selected_for_simulation=False,
        parameter_uncertainty_estimated=False,
        owner_eligibility_changed=False,
        new_simulations=0,
        district_demand_ready=False,
        checks=dict(
            evidence_hashes_match=True,
            energy_owner_links_match=True,
            likelihoods_finite_and_reproduced=True,
            multiple_optimizer_starts_agree=True,
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
        "An input changed during export.",
    )

    (output / "usual_energy_fit_report.json").write_text(
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

    print("Usual-energy candidate fitting complete.")

    for key in (
        "owners_retained",
        "owners_used",
        "owners_excluded",
        "included_likelihood_types",
        "exclusion_reasons",
    ):
        print(f"{key}: {report[key]}")

    for row in report["candidates"]:
        print(
            f"{row['family']}: "
            f"shape={row['shape']:.6f}, "
            f"scale={row['scale_kwh']:.6f} kWh, "
            f"mean={row['mean_kwh']:.3f} kWh, "
            f"AIC={row['aic']:.3f}, "
            f"delta_AIC={row['delta_aic']:.3f}"
        )

    print(
        f"lowest_aic_candidate: {report['lowest_aic_candidate']}"
    )
    print(
        "All evidence-integrity, likelihood "
        "and optimizer-agreement checks passed."
    )
    print(f"Output: {output.relative_to(ROOT)}")
    print(
        "Synthetic owner-level candidate fits; model selection, "
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
            f"Usual-energy fitting failed: {error}",
            file=sys.stderr,
        )
        raise SystemExit(1)