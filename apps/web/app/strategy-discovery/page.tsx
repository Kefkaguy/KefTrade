import { Card, DataTable, EmptyState, MetricCard, PageTitle } from "@/components/ResearchUI";
import { StrategyDiscoveryActions } from "@/components/StrategyDiscoveryActions";
import { getStrategyDiscoveryDashboard, type StrategyDiscoveryRow } from "@/lib/api";

export default async function StrategyDiscoveryPage() {
  const dashboard = await getStrategyDiscoveryDashboard({ limit: 20 }).catch(() => null);

  if (!dashboard) {
    return (
      <section>
        <PageTitle title="Strategy Discovery" description="Autonomous research engine for generated deterministic strategy variants." actions={<StrategyDiscoveryActions />} />
        <EmptyState title="Strategy discovery unavailable." body="Run migrations, then start a discovery pass to populate generated strategy evidence." />
      </section>
    );
  }

  return (
    <section>
      <PageTitle
        title="Strategy Discovery"
        description="Generate, test, promote, retire, and evolve deterministic strategy variants from reusable research rule blocks."
        actions={<StrategyDiscoveryActions />}
      />

      <div className="metricGrid">
        <MetricCard label="Generated" value={dashboard.summary.generated} detail="Stored candidates" />
        <MetricCard label="Promoted" value={dashboard.summary.promoted} detail="Passed evidence gates" tone="success" />
        <MetricCard label="Rejected" value={dashboard.summary.rejected} detail="Learned failures" tone="warning" />
        <MetricCard label="Families" value={dashboard.summary.families} detail="Lineage groups" />
      </div>

      <Card title="Strongest Discoveries" eyebrow="Stored evidence">
        {dashboard.strongest_discoveries.length ? <DiscoveryTable rows={dashboard.strongest_discoveries} /> : <EmptyState title="No discoveries yet." body="Run the discovery engine to generate and validate deterministic strategies." />}
      </Card>

      <div className="grid two">
        <Card title="Newest Discoveries" eyebrow="Latest generated">
          {dashboard.newest_discoveries.length ? <DiscoveryTable rows={dashboard.newest_discoveries.slice(0, 8)} compact /> : <EmptyState title="No new discoveries." body="Generated candidates will appear here after a run." />}
        </Card>
        <Card title="Successful Rule Combinations" eyebrow="Promoted patterns">
          {dashboard.successful_rule_combinations.length ? (
            <DataTable
              columns={["Combination", "Count", "Best score"]}
              rows={dashboard.successful_rule_combinations.map((row) => [row.combination, row.count, format(row.best_score)])}
            />
          ) : <EmptyState title="No promoted combinations yet." body="Rule combinations are listed only after stored evidence promotes candidates." />}
        </Card>
      </div>

      <Card title="Evolution History" eyebrow="Family tree events">
        {dashboard.evolution_history.length ? (
          <DataTable
            columns={["Candidate", "Parent", "Event", "Created"]}
            rows={dashboard.evolution_history.map((row) => [
              String(row.candidate_id ?? ""),
              String(row.parent_candidate_id ?? "root"),
              String(row.event_type ?? ""),
              String(row.created_at ?? "")
            ])}
          />
        ) : <EmptyState title="No evolution events yet." body="Promoted strategies can generate child variants while preserving parent lineage." />}
      </Card>

      <Card title="Safety Boundary" eyebrow="Research only">
        <p className="muted">{dashboard.safety}</p>
      </Card>
    </section>
  );
}

function DiscoveryTable({ rows, compact = false }: { rows: StrategyDiscoveryRow[]; compact?: boolean }) {
  return (
    <DataTable
      columns={compact ? ["Candidate", "Status", "Score", "PF"] : ["Candidate", "Status", "Score", "Trades", "PF", "Rules", "Why"]}
      rows={rows.map((row) => compact ? [
        row.candidate_id,
        <span className={`status ${statusClass(row.status)}`} key={row.candidate_id}>{row.status}</span>,
        format(row.research_score),
        format(row.metrics?.profit_factor)
      ] : [
        row.candidate_id,
        <span className={`status ${statusClass(row.status)}`} key={row.candidate_id}>{row.status}</span>,
        format(row.research_score),
        format(row.metrics?.number_of_trades),
        format(row.metrics?.profit_factor),
        Object.values(row.blocks ?? {}).join(" + "),
        row.explanation
      ])}
    />
  );
}

function format(value: unknown) {
  if (value === null || value === undefined || value === "") return "n/a";
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(Math.abs(number) >= 10 ? 0 : 2) : String(value);
}

function statusClass(status: string) {
  if (status === "promoted") return "setup";
  if (status === "rejected" || status === "retired") return "avoid";
  return "watchlist";
}
