import { Card, DataTable, EmptyState, MetricCard, PageTitle } from "@/components/ResearchUI";
import { HypothesisComposer, HypothesisExperimentAction } from "@/components/HypothesisWorkflow";
import { countBy, getLiveResearchSnapshot, statusClass, titleFromEvent } from "@/lib/live-research";

export default async function HypothesesPage() {
  const snapshot = await getLiveResearchSnapshot();
  const statuses = countBy(snapshot.hypotheses, (hypothesis) => hypothesis.status);
  return (
    <div className="pageStack">
      <PageTitle title="Hypotheses" description="Evidence-backed research questions with status, linked experiments, and next actions." />
      <div className="metricGrid">
        <MetricCard label="Total" value={snapshot.hypotheses.length} detail="Backend hypotheses" />
        <MetricCard label="Rejected" value={statuses.rejected ?? 0} detail="Repeated failures" tone="error" />
        <MetricCard label="Research More" value={statuses.research_more ?? 0} detail="Needs deeper tests" tone="warning" />
        <MetricCard label="Validated" value={statuses.validated ?? 0} detail="Evidence threshold passed" tone="success" />
      </div>
      <Card title="Create hypothesis" eyebrow="Research intake">
        <HypothesisComposer />
      </Card>
      <Card title="Research backlog" eyebrow="Questions">
        {snapshot.hypotheses.length ? (
          <DataTable
            columns={["Title", "Hypothesis", "Status", "Tags", "Updated", "Action"]}
            rows={snapshot.hypotheses.map((hypothesis) => [
              hypothesis.title,
              hypothesis.hypothesis,
              <span className={`status ${statusClass(hypothesis.status)}`} key={hypothesis.id}>
                {titleFromEvent(hypothesis.status)}
              </span>,
              hypothesis.tags?.join(", ") || "None",
              new Date(hypothesis.updated_at).toLocaleDateString(),
              <HypothesisExperimentAction key={`${hypothesis.id}-action`} hypothesis={hypothesis} />
            ])}
          />
        ) : (
          <EmptyState title="No hypotheses yet." body="Create your first research hypothesis using the form above, then run its first experiment from this page." />
        )}
      </Card>
    </div>
  );
}
