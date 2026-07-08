import Link from "next/link";
import { AssetLink, BarList, Card, DataTable, EmptyState, LineChart, MetricCard, PageTitle, Timeline } from "@/components/ResearchUI";
import { DataActions } from "@/components/DataActions";
import { getCandles, getSignal } from "@/lib/api";
import {
  barRows,
  countBy,
  displayRecommendation,
  displayAssetClass,
  getLiveResearchSnapshot,
  latestExperimentRows,
  statusClass,
  timelineItems,
  validationSeries
} from "@/lib/live-research";
import { money } from "@/lib/format";

export default async function DashboardPage() {
  const [candles, signal, snapshot] = await Promise.all([
    getCandles(120).catch(() => []),
    getSignal().catch(() => null),
    getLiveResearchSnapshot()
  ]);
  const latest = candles.at(-1);
  const recommendations = countBy(snapshot.archive, (row) => displayRecommendation(row.recommendation));
  const regimeRows = barRows(countBy(snapshot.archive.flatMap((row) => row.market_regimes), (value) => value), "No regimes");
  const strategyRows = latestExperimentRows(snapshot.archive, 6);
  const events = timelineItems(snapshot, 5);
  const latestConclusion = snapshot.intelligence?.confidence?.[0]?.conclusion;
  const latestValidation = snapshot.validationRuns.at(-1);

  return (
    <div className="pageStack">
      <PageTitle
        title="Research Command Center"
        description="Live research status, validation evidence, market data health, assets, timeline, and copilot-ready context."
        actions={<DataActions />}
      />

      <section className="heroPanel">
        <div>
          <span className="sectionLabel">Research Status</span>
          <h2>{latestConclusion || "No validated research conclusion yet."}</h2>
          <p>
            {snapshot.intelligence
              ? `${snapshot.intelligence.summary.evidence_item_count} evidence records, ${snapshot.intelligence.summary.validation_run_count} validation runs, and ${snapshot.intelligence.summary.recommendation_count} research recommendations are loaded from the backend.`
              : "Sync data, create hypotheses, and run validation to populate the research command center."}
          </p>
        </div>
        <div className="heroMetrics">
        <MetricCard label="Latest BTC close" value={latest ? money(latest.close) : "No candles"} detail="Live candles endpoint" />
          <MetricCard label="Current signal" value={signal?.signal ?? "No signal"} detail="Read-only research signal" tone="warning" />
          <MetricCard label="Latest validation" value={latestValidation ? `Run ${latestValidation.id}` : "None"} detail={latestValidation ? `${latestValidation.candidate_count} candidates` : "Run alpha validation"} />
        </div>
      </section>

      <div className="metricGrid">
        <MetricCard label="Total experiments" value={snapshot.intelligence?.summary.experiment_count ?? 0} detail="Strategy experiment records" />
        <MetricCard label="Validated strategies" value={recommendations["Validated Alpha"] ?? 0} detail="Passed evidence gates" tone="success" />
        <MetricCard label="Rejected strategies" value={recommendations.Reject ?? 0} detail="Rejected by validation" tone="error" />
        <MetricCard label="Research More" value={recommendations["Research More"] ?? recommendations["Needs More Research"] ?? 0} detail="Needs stronger evidence" tone="warning" />
      </div>

      <div className="dashboardGrid">
        <Card title="Validation history" eyebrow="Evidence">
          <LineChart values={validationSeries(snapshot.validationRuns)} label="Validation history score summary" />
        </Card>
        <Card title="Regime distribution" eyebrow="Market Intelligence">
          <BarList rows={regimeRows} />
        </Card>
      </div>

      <div className="dashboardGrid wideLeft">
        <Card title="Latest experiments" eyebrow="Research">
          {strategyRows.length ? (
            <DataTable
              columns={["Strategy", "Candidate", "Recommendation", "Trades", "Failure analysis"]}
              rows={strategyRows.map((row) => [
                row.strategy,
                row.candidate,
                <span className={`status ${statusClass(row.recommendation)}`} key={row.candidate}>
                  {row.recommendation}
                </span>,
                row.trades,
                row.failure
              ])}
            />
          ) : (
            <EmptyState
              title="No experiments yet."
              body="Run alpha discovery or validation to build the experiment archive."
              action={<Link className="button" href="/alpha">Open discovery lab</Link>}
            />
          )}
        </Card>
        <Card title="Active hypotheses" eyebrow="Workspace">
          {snapshot.hypotheses.length ? (
            <div className="hypothesisList">
              {snapshot.hypotheses.slice(0, 5).map((hypothesis) => (
                <article key={hypothesis.id}>
                  <span />
                  <p>{hypothesis.title}</p>
                </article>
              ))}
            </div>
          ) : (
            <EmptyState
              title="No hypotheses yet."
              body="Create your first research hypothesis."
              action={<Link className="button" href="/hypotheses">Create hypothesis</Link>}
            />
          )}
        </Card>
      </div>

      <div className="dashboardGrid">
        <Card title="Research progress" eyebrow="Archive">
          {events.length ? (
            <Timeline items={events} />
          ) : (
            <EmptyState
              title="No timeline yet."
              body="Research activity will appear here after hypotheses, experiments, or validation runs."
              action={<Link className="button" href="/hypotheses">Start research</Link>}
            />
          )}
        </Card>
        <Card title="Assets under study" eyebrow="Coverage">
          {snapshot.symbols.length ? (
            <div className="assetGrid">
              {snapshot.symbols.map((asset) => (
                <AssetLink key={asset.symbol} symbol={asset.symbol} label={`${asset.symbol} · ${displayAssetClass(asset.asset_class)}`} />
              ))}
            </div>
          ) : (
            <EmptyState title="No assets synced." body="Sync market data to register research assets." action={<Link className="button" href="/dashboard">Sync data</Link>} />
          )}
        </Card>
      </div>
    </div>
  );
}
