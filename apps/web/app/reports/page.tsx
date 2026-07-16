import { AlertTriangle, Archive, CalendarDays, CheckCircle2, ChevronRight, FileText, ShieldCheck } from "lucide-react";
import { SavedReports } from "@/components/SavedReports";
import { GenerateDailyReportButton } from "@/components/DailyReportActions";
import { DataTable, EmptyState, LineChart } from "@/components/ResearchUI";
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
    <div className="reportLibrary">
      <header className="reportLibraryHero">
        <div>
          <span className="eyebrow">Research archive</span>
          <h1>Research reports</h1>
          <p>A durable record of what KefTrade examined, what it found, and where evidence remains incomplete.</p>
        </div>
        <GenerateDailyReportButton />
      </header>

      {latest ? (
        <div className="reportReaderLayout">
          <aside className="reportIndex" aria-label="Report index">
            <div className="reportIndexHeading">
              <Archive size={17} />
              <div><strong>Daily archive</strong><span>{dailyReports.length} stored report{dailyReports.length === 1 ? "" : "s"}</span></div>
            </div>
            <nav>
              {dailyReports.slice(0, 12).map((report, index) => (
                <a href={index === 0 ? "#current-report" : "#report-history"} className={index === 0 ? "active" : undefined} key={report.report_date}>
                  <span>{report.report_date}</span><small>{report.summary.assets_scanned.count} assets</small><ChevronRight size={13} />
                </a>
              ))}
            </nav>
            <div className="reportArchiveSafety"><ShieldCheck size={15} /><span>Simulation evidence only</span></div>
          </aside>

          <main className="reportReader" id="current-report">
            <header className="reportDocumentHeader">
              <div className="reportDocumentIcon"><FileText size={22} /></div>
              <div>
                <span>Daily research record</span>
                <h2>{latest.report_date}</h2>
                <p>Generated {formatDate(latest.generated_at)}</p>
              </div>
              <div className={`reportHealth ${latest.summary.scheduler_errors.count ? "warning" : "healthy"}`}>
                {latest.summary.scheduler_errors.count ? <AlertTriangle size={15} /> : <CheckCircle2 size={15} />}
                {latest.summary.scheduler_errors.count ? "Review required" : "Operations stable"}
              </div>
            </header>

            <section className="reportKeyFindings" aria-label="Key report findings">
              <ReportFact label="Markets examined" value={latest.summary.assets_scanned.count} detail={latest.summary.assets_scanned.symbols.join(", ") || "None scanned"} />
              <ReportFact label="Setups retained" value={latest.summary.setups_found.count} detail={`${latest.summary.no_setup_decisions.count} rejected or no setup`} />
              <ReportFact label="Data warnings" value={latest.summary.data_freshness.counts.Warning ?? 0} detail={`${latest.summary.data_freshness.counts.Stale ?? 0} stale`} />
              <ReportFact label="Scheduler uptime" value={formatUptime(latest.summary.scheduler_uptime)} detail={`${latest.summary.scheduler_errors.count} errors`} />
            </section>

            <section className="reportNarrative">
              <div className="reportSectionHeading"><span>01</span><div><h3>Research record</h3><p>The complete persisted daily summary.</p></div></div>
              <pre>{latest.markdown_report}</pre>
            </section>

            <section className="reportEvidenceLedger">
              <div className="reportSectionHeading"><span>02</span><div><h3>Evidence ledger</h3><p>Operational and simulation totals behind this report.</p></div></div>
              <dl>
                <LedgerRow label="Fresh market datasets" value={latest.summary.data_freshness.counts.Healthy ?? 0} />
                <LedgerRow label="Stale-data blocks" value={latest.summary.stale_data_blocks.count} />
                <LedgerRow label="Important alerts" value={latest.summary.important_alerts.count} />
                <LedgerRow label="Simulated orders / fills" value={`${latest.summary.paper_orders.count} / ${latest.summary.paper_fills.count}`} />
                <LedgerRow label="Simulated realized P&L" value={money(latest.summary.pnl.realized)} />
                <LedgerRow label="Simulated unrealized P&L" value={money(latest.summary.pnl.unrealized)} />
              </dl>
              <p><ShieldCheck size={14} />{latest.summary.safety}</p>
            </section>

            <ReportAppendix title="Historical evidence" meta={`${analytics?.windows.all_time?.report_count ?? 0} reports`}>
              {analytics && series.length ? (
                <>
                  <div className="reportWindowStrip">
                    <ReportFact label="7d setups" value={analytics.windows["7d"]?.setups_found ?? 0} detail={`${analytics.windows["7d"]?.no_setup_decisions ?? 0} no-setup`} />
                    <ReportFact label="7d stale blocks" value={analytics.windows["7d"]?.stale_data_blocks ?? 0} detail="Data readiness" />
                    <ReportFact label="30d setups" value={analytics.windows["30d"]?.setups_found ?? 0} detail={`${analytics.windows["30d"]?.scheduler_errors ?? 0} scheduler errors`} />
                    <ReportFact label="7d simulated P&L change" value={money(analytics.windows["7d"]?.realized_pnl_change)} detail="Paper evidence" />
                  </div>
                  <div className="reportChartGrid">
                    <ReportChart title="Scheduler uptime"><LineChart values={series.map((row) => Number(row.scheduler_uptime ?? 0))} label="Scheduler uptime over stored daily reports" /></ReportChart>
                    <ReportChart title="Stale-data frequency"><LineChart values={series.map((row) => row.stale_data_blocks)} label="Stale-data blocks over stored daily reports" /></ReportChart>
                    <ReportChart title="Setups retained"><LineChart values={series.map((row) => row.setups_found)} label="Setups found over stored daily reports" /></ReportChart>
                    <ReportChart title="No-setup decisions"><LineChart values={series.map((row) => row.no_setup_decisions)} label="No-setup decisions over stored daily reports" /></ReportChart>
                    <ReportChart title="Simulated P&L"><LineChart values={series.map((row) => row.realized_pnl + row.unrealized_pnl)} label="Combined simulated P&L over stored reports" /></ReportChart>
                    <ReportChart title="Paper equity"><LineChart values={series.map((row) => row.equity)} label="Simulated paper equity over stored reports" /></ReportChart>
                  </div>
                </>
              ) : <EmptyState title="History begins with this report" body="Additional daily reports will reveal evidence and operations trends." />}
            </ReportAppendix>

            <ReportAppendix title="Comparisons and exceptions" meta="Stored evidence">
              <div className="reportComparisonGrid">
                <div><h4>Assets</h4>{analytics?.asset_comparison.length ? <DataTable columns={["Asset", "Days", "Setups", "Stale", "Alerts"]} rows={analytics.asset_comparison.slice(0, 12).map((row) => [String(row.symbol), row.scanned_days, row.setups, row.stale_blocks, row.important_alerts])} /> : <EmptyState title="No asset comparison" body="Asset evidence appears after markets are scanned across reports." />}</div>
                <div><h4>Strategies</h4>{analytics?.strategy_comparison.length ? <DataTable columns={["Strategy", "Setups", "No setup", "Alerts"]} rows={analytics.strategy_comparison.slice(0, 12).map((row) => [String(row.strategy), row.setups, row.no_setup, row.important_alerts])} /> : <EmptyState title="No strategy comparison" body="Strategy evidence appears after setup reviews are stored." />}</div>
              </div>
              <div className="reportExceptionTable">
                <h4>Recurring operational failures</h4>
                {analytics?.recurring_operational_failures.length ? <DataTable columns={["Symbol", "Type", "Count", "Dates", "Message"]} rows={analytics.recurring_operational_failures.slice(0, 12).map((row) => [String(row.symbol ?? "SYSTEM"), String(row.event_type ?? "unknown"), String(row.count ?? 0), Array.isArray(row.dates) ? row.dates.join(", ") : "", String(row.message ?? "")])} /> : <EmptyState title="No recurring failures" body="No repeated stale-data or scheduler issue is present in stored reports." />}
              </div>
            </ReportAppendix>

            <ReportAppendix title="Weekly synthesis" meta="Last 7 reports">
              {analytics ? <div className="weeklySynthesis"><div><CalendarDays size={18} /><strong>{String(analytics.weekly_summary.summary.report_count ?? 0)} reports reviewed</strong></div><p>{analytics.weekly_summary.narrative}</p><span>{String(analytics.weekly_summary.summary.setups_found ?? 0)} setups · {String(analytics.weekly_summary.summary.no_setup_decisions ?? 0)} no-setup decisions · {String(analytics.weekly_summary.summary.scheduler_errors ?? 0)} scheduler errors</span></div> : <EmptyState title="No weekly synthesis" body="The synthesis is derived from stored daily reports." />}
            </ReportAppendix>

            <section className="reportHistory" id="report-history">
              <div className="reportSectionHeading"><span>03</span><div><h3>Report history</h3><p>Every persisted daily research record.</p></div></div>
              <DataTable columns={["Date", "Assets", "Setups", "No setup", "Stale", "Errors", "Orders", "Fills", "Realized", "Unrealized"]} rows={dailyReports.map((report) => [report.report_date, report.summary.assets_scanned.count, report.summary.setups_found.count, report.summary.no_setup_decisions.count, report.summary.stale_data_blocks.count, report.summary.scheduler_errors.count, report.summary.paper_orders.count, report.summary.paper_fills.count, money(report.summary.pnl.realized), money(report.summary.pnl.unrealized)])} />
            </section>
          </main>
        </div>
      ) : <div className="reportFirstRun"><FileText size={28} /><EmptyState title="Create the first research record" body="Generate today's report to persist scan, evidence, scheduler, paper simulation, P&L, and data-readiness history." action={<GenerateDailyReportButton />} /></div>}

      <SavedReports />
    </div>
  );
}

function ReportFact({ label, value, detail }: { label: string; value: string | number; detail: string }) { return <div className="reportFact"><span>{label}</span><strong>{value}</strong><small>{detail}</small></div>; }
function LedgerRow({ label, value }: { label: string; value: string | number }) { return <div><dt>{label}</dt><dd>{value}</dd></div>; }
function ReportAppendix({ title, meta, children }: { title: string; meta: string; children: React.ReactNode }) { return <details className="reportAppendix"><summary><span>{title}</span><small>{meta}</small><ChevronRight size={16} /></summary><div>{children}</div></details>; }
function ReportChart({ title, children }: { title: string; children: React.ReactNode }) { return <div className="reportChart"><h4>{title}</h4>{children}</div>; }
function formatDate(value?: string | null) { return value ? new Date(value).toLocaleString() : "Never"; }
function formatUptime(value: string | number | null) { return value === null || value === undefined ? "Manual" : `${number(value, 2)}%`; }
