# PP1 research stopping point

The stopping point is a reproducible synthetic-behaviour research prototype with
verified accounting, retained model flags and explicit allocation assumptions.

## Finish the bounded pilot

1. Run run_charging_destination_pilot.py with the three frozen source folders in
   the supplied PowerShell command.
2. Confirm that the console says Conditional charging-destination pilot complete.
3. Open PP1_RESULTS.md and charging_destination_pilot_report.json in the new output
   directory. Confirm status complete and all checks true.
4. Inspect destination_allocation_overview.png. Cohort curves should overlap while
   located district allocation changes. Outside unresolved must remain visible.
5. Keep the original calibration and verification flags in the interpretation.

If a source hash check fails, preserve the source files and share the exact error.
Do not bypass verification or overwrite upstream files to force completion.
If only Matplotlib is unavailable, rerun with --skip-figures to retain the checked
tables and generated results note without installing new dependencies under time
pressure.

## Freeze for the viva

After reviewing the actual run, stop adding modelling features. Save the working
code, policy, generated report, results note and figure with the repository.
Review git status and staged file sizes before committing. Do not blindly stage
every raw public download or every previous simulation output.

The small code/policy/document change can be committed after running and checking:

    git add -- src/ev_demand/simulation/run_charging_destination_pilot.py config/synthetic/charging_destination_policy_v1.json tests/test_charging_destination_pilot.py docs/pp1_charging_destination_assumptions.md docs/pp1_viva_preparation.md docs/pp1_research_freeze.md
    git --no-pager diff --cached --stat
    git commit -m "Add conditional charging destination pilot and PP1 preparation guide"

Keep a separate copy of the generated output folder for the demonstration.
It can be large because replicate-level profiles are retained for auditability.
Record its full path, source hashes and report. The generated reports contain
readiness flags and should travel with the figures and demo bundle.

## A short demonstration

1. Explain the research question and show the pipeline.
2. Show an original answer, its encoded evidence and an owner-parameter example.
3. Explain the control/availability generator comparison and its source flags.
4. Show a 24-hour profile, its peak and daily energy with the experiment's owner
   count, seed settings and replicate count.
5. Show the destination-allocation policy, comparison figure and conservation
   report. Explain unresolved outside energy and the original 32-owner scope.
6. Finish with the real-data collection, refitting, validation and remaining
   geographic/forecast-model work.

Choose the length of this demonstration to fit the panel's allocated time.
Use computed outputs from the actual run; the package's automated-test fixture
data are for verification and must never be shown as research results.
