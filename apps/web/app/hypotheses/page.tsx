import { Card, DataTable, MetricCard, PageTitle } from "@/components/ResearchUI";
import { hypotheses } from "@/lib/research-data";

export default function HypothesesPage() {
  return (
    <div className="pageStack">
      <PageTitle title="Hypotheses" description="Evidence-backed research questions with status, linked experiments, and next actions." />
      <div className="metricGrid">
        <MetricCard label="Active" value={hypotheses.length} detail="Awaiting more evidence" />
        <MetricCard label="Rejected" value="7" detail="Repeated failures" tone="error" />
        <MetricCard label="Research More" value="3" detail="Needs deeper tests" tone="warning" />
        <MetricCard label="Validated" value="0" detail="Evidence threshold intact" tone="success" />
      </div>
      <Card title="Research backlog" eyebrow="Questions">
        <DataTable
          columns={["Hypothesis", "Status", "Linked evidence", "Next step"]}
          rows={hypotheses.map((hypothesis, index) => [
            hypothesis,
            <span className="status watchlist" key={hypothesis}>Research More</span>,
            `validation_run:${20 + index}`,
            index % 2 === 0 ? "Test across equities and volatility regimes" : "Add falsification test before new variants"
          ])}
        />
      </Card>
    </div>
  );
}
