"""Research Engine labs.

The Research Engine (campaign orchestration, simulation/backtesting,
validation gates, and the candidate/elite lifecycle -- currently living in
`app.services.research_campaigns`, `app.services.family_registry`, and
related modules) is shared across every research style. A "lab" supplies
only the behavior specific to one style of trading -- what features it needs,
what strategy structures it generates, and what validation rules are unique
to it -- and calls back into the shared engine for everything else
(campaign lifecycle, worker claiming, the honest elite gate, family
registry auditing, repair).

The default/swing research behavior that already exists is not being moved
into a `labs/swing/` package as part of adding this namespace -- that would
be a large, risky refactor of working code for no functional benefit right
now. `labs/` currently holds only genuinely new lab-specific code
(`labs/intraday/`); existing modules remain where they are.
"""
