import { SavedReports } from "@/components/SavedReports";
import { GenerateDailyReportButton } from "@/components/DailyReportActions";
import { Card, DataTable, EmptyState, LineChart, MetricCard, PageTitle } from "@/components/ResearchUI";
import { getDailyReportAnalytics, getDailyResearchReports } from "@/lib/api";
import { money, number } from "@/lib/format";

export default async function ReportsPage() {
  const [dailyReports, analytics] = await Promise.all([
    getDailyResearchReports(30).catch(() => []),
    getDailyReportAnalytics().catch(() => null)
  ]);
  const latest = dailyReports[0] ?? null;
  const series = analytics?.series ?? [];
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

      <Card title="Historical Performance Analytics" eyebrow="7-day / 30-day / all-time">
        {analytics && series.length ? (
          <>
            <div className="metricGrid">
              <MetricCard label="7d setups" value={String(analytics.windows["7d"]?.setups_found ?? 0)} detail={`${analytics.windows["7d"]?.no_setup_decisions ?? 0} no-setup decisions`} />
              <MetricCard label="7d stale blocks" value={String(analytics.windows["7d"]?.stale_data_blocks ?? 0)} tone={Number(analytics.windows["7d"]?.stale_data_blocks ?? 0) ? "warning" : "success"} />
              <MetricCard label="7d scheduler uptime" value={formatUptime(analytics.windows["7d"]?.avg_scheduler_uptime as number | null)} />
              <MetricCard label="7d realized P&L Δ" value={money(analytics.windows["7d"]?.realized_pnl_change)} detail="Simulated" />
              <MetricCard label="30d setups" value={String(analytics.windows["30d"]?.setups_found ?? 0)} detail={`${analytics.windows["30d"]?.scheduler_errors ?? 0} scheduler errors`} />
              <MetricCard label="All-time reports" value={String(analytics.windows.all_time?.report_count ?? 0)} detail="Stored daily reports" />
            </div>
            <div className="dashboardGrid">
              <Card title="Scheduler uptime" eyebrow="Percent">
                <LineChart values={series.map((row) => Number(row.scheduler_uptime ?? 0))} label="Scheduler uptime over stored daily reports" />
              </Card>
              <Card title="Stale-data frequency" eyebrow="Blocks">
                <LineChart values={series.map((row) => row.stale_data_blocks)} label="Stale-data blocks over stored daily reports" />
              </Card>
            </div>
            <div className="dashboardGrid">
              <Card title="Setups found" eyebrow="Review items">
                <LineChart values={series.map((row) => row.setups_found)} label="Setups found over stored daily reports" />
              </Card>
              <Card title="No-setup decisions" eyebrow="Review items">
                <LineChart values={series.map((row) => row.no_setup_decisions)} label="No-setup decisions over stored daily reports" />
              </Card>
            </div>
            <div className="dashboardGrid">
              <Card title="Simulated P&L over time" eyebrow="Paper only">
                <LineChart values={series.map((row) => row.realized_pnl + row.unrealized_pnl)} label="Combined simulated realized and unrealized P&L" />
              </Card>
              <Card title="Paper equity over time" eyebrow="Simulated">
                <LineChart values={series.map((row) => row.equity)} label="Simulated paper equity over stored reports" />
              </Card>
            </div>
          </>
        ) : (
          <EmptyState title="No historical analytics yet." body="Generate daily reports across multiple dates to populate trend charts and comparisons." />
        )}
      </Card>

      <div className="dashboardGrid">
        <Card title="Asset comparison" eyebrow="All stored daily reports">
          {analytics?.asset_comparison.length ? (
            <DataTable
              columns={["Asset", "Scanned days", "Setups", "Stale blocks", "Important alerts"]}
              rows={analytics.asset_comparison.slice(0, 12).map((row) => [String(row.symbol), row.scanned_days, row.setups, row.stale_blocks, row.important_alerts])}
            />
          ) : <EmptyState title="No asset comparison yet." body="Asset rankings will appear after reports contain scan, setup, stale-data, and alert history." />}
        </Card>
        <Card title="Strategy comparison" eyebrow="All stored daily reports">
          {analytics?.strategy_comparison.length ? (
            <DataTable
              columns={["Strategy", "Setups", "No setup", "Important alerts"]}
              rows={analytics.strategy_comparison.slice(0, 12).map((row) => [String(row.strategy), row.setups, row.no_setup, row.important_alerts])}
            />
          ) : <EmptyState title="No strategy comparison yet." body="Strategy summaries will appear when setup and no-setup review records are present." />}
        </Card>
      </div>

      <Card title="Recurring Operational Failures" eyebrow="Grouped stale-data and scheduler issues">
        {analytics?.recurring_operational_failures.length ? (
          <DataTable
            columns={["Symbol", "Type", "Count", "Dates", "Message"]}
            rows={analytics.recurring_operational_failures.slice(0, 12).map((row) => [
              String(row.symbol ?? "SYSTEM"),
              String(row.event_type ?? "unknown"),
              String(row.count ?? 0),
              Array.isArray(row.dates) ? row.dates.join(", ") : "",
              String(row.message ?? "")
            ])}
          />
        ) : <EmptyState title="No recurring failures detected." body="Recurring operational issues are grouped from stored stale-data blocks and scheduler errors." />}
      </Card>

      <Card title="Weekly Research Summary" eyebrow="Last 7 stored reports">
        {analytics ? (
          <div className="dashboardGrid wideLeft">
            <div className="scoreList">
              <span>Reports reviewed <strong>{String(analytics.weekly_summary.summary.report_count ?? 0)}</strong></span>
              <span>Setups found <strong>{String(analytics.weekly_summary.summary.setups_found ?? 0)}</strong></span>
              <span>No-setup decisions <strong>{String(analytics.weekly_summary.summary.no_setup_decisions ?? 0)}</strong></span>
              <span>Stale-data blocks <strong>{String(analytics.weekly_summary.summary.stale_data_blocks ?? 0)}</strong></span>
              <span>Scheduler errors <strong>{String(analytics.weekly_summary.summary.scheduler_errors ?? 0)}</strong></span>
              <span>Paper orders <strong>{String(analytics.weekly_summary.summary.paper_orders ?? 0)}</strong></span>
            </div>
            <div className="actionNote">
              <strong>Summary</strong>
              <p>{analytics.weekly_summary.narrative}</p>
              <p className="formHint">Research-only weekly summary. All paper values are simulated.</p>
            </div>
          </div>
        ) : <EmptyState title="No weekly summary yet." body="Weekly summaries are derived from stored daily reports." />}
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
