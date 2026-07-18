export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";
export const revalidate = 0;

import { Card, EmptyState, MetricCard, PageTitle, Timeline } from "@/components/ResearchUI";
import { countBy, getLiveResearchSnapshot, timelineItems } from "@/lib/live-research";

export default async function JournalPage() {
  const snapshot = await getLiveResearchSnapshot();
  const events = timelineItems(snapshot, 30);
  const journalTypes = countBy(snapshot.journal, (entry) => entry.entry_type);
  return (
    <div className="pageStack">
      <PageTitle title="Research Journal" description="Chronological research memory connecting hypotheses, experiments, results, conclusions, and next actions." />
      <div className="metricGrid">
        <MetricCard label="Entries" value={snapshot.journal.length} detail="Journal records" />
        <MetricCard label="Timeline events" value={snapshot.timeline.length} detail="Research activity" tone="warning" />
        <MetricCard label="Hypotheses created" value={journalTypes.hypothesis_created ?? 0} detail="Backlog additions" />
        <MetricCard label="Validation runs" value={snapshot.validationRuns.length} detail="Evidence checks" tone="success" />
      </div>
      <Card title="Chronological history" eyebrow="Archive">
        {events.length ? <Timeline items={events} /> : <EmptyState title="No journal entries yet." body="Create a hypothesis or run an experiment to start the research timeline." />}
      </Card>
    </div>
  );
}
