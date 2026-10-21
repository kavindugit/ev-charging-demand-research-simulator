# Vehicle-class profile export

Source pilot: data/simulated/synthetic/availability_generator_pilot_20261020T045820_640160Z

Study: 28 days; warm-up: 7 days; replicates: 10; selected owners: 60.

Vehicle classes are the original survey labels. They have not been verified as DMT administrative classes or powertrain groups.

Profiles use saved accepted sessions and their effective average power as the existing constant-power test proxy. Energy is clipped to study hours [0, 24 * study_days). Warm-up carry-in and study-end carry-out are accounted for. Rejected candidates contribute no load.

Each class denominator includes every selected owner in that class, including owners with no study-period charging. Classes with no selected owners receive coverage records without demand estimates.

The 24 profile values are average kW in each one-hour time-of-day bin across the study days. Their sum, multiplied by one hour, equals mean daily energy in kWh.

MCSE is computed across saved replicate results, conditional on the source model and screened parameter bank. It is not an empirical population confidence interval. MCSE is blank when only one replicate is available. Class means add; class MCSEs must not be added. Joint replicate outputs are retained for covariance-aware calculations.

Generator differences pair saved replicate/owner selections. They do not establish common candidate-event pairing.

Battery-upper flags retain True, False and missing as separate states. A missing flag does not establish a passed comparison. Missing flags remain blank in the exported CSV and are counted in the report.

Calibration, verification, battery-review and power-evidence flags are retained. Exporting a class does not establish vehicle-limit or connector compatibility. Per-study-vehicle profiles do not establish nationally representative class behaviour.

No simulations, random draws, fitting, source corrections, owner exclusions, fleet scaling or empirical validation were performed. Original sources and results were not changed.
