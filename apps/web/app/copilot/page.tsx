export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";
export const revalidate = 0;

import { Card, DataTable, EmptyState, EvidenceBadges, PageTitle } from "@/components/ResearchUI";
import { getCopilotInteractions } from "@/lib/api";
import { displayProviderFromModel } from "@/lib/live-research";

export default async function CopilotPage() {
  const interactions = await getCopilotInteractions().catch(() => []);
  return (
    <div className="pageStack">
      <PageTitle title="AI Copilot" description="Read-only research assistant grounded in KefTrade experiments, validation runs, journal entries, and evidence references." />
      <Card title="Interaction history" eyebrow="Audit trail">
        {interactions.length ? (
          <DataTable
            columns={["Question", "Provider / Model", "Evidence", "Safety", "Created"]}
            rows={interactions.slice(0, 12).map((item) => [
              item.question,
              `${displayProviderFromModel(item.model)} / ${item.model}`,
              <EvidenceBadges key={`${item.id}-refs`} refs={item.evidence_refs ?? []} />,
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
