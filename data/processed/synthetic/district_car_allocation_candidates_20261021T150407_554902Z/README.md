# Conditional district Car allocation comparisons

DCS population counts are public reference data. The two allocation rules and national Car share paths are study assumptions. Neither case measures district EV ownership. Each case contains 750 configurations; together they contain 1500 candidate rows.

Population shares assume equal EVs per resident. Equal-district shares are a benchmark. Shares are fixed across 2026-2035. Counts describe a conditional owner-residence proxy, not charging destinations. Behaviour contexts and geographic place types are not allocated here.

Exact rational shares conserve each supplied national count. CSV district counts are rounded to six decimal places; small signed residuals are retained in allocation_balance_checks.csv. No integer vehicles or new virtual owners are generated.

No fleet contract or production weights are activated. No demand simulations, parameter draws, fitting or AI training occur. National scenarios remain drafts; Car registration-class and powertrain transfer assumptions remain unverified. The recorded acquisition hash checks snapshot consistency, not an independently published checksum.
