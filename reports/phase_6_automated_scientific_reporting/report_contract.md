# Report Contract

- Every completed campaign finalization calls the Phase 6 scientific report generator.
- Manual report regeneration uses the same deterministic report path.
- Reports are stored in the existing `research_campaign_reports` table.
- Reports use explicit evidence references and say `Inconclusive — insufficient evidence.` when support is missing.
- Post-hoc hypotheses are not reported as confirmed without independent future frozen validation.
- Validation policy remains unchanged.