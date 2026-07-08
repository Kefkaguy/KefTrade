import { AlphaValidationRunner } from "@/components/AlphaValidationRunner";
import { Card, DataTable, EmptyState } from "@/components/ResearchUI";
import { getValidationRuns } from "@/lib/api";
import Link from "next/link";

export default async function ValidationPage() {
  const runs = await getValidationRuns().catch(() => []);
  return (
    <div className="grid">
      <header className="pageHeader">
        <div>
          <h1>Alpha Validation</h1>
          <p className="muted">Validate discovered alpha across assets, timeframes, regimes, bootstrap samples, and Monte Carlo paths.</p>
        </div>
      </header>
      <AlphaValidationRunner />
      <Card title="Saved validation runs" eyebrow="Drilldowns">
        {runs.length ? (
          <DataTable
            columns={["Run", "Candidates", "Symbols", "Timeframes", "Created"]}
            rows={runs.slice(0, 20).map((run) => [
              <Link className="tableLink" href={`/validation/${run.id}`} key={run.id}>Run {run.id}</Link>,
              run.candidate_count,
              run.symbol_set.join(", "),
              run.timeframe_set.join(", "),
              new Date(run.created_at).toLocaleString()
            ])}
          />
        ) : (
          <EmptyState title="No saved validation runs." body="Run alpha validation to persist evidence and unlock run drilldowns." />
        )}
      </Card>
    </div>
  );
}
