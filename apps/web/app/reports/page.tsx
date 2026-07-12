import { SavedReports } from "@/components/SavedReports";
import { GenerateDailyReportButton } from "@/components/DailyReportActions";
import { Card, DataTable, EmptyState, MetricCard, PageTitle } from "@/components/ResearchUI";
import { getDailyResearchReports } from "@/lib/api";
import { money, number } from "@/lib/format";

export default async function ReportsPage() {
  const dailyReports = await getDailyResearchReports(14).catch(() => []);
  const latest = dailyReports[0] ?? null;
  return (
    <div className="pageStack reportsPage">
      <PageTitle
        title="Research Reports"
        description="Persisted daily research operations reports plus browser-local saved evidence reports."
        actions={<GenerateDailyReportButton />}
      />
      <Card title="Daily Research Report System" eyebrow="Stored operations summaries">
        {latest ? (
          <>
            <div className="metricGrid">
              <MetricCard label="Report date" value={latest.report_date} detail={`Generated ${formatDate(latest.generated_at)}`} />
              <MetricCard label="Assets scanned" value={latest.summary.assets_scanned.count} detail={latest.summary.assets_scanned.symbols.join(", ") || "No assets scanned"} />
              <MetricCard label="Setups found" value={latest.summary.setups_found.count} />
              <MetricCard label="No-setup decisions" value={latest.summary.no_setup_decisions.count} />
              <MetricCard label="Stale-data blocks" value={latest.summary.stale_data_blocks.count} tone={latest.summary.stale_data_blocks.count ? "warning" : "success"} />
              <MetricCard label="Scheduler errors" value={latest.summary.scheduler_errors.count} tone={latest.summary.scheduler_errors.count ? "error" : "success"} />
              <MetricCard label="Paper orders" value={latest.summary.paper_orders.count} detail="Simulated only" />
              <MetricCard label="Paper fills" value={latest.summary.paper_fills.count} detail="Simulated only" />
              <MetricCard label="Realized P&L" value={money(latest.summary.pnl.realized)} detail="Simulated" />
              <MetricCard label="Unrealized P&L" value={money(latest.summary.pnl.unrealized)} detail="Simulated" />
              <MetricCard label="Scheduler uptime" value={formatUptime(latest.summary.scheduler_uptime)} />
              <MetricCard label="Important alerts" value={latest.summary.important_alerts.count} tone={latest.summary.important_alerts.count ? "warning" : "success"} />
            </div>
            <div className="dashboardGrid">
              <div className="scoreList">
                <span>Fresh data <strong>{latest.summary.data_freshness.counts.Healthy ?? 0}</strong></span>
                <span>Warning data <strong>{latest.summary.data_freshness.counts.Warning ?? 0}</strong></span>
                <span>Stale data <strong>{latest.summary.data_freshness.counts.Stale ?? 0}</strong></span>
                <span>Safety <strong>{latest.summary.safety}</strong></span>
              </div>
              <pre className="reportBlock compactReportBlock">{latest.markdown_report}</pre>
            </div>
          </>
        ) : (
          <EmptyState title="No daily research reports yet." body="Generate today’s report to persist the day’s stored scan, alert, scheduler, paper simulation, P&L, and freshness summary." action={<GenerateDailyReportButton />} />
        )}
      </Card>

      <Card title="Recent daily reports" eyebrow="History">
        {dailyReports.length ? (
          <DataTable
            columns={["Date", "Assets", "Setups", "No setup", "Stale", "Scheduler errors", "Orders", "Fills", "Realized", "Unrealized"]}
            rows={dailyReports.map((report) => [
              report.report_date,
              report.summary.assets_scanned.count,
              report.summary.setups_found.count,
              report.summary.no_setup_decisions.count,
              report.summary.stale_data_blocks.count,
              report.summary.scheduler_errors.count,
              report.summary.paper_orders.count,
              report.summary.paper_fills.count,
              money(report.summary.pnl.realized),
              money(report.summary.pnl.unrealized)
            ])}
          />
        ) : <EmptyState title="No report history." body="Daily research reports will appear here after generation." />}
      </Card>

      <SavedReports />
    </div>
  );
}

function formatDate(value?: string | null) {
  return value ? new Date(value).toLocaleString() : "Never";
}

function formatUptime(value: string | number | null) {
  if (value === null || value === undefined) return "manual/disabled";
  return `${number(value, 2)}%`;
}
