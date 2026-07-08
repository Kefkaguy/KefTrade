import { Card, DataTable, EmptyState, MetricCard, PageTitle } from "@/components/ResearchUI";
import { getPromisingResearchCandidates, type PromisingResearchCandidate } from "@/lib/api";
import { money, number, percent } from "@/lib/format";
import Link from "next/link";

export default async function PromisingCandidatesPage() {
  const report = await getPromisingResearchCandidates().catch(() => null);
  const candidates = report?.candidates.slice(0, 12) ?? [];
  const best = candidates[0];

  return (
    <div className="pageStack">
      <PageTitle
        title="Promising Research Candidates"
        description="Cross-asset, multi-timeframe, out-of-sample, and walk-forward evidence for research candidates. Research only; no execution."
      />

      {report ? (
        <>
          <div className="metricGrid">
            <MetricCard label="Candidates" value={String(report.summary.candidate_count ?? candidates.length)} detail="Bounded research ranking" />
            <MetricCard label="Datasets" value={String(report.summary.dataset_count ?? report.datasets.length)} detail="Assets and timeframes with features" />
            <MetricCard label="Validation ready" value={String(report.summary.validation_ready_count ?? 0)} detail="Candidate for formal validation only" tone="warning" />
            <MetricCard label="Top score" value={number(report.summary.top_score)} detail={String(report.summary.top_candidate ?? "No candidate")} />
          </div>

          <Card title="Best candidates" eyebrow="Robustness ranking">
            {candidates.length ? (
              <DataTable
                columns={["Rank", "Candidate", "Strategy", "Score", "PF", "Expectancy", "Trades", "DD", "Stability", "X-Asset", "OOS", "Validation status"]}
                rows={candidates.map((row) => [
                  row.rank,
                  <Link className="tableLink" href={`/candidates/${encodeURIComponent(row.candidate_id)}`} key={row.candidate_id}>{row.candidate_id}</Link>,
                  row.strategy_name,
                  number(row.research_score),
                  metric(row, "profit_factor"),
                  money(row.aggregate_metrics.expectancy_per_trade),
                  String(row.aggregate_metrics.number_of_trades ?? 0),
                  percent(row.aggregate_metrics.max_drawdown),
                  number(row.stability_score),
                  number(row.cross_asset_consistency),
                  number(row.out_of_sample_score),
                  <span className={`status ${statusTone(row.validation_status)}`} key={row.candidate_id}>{row.validation_status}</span>
                ])}
              />
            ) : (
              <EmptyState title="No candidates ranked." body="The backend did not return any research candidates with sufficient dataset coverage." />
            )}
          </Card>

          {best ? <CandidateEvidence candidate={best} /> : null}

          <Card title="Research report" eyebrow="Generated after experiment">
            <pre className="reportBlock">{best?.research_report ?? report.markdown_report}</pre>
          </Card>
        </>
      ) : (
        <EmptyState
          title="Promising candidates unavailable."
          body="Start the API and confirm candles/features are synced. This page is read-only and does not change validation thresholds."
        />
      )}
    </div>
  );
}

function CandidateEvidence({ candidate }: { candidate: PromisingResearchCandidate }) {
  return (
    <div className="dashboardGrid wideLeft">
      <Card title="Evidence summary" eyebrow={candidate.candidate_id}>
        <div className="evidenceSummary">
          <p>{candidate.evidence_summary}</p>
          <div>
            <strong>Recommended next experiment</strong>
            <p>{candidate.recommended_next_experiment}</p>
          </div>
          <div>
            <strong>Parameters</strong>
            <p>{Object.entries(candidate.parameters).map(([key, value]) => `${key}=${String(value)}`).join(", ") || "No changed parameters"}</p>
          </div>
        </div>
      </Card>
      <Card title="Worked vs failed" eyebrow="Cross-dataset behavior">
        <div className="assetOutcomeGrid">
          <div>
            <strong>Worked</strong>
            {candidate.assets_worked.length ? candidate.assets_worked.map((asset) => <span className="status setup" key={asset}>{asset}</span>) : <span className="status">None</span>}
          </div>
          <div>
            <strong>Failed</strong>
            {candidate.assets_failed.length ? candidate.assets_failed.map((asset) => <span className="status avoid" key={asset}>{asset}</span>) : <span className="status setup">None</span>}
          </div>
        </div>
      </Card>
    </div>
  );
}

function metric(row: PromisingResearchCandidate, key: string) {
  const value = row.aggregate_metrics[key];
  return value === null || value === undefined ? "N/A" : number(value);
}

function statusTone(status: string) {
  if (status.includes("alpha validation")) return "setup";
  if (status.includes("more evidence")) return "watchlist";
  return "avoid";
}
