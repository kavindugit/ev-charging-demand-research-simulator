# Residence-attributed conditional Car profiles

Calculation: district expected Car count times saved mean power per study Car. The Car denominator includes all selected Car owners. Source registration-cohort profiles are divided by their reference count to recover the per-Car basis; the 2025 registration count is not used as future stock.

Two allocation comparisons cover 750 district/year/scenario configurations each. Each is evaluated with three energy cases, two power cases and two generators. The 18000 wide TOTAL profile rows are deterministic transfers of the existing simulations, not additional Monte Carlo replicates. National profiles occur once per year/scenario/energy/power/generator because the two allocation cases conserve the same national totals.

Exact rational count weights are used for calculation. Six-decimal district counts are display values; the index retains exact numerator/denominator pairs. Each mean_H column is average kW over that one-hour bin; summing the 24 columns gives mean daily kWh. Peaks are peaks of mean profiles, not distributions of daily peak demand.

TOTAL includes T1-T4 behavioural contexts. The four context daily energies are exported alongside TOTAL profiles. For any context/hour, multiply its per_car_mean_profiles_24h.csv value by the exact district count in district_profile_index.csv. Geographic P1-P4 place types are not assigned by this calculation. Residence attribution does not locate charging: owners can charge outside their home district.

Both scheduling methods and all energy cases remain comparisons. Calibration and battery-review flags remain in the hash-bound source_car_selection_review.csv. No new eligibility decisions are made. Study-class and powertrain transfer assumptions remain unverified.

Scaled source MCSE describes finite-replicate uncertainty conditional on the saved synthetic cohort, model and parameter bank. It excludes survey representativeness, fitted-parameter estimation, fleet-count, allocation-rule and real-world prediction uncertainty. Districts share the same replicate basis. National MCSE is checked using aggregated replicate vectors; districts are not treated as independent. Context MCSEs must not be added. No fleet prediction intervals or P05/P95 day-level bands are created.

Within each energy/power/generator case, normalized hourly shape is fixed across district, year and adoption scenario by construction. This is a scaling baseline. Its copies do not provide independent training examples or evidence that a neural model learns changing behaviour. No new sessions, owners, parameter draws, demand simulations or AI training occur; fleet inputs and geographic production weights remain unactivated.
