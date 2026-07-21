import { BarList, Card, DataTable, EmptyState, MetricCard, PageTitle } from "@/components/ResearchUI";
import { getEliteDeploymentAudit, getPortfolioReadiness, getStrategyDiagnosticsSummary } from "@/lib/api";

export const revalidate = 30;

export default async function DiagnosticsPage() {
  const [summaryResult, auditResult, portfolioResult] = await Promise.allSettled([
    getStrategyDiagnosticsSummary(),
    getEliteDeploymentAudit(),
    getPortfolioReadiness()
  ]);
  const summary = summaryResult.status === "fulfilled" ? summaryResult.value : null;
  const audit = auditResult.status === "fulfilled" ? auditResult.value : null;
  const portfolio = portfolioResult.status === "fulfilled" ? portfolioResult.value : null;

  return (
    <div className="pageStack">
      <PageTitle title="Trade Generation Diagnostics" description="Explainable strategy gates, elite deployment coverage, model-bounded risk, and portfolio readiness." />

      <div className="compactMetrics">
        <MetricCard label="Evaluations" value={summary?.evaluated ?? "Unavailable"} detail="Latest stored decisions, capped at 5,000" />
        <MetricCard label="Setup frequency" value={summary ? `${(summary.setup_frequency * 100).toFixed(2)}%` : "Unavailable"} detail={summary?.most_common_rejection ? `Top rejection: ${summary.most_common_rejection}` : "No dominant rejection yet"} tone={summary?.setup_frequency === 0 ? "warning" : "neutral"} />
        <MetricCard label="Strategy health" value={summary?.health.label.replaceAll("_", " ") ?? "Unavailable"} detail={summary ? `Evidence score ${summary.health.score}/100` : "Diagnostics API unavailable"} tone={summary?.health.label === "healthy" ? "success" : "warning"} />
      </div>

      <div className="dashboardGrid wideLeft">
        <Card title="Most common failed gates" eyebrow="Why no trade">
          {summary?.failed_gates.length ? (
            <BarList rows={summary.failed_gates.slice(0, 10).map((gate) => ({ label: gate.code, value: gate.count, meta: `${(gate.rate * 100).toFixed(1)}%` }))} />
          ) : <EmptyState title="No failed gates stored" body="Run the shadow evaluator to begin collecting structured rejection evidence." />}
        </Card>
        <Card title="Portfolio safety" eyebrow="Calculated risk">
          <div className="compactMetrics twoColumns">
            <MetricCard label="Current heat" value={portfolio ? `${Number(portfolio.portfolio_heat_pct) * 100}%` : "Unavailable"} />
            <MetricCard label="Heat limit" value={portfolio ? `${Number(portfolio.heat_limit_pct) * 100}%` : "Unavailable"} />
            <MetricCard label="Same symbol" value={portfolio?.same_symbol_limit ?? "Unavailable"} detail="First-ranked strategy wins" />
            <MetricCard label="Correlation cap" value={portfolio?.correlation_limit ?? "Unavailable"} />
          </div>
        </Card>
      </div>

      <Card title="Elite deployment audit" eyebrow="Promotion coverage">
        {audit ? (
          <>
            <p className="muted">{audit.counts.elites} elites · {audit.counts.internal_deployments} internal deployments · {audit.counts.external_deployments} broker-paper records</p>
            <DataTable columns={["Elite", "Strategy", "Market", "Internal", "Broker paper", "Blockers"]} rows={audit.items.map((item) => [
              item.elite_id,
              `${item.strategy_name} ${item.strategy_version}`,
              `${item.symbol ?? "—"} ${item.timeframe ?? ""}`,
              item.internal_deployment_id ? `#${item.internal_deployment_id} ${item.internal_status ?? ""}` : "Missing",
              item.external_deployment_id ? `#${item.external_deployment_id} ${item.external_state ?? ""}` : "Not approved",
              item.blockers.length ? item.blockers.join(", ") : "None"
            ])} />
          </>
        ) : <EmptyState title="Deployment audit unavailable" body="The API or database migration is not ready yet." />}
      </Card>

      <Card title="Model authority boundary" eyebrow="Risk challenger">
        <p className="muted">The model can recommend enter, wait, or reject and propose risk up to the configured cap. It cannot change the symbol, strategy, stop, or target; it cannot bypass deterministic setup, broker reconciliation, duplicate-symbol, correlation, or portfolio-heat gates. Paper submission additionally requires both execution flags and explicit deployment approval. Live-money routing remains prohibited.</p>
      </Card>
    </div>
  );
}
