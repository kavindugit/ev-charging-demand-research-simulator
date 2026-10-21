# Fleet scenario input contract

The JSON Schema describes COMPLETED inputs. The CSV/JSON templates are
deliberately incomplete and will not pass full readiness validation.
Blank numbers are unresolved, not zero. No baseline, adoption rate,
district share, vehicle scope or integerization method is activated.

1. Define vehicle/powertrain scope and its compatibility with the behavioural model.
2. Choose supplied national counts OR conditional penetration; document count basis.
3. Supply 30 national year/scenario rows with evidence and any required assumptions.
4. Supply all 750 district shares; shares across 25 districts must sum to one per year/scenario.
5. Complete the assumption register and verify every evidence/assumption reference.
6. Implement/run both schema validation and the aggregate semantic checks before activation.

The seven public references retain historical DMT values and their source
selectors. They do not establish an active EV baseline or future values.
No example shares or growth rates are copied from the original plan.
District fleet shares are not within-district place-typology weights or
away-charging destination shares. Those mappings are separate downstream work.

Planned scope: 25 districts x 10 years x 3 scenarios = 750 configurations.
Four downstream place groups would produce 3,000 profile groups. These
are planned output groups, not simulations or independent populations;
replicates are an additional experimental dimension selected separately.
