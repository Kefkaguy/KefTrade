import { AssetLink, BarList, Card, DataTable, LineChart, MetricCard, PageTitle, Timeline } from "@/components/ResearchUI";
import { DataActions } from "@/components/DataActions";
import { getCandles, getSignal } from "@/lib/api";
import { assets, equityCurve, hypotheses, journalEntries, regimeRows, strategies } from "@/lib/research-data";
import { money } from "@/lib/format";

export default async function DashboardPage() {
  const candles = await getCandles(120).catch(() => []);
  const signal = await getSignal().catch(() => null);
  const latest = candles.at(-1);
  const rejected = strategies.filter((strategy) => strategy.recommendation === "Reject").length;
  const researchMore = strategies.filter((strategy) => strategy.recommendation === "Research More").length;

  return (
    <div className="pageStack">
      <PageTitle
        title="Research Dashboard"
        description="A stock-first quantitative research workspace showing validation status, active hypotheses, and evidence-backed next steps."
        actions={<DataActions />}
      />

      <section className="heroPanel">
        <div>
          <span className="sectionLabel">Today</span>
          <h2>Evidence gates are working. No weak strategy is promoted.</h2>
          <p>
            Latest research continues to reject unstable deterministic strategies. The next productive work is hypothesis-driven equity analysis,
            not forced optimization.
          </p>
        </div>
        <div className="heroMetrics">
          <MetricCard label="Latest BTC close" value={latest ? money(latest.close) : "No candles"} detail="Development dataset" />
          <MetricCard label="Current signal" value={signal?.signal ?? "No signal"} detail="Read-only research signal" tone="warning" />
        </div>
      </section>

      <div className="metricGrid">
        <MetricCard label="Total experiments" value="61" detail="Backend test coverage mirrors research modules" />
        <MetricCard label="Validated strategies" value="0" detail="Evidence gates preserved" tone="success" />
        <MetricCard label="Rejected strategies" value={rejected} detail="Correctly rejected" tone="error" />
        <MetricCard label="Research More" value={researchMore} detail="Needs stronger evidence" tone="warning" />
      </div>

      <div className="dashboardGrid">
        <Card title="Validation history" eyebrow="Evidence">
          <LineChart values={equityCurve} label="Validation history equity curve summary" />
        </Card>
        <Card title="Regime pressure" eyebrow="Market Intelligence">
          <BarList rows={regimeRows} />
        </Card>
      </div>

      <div className="dashboardGrid wideLeft">
        <Card title="Latest experiments" eyebrow="Research">
          <DataTable
            columns={["Strategy", "Version", "Recommendation", "Failure analysis"]}
            rows={strategies.slice(0, 5).map((strategy) => [
              strategy.name,
              strategy.version,
              <span className={`status ${strategy.recommendation === "Reject" ? "avoid" : "watchlist"}`} key={strategy.name}>
                {strategy.recommendation}
              </span>,
              strategy.failure
            ])}
          />
        </Card>
        <Card title="Active hypotheses" eyebrow="Workspace">
          <div className="hypothesisList">
            {hypotheses.map((hypothesis) => (
              <article key={hypothesis}>
                <span />
                <p>{hypothesis}</p>
              </article>
            ))}
          </div>
        </Card>
      </div>

      <div className="dashboardGrid">
        <Card title="Research progress" eyebrow="Archive">
          <Timeline items={journalEntries.slice(0, 3)} />
        </Card>
        <Card title="Assets under study" eyebrow="Coverage">
          <div className="assetGrid">
            {assets.map((asset) => (
              <AssetLink key={asset.symbol} symbol={asset.symbol} />
            ))}
          </div>
        </Card>
      </div>
    </div>
  );
}
