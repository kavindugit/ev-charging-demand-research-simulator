# Conditional electric-car registration-cohort calculation

The public input counts newly registered Electric motor cars in 2025. It does not count the active national electric fleet. The saved Car profiles are transferred under assumption TRANSFER-CAR-REGCOHORT-01. Survey-to-registration class and powertrain compatibility remain unverified.

Calculation: source Car profile / all selected Car owners * reference cohort count. Every selected Car owner remains in the denominator, including those with no charging during a study replicate. Contexts remain T1-T4 behavioural contexts. TOTAL is their sum.

Mean-profile peaks are maxima of ensemble mean curves. Their hours are not estimates of the distribution of individual daily peaks. MCSE is recomputed across each case's saved replicate vectors and scaled by the same constant. Context MCSEs are not added. This does not simulate independent vehicles in a new large fleet or establish fleet-level predictive intervals. Cases are not pooled.

The count reference year labels the registration cohort, not a historically reconstructed charging year. Battery missing states, calibration flags and verification flags are copied for the Car selections. Vehicle limits and connector compatibility are not established by this calculation. The separate extension cohort, SUV/crossover and other survey classes are not added.

No new random draws, simulations, fitted parameters, exclusions, 2026-2035 fleet counts, district shares or place allocations are created. This is a reference scaling check before future scenario inputs are chosen; empirical validation remains outstanding.
