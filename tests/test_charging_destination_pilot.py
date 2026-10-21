"""Boundary, allocation and provenance tests; all sessions here are test fixtures."""

import argparse
import copy
import csv
import importlib.util
import io
import itertools
import json
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "src/ev_demand/simulation/run_charging_destination_pilot.py"
SPEC = importlib.util.spec_from_file_location("charging_destination", SCRIPT)
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)
POLICY = ROOT / "config/synthetic/charging_destination_policy_v1.json"


def write_csv(path, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def make_session(start, end, power):
    limit = 672.0
    return {"start_hour_absolute": start, "end_hour_absolute": end, "active_duration_hours": end - start,
        "session_energy_kwh": (end - start) * power, "effective_average_power_kw": power,
        "energy_before_study_kwh": max(0, min(end, 0) - start) * power,
        "energy_in_study_kwh": max(0, min(end, limit) - max(start, 0)) * power,
        "energy_after_study_kwh": max(0, end - max(start, limit)) * power}


def fixture(base):
    """Create a linked 32-owner, ten-replicate test package, never real results."""
    geo, pilot, classes = base / "geo", base / "pilot", base / "classes"
    for folder in (geo, pilot, classes):
        folder.mkdir(parents=True)
    district_rows = [{"district_id": f"DCS-{i:02}", "district_name": f"Test district {i}"} for i in range(1, 26)]
    registry = base / "district_registry.csv"
    write_csv(registry, district_rows)
    owners = []
    for i in range(32):
        answer = m.ANSWERS[i % 4]
        named = f"DCS-{(i + 5) % 25 + 1:02}" if i % 4 in (2, 3) else ""
        owners.append({"owner_id": f"FIXTURE-{i:03}", "vehicle_class": "Car", "screened_cohort": "common_energy_cases",
            "in_saved_Car_profiles": "True", "data_origin": "SYNTHETIC", "home_district_id": f"DCS-{i % 25 + 1:02}",
            "away_geography_answer": answer, "outside_district_answer": named or "Not applicable",
            "named_outside_district_id": named, "outside_answer_status": "named_district" if named else "reported_not_applicable"})
    write_csv(geo / "owner_geography_resolved.csv", owners)
    replicates = tuple(str(i) for i in range(1, 11))
    selected = [{"owner_id": o["owner_id"], "replicate": r, "energy_case": "bounded_uniform_test", "screen_cohort": "common_energy_cases"}
                for r, o in itertools.product(replicates, owners)]
    write_csv(pilot / "selected_owner_parameters.csv", selected)
    sessions = []
    for r, g, o in itertools.product(replicates, m.GENERATORS, owners):
        windows = [("T2", -1, 2, 2.0)]
        if o["away_geography_answer"] != m.ANSWERS[0]:
            windows += [("T1", 23.5, 25.5, 3.0), ("T3", 670, 674, 4.0), ("T4", 100.25, 101, 5.0)]
        factor = (1 + int(r) / 100) * (1.2 if g == "availability_aware" else 1.0)
        for c, start, end, power in windows:
            sessions.append({"generator": g, "replicate": r, "owner_id": o["owner_id"],
                "session_id": f"{o['owner_id']}_{c}", "context": c, "output_origin": "SIMULATED",
                "behaviour_input_origin": "SYNTHETIC", "data_origin": "SYNTHETIC", **make_session(start, end, power * factor)})
    write_csv(pilot / "sessions.csv", sessions)
    reference = {(r, g, c, h): 0.0 for r, g, c, h in itertools.product(replicates, m.GENERATORS, m.ALL_CONTEXTS, range(24))}
    # Independent reference: integrate against every study hour, rather than
    # the production function's clipping and event-hour iteration.
    for row in sessions:
        for h in range(672):
            overlap = max(0, min(h + 1, row["end_hour_absolute"]) - max(h, row["start_hour_absolute"]))
            contribution = overlap * row["effective_average_power_kw"] / 28
            for c in (row["context"], "TOTAL"):
                reference[row["replicate"], row["generator"], c, h % 24] += contribution
    profiles = [{"replicate": r, "generator": g, "vehicle_class": "Car", "selected_owners": 32,
                 "context": c, "hour_of_day": h, "mean_power_kw": value} for (r, g, c, h), value in reference.items()]
    daily = [{"replicate": r, "generator": g, "vehicle_class": "Car", "selected_owners": 32,
              "context": c, "daily_energy_kwh": sum(reference[r, g, c, h] for h in range(24))}
             for r, g, c in itertools.product(replicates, m.GENERATORS, m.ALL_CONTEXTS)]
    write_csv(classes / "class_replicate_profiles.csv", profiles)
    write_csv(classes / "class_replicate_daily_energy.csv", daily)
    write_csv(classes / "class_owner_links.csv", [{"owner_id": o["owner_id"], "vehicle_class": "Car",
              "owner_replicate_selections": 10} for o in owners])
    write_csv(classes / "class_selection_review.csv", [{"owner_id": o["owner_id"], "replicate": r,
        "vehicle_class": "Car", "exceeds_reported_battery_upper": "" if i % 3 == 0 else "False",
        "calibration_status": "proxy_target_matched" if i % 4 else "zero_idle_proxy_below_target_review",
        "verification_within_development_band": "False" if i % 5 == 0 else "True",
        "output_origin": "SIMULATED", "behaviour_input_origin": "SYNTHETIC"}
        for i, (r, o) in enumerate(itertools.product(replicates, owners))])
    marker = base / "fixture_marker.txt"
    marker.write_text("TEST FIXTURE ONLY", encoding="utf-8")

    def save_report(folder, filename, data, inputs):
        data.update(input_sha256={m.relative(p): m.digest(p) for p in inputs},
                    output_sha256={p.name: m.digest(p) for p in folder.iterdir() if p.is_file()})
        (folder / filename).write_text(json.dumps(data), encoding="utf-8")

    settings = {"energy_case": "bounded_uniform_test", "power_scenario": "reduced_test", "cohort": "common_energy_cases",
                "replicates": 10, "study_days": 28, "warmup_days": 7}
    save_report(pilot, "availability_generator_report.json", {"schema_version": "availability_generator_comparison_v1",
        "status": "complete", "behaviour_input_origin": "SYNTHETIC", "output_origin": "SIMULATED", **settings,
        "owner_ids": [o["owner_id"] for o in owners], "checks": {"fixture_source_checked": True},
        "completed_frequency_agreement_established": False, "warmup_stationarity_established": False}, [marker])
    save_report(classes, "vehicle_class_profile_report.json", {"schema_version": "vehicle_class_profile_export_v1",
        "status": "complete", "source_pilot": m.relative(pilot), "class_counts_in_selected_cohort": {"Car": 32},
        **settings, "checks": {"fixture_class_checked": True}}, [pilot / "availability_generator_report.json"])
    save_report(geo, "charging_geography_resolved_report.json", {"schema_version": "charging_geography_resolved_v1",
        "status": "resolved", "data_origin": "SYNTHETIC", "review_flags": {"fixture_issues": 0},
        "owners_retained": 32, "profile_Car_owners": 32, "checks": {"fixture_geography_checked": True}},
        [registry, pilot / "availability_generator_report.json", classes / "vehicle_class_profile_report.json"])
    return argparse.Namespace(geography=str(geo), pilot=str(pilot), class_profiles=str(classes), policy=str(POLICY),
                              output_root=str(base / "out"), skip_figures=True)


class DestinationPilotTests(unittest.TestCase):
    def setUp(self):
        self.policy = m.read_json(POLICY)

    def test_policy_uses_explicit_fraction_cases_and_rejects_false_category_order(self):
        cases = m.policy_cases(self.policy)
        self.assertEqual(list(cases), [m.BASELINE, "lower_outside_fraction_test", "higher_outside_fraction_test"])
        wrong = copy.deepcopy(self.policy)
        wrong["cases"][0]["outside_fractions_by_answer"][m.ANSWERS[3]] = 0.2
        with self.assertRaisesRegex(ValueError, "contradict"):
            m.policy_cases(wrong)

    def test_unreported_outside_destination_stays_unresolved_and_home_stays_home(self):
        owner = {"owner_id": "A", "home_district_id": "DCS-11", "away_geography_answer": m.ANSWERS[1],
                 "named_outside_district_id": "", "outside_answer_status": "reported_not_applicable"}
        fractions = m.policy_cases(self.policy)["higher_outside_fraction_test"]
        self.assertEqual(m.allocation_rule(owner, "T1", fractions, {"DCS-11"}), [("DCS-11", 0.6), (m.UNRESOLVED, 0.4)])
        self.assertEqual(m.allocation_rule(owner, "T2", fractions, {"DCS-11"}), [("DCS-11", 1.0), (m.UNRESOLVED, 0.0)])

    def test_same_home_outside_and_unknown_answers_fail(self):
        owner = {"owner_id": "A", "home_district_id": "DCS-11", "away_geography_answer": m.ANSWERS[2],
                 "named_outside_district_id": "DCS-11", "outside_answer_status": "named_district"}
        with self.assertRaisesRegex(ValueError, "Invalid named outside"):
            m.allocation_rule(owner, "T3", m.policy_cases(self.policy)[m.BASELINE], {"DCS-11"})
        owner.update(named_outside_district_id="", outside_answer_status="reported_not_applicable", away_geography_answer="Unknown")
        with self.assertRaisesRegex(ValueError, "Unsupported geography answer"):
            m.allocation_rule(owner, "T3", m.policy_cases(self.policy)[m.BASELINE], {"DCS-11"})

    def test_boundary_and_midnight_energy_are_clipped_analytically(self):
        curve, _, _ = m.session_profile(make_session(-1, 2, 2), 28)
        self.assertAlmostEqual(curve[0], 2 / 28)
        self.assertAlmostEqual(curve[1], 2 / 28)
        self.assertAlmostEqual(sum(curve), 4 / 28)
        curve, _, _ = m.session_profile(make_session(23.5, 25.5, 4), 28)
        self.assertAlmostEqual(curve[23], 2 / 28)
        self.assertAlmostEqual(curve[0], 4 / 28)
        self.assertAlmostEqual(curve[1], 2 / 28)
        curve, _, _ = m.session_profile(make_session(671.5, 673, 2), 28)
        self.assertAlmostEqual(sum(curve), 1 / 28)
        self.assertAlmostEqual(curve[23], 1 / 28)

    def test_bad_energy_partition_and_missing_boolean_are_not_silently_accepted(self):
        row = make_session(0, 1, 2)
        row["energy_in_study_kwh"] = 3
        with self.assertRaisesRegex(ValueError, "energy_in_study"):
            m.session_profile(row, 28)
        self.assertIsNone(m.boolean("", optional=True))
        with self.assertRaisesRegex(ValueError, "Invalid boolean"):
            m.boolean("")

    def test_mcse_uses_replicates_and_keeps_zero_distinct_from_missing(self):
        average, mcse = m.mean_mcse([1, 3])
        self.assertEqual(average, 2)
        self.assertAlmostEqual(mcse, 1)
        self.assertEqual(m.mean_mcse([0, 0]), (0, 0))
        with self.assertRaisesRegex(ValueError, "two saved replicates"):
            m.mean_mcse([1])

    def test_end_to_end_balances_source_flags_unique_headers_and_hash_tampering(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tests", prefix="destination_fixture_") as tmp:
            args = fixture(Path(tmp))
            with redirect_stdout(io.StringIO()):
                output = m.run(args)
            report = m.Sources().package(output, "charging_destination_pilot_report.json")
            self.assertEqual(report["status"], "complete")
            self.assertEqual(report["conservation_checks"], 10000)
            self.assertLess(report["maximum_absolute_conservation_error"], 1e-10)
            self.assertEqual(report["source_Car_review_flags"]["missing_battery_upper_flags"], 107)
            self.assertEqual(report["new_routing_random_draws"], 0)
            for name in report["output_sha256"]:
                if name.endswith(".csv"):
                    with (output / name).open(encoding="utf-8-sig", newline="") as stream:
                        headers = next(csv.reader(stream))
                    self.assertEqual(len(headers), len(set(headers)), name)
            summary = list(m.read_csv(output / "routing_summary.csv", ("allocation_case", "context")))
            for generator in m.GENERATORS:
                total = [r for r in summary if r["generator"] == generator and r["context"] == "TOTAL"]
                self.assertEqual(len({r["cohort_mean_daily_energy_kwh"] for r in total}), 1)
                self.assertGreater(float(total[1]["unresolved_outside_mean_daily_energy_kwh"]), 0)
                self.assertGreater(float(total[2]["unresolved_outside_mean_daily_energy_kwh"]), float(total[1]["unresolved_outside_mean_daily_energy_kwh"]))
            (Path(args.pilot) / "sessions.csv").write_text("changed", encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(ValueError, "Source hash mismatch"):
                    m.run(args)


if __name__ == "__main__":
    unittest.main()
