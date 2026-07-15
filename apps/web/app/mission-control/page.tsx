import Link from "next/link";
import { Card, EmptyState, PageTitle } from "@/components/ResearchUI";
import { MissionControlDashboard } from "@/components/MissionControlDashboard";
import { getDeploymentManagement, getMissionControl, getResearchIntelligence } from "@/lib/api";

export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";
export const revalidate = 0;

export default async function MissionControlPage() {
  const [snapshot, deploymentManagement] = await Promise.all([
    getMissionControl().catch((error) => ({ error: error instanceof Error ? error.message : "Mission Control unavailable" })),
    getDeploymentManagement().catch((error) => ({ error: error instanceof Error ? error.message : "Deployment management unavailable" }))
  ]);

  if ("error" in snapshot) {
    return (
      <div className="pageStack">
        <PageTitle title="Research Mission Control" description="Multi-asset research operations dashboard." />
        <Card title="Mission Control unavailable" eyebrow="Full error">
          <EmptyState title="Unable to load dashboard data." body={snapshot.error} action={<Link className="button" href="/paper">Open Paper Lab</Link>} />
        </Card>
      </div>
    );
  }

  const researchIntelligence = await getResearchIntelligence().catch(() => null);
  const deploymentError = "error" in deploymentManagement ? deploymentManagement.error : null;

  return (
    <MissionControlDashboard
      snapshot={snapshot}
      deploymentManagement={"error" in deploymentManagement ? null : deploymentManagement}
      deploymentError={deploymentError}
      researchIntelligence={researchIntelligence}
    />
  );
}
