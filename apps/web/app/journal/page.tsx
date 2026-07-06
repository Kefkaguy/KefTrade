import { Card, MetricCard, PageTitle, Timeline } from "@/components/ResearchUI";
import { journalEntries } from "@/lib/research-data";

export default function JournalPage() {
  return (
    <div className="pageStack">
      <PageTitle title="Research Journal" description="Chronological research memory connecting hypotheses, experiments, results, conclusions, and next actions." />
      <div className="metricGrid">
        <MetricCard label="Entries" value={journalEntries.length} detail="Recent research timeline" />
        <MetricCard label="Open actions" value="3" detail="Evidence-backed" tone="warning" />
        <MetricCard label="Rejected paths" value="7" detail="Documented failures" tone="error" />
        <MetricCard label="Validated alpha" value="0" detail="No false positives" tone="success" />
      </div>
      <Card title="Chronological history" eyebrow="Archive">
        <Timeline items={journalEntries} />
      </Card>
    </div>
  );
}
