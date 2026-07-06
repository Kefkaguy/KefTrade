import { Card, DataTable, EmptyState, PageTitle } from "@/components/ResearchUI";
import { getCopilotInteractions } from "@/lib/api";

export default async function CopilotPage() {
  const interactions = await getCopilotInteractions().catch(() => []);
  return (
    <div className="pageStack">
      <PageTitle title="AI Copilot" description="Read-only research assistant grounded in KefTrade experiments, validation runs, journal entries, and evidence references." />
      <Card title="Interaction history" eyebrow="Audit trail">
        {interactions.length ? (
          <DataTable
            columns={["Question", "Model", "Confidence", "Safety", "Created"]}
            rows={interactions.slice(0, 12).map((item) => [
              item.question,
              item.model,
              item.confidence,
              item.safety_flags?.join(", ") || "None",
              new Date(item.created_at).toLocaleString()
            ])}
          />
        ) : (
          <EmptyState title="No interactions loaded" body="Ask the copilot panel a research question to create an auditable interaction." />
        )}
      </Card>
    </div>
  );
}
