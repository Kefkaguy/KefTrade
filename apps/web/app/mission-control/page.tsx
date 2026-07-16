import Link from "next/link";
import { Card, EmptyState, PageTitle } from "@/components/ResearchUI";
import { MissionControlDashboard } from "@/components/MissionControlDashboard";
import { getDeploymentManagement, getMissionControl, getResearchIntelligence } from "@/lib/api";

export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";
export const revalidate = 0;

export default async function MissionControlPage() {
  const startedAt = Date.now();
  console.info("[mission-control] page load started");
  const [snapshot, deploymentManagement] = await Promise.all([
    timed("mission-control snapshot", () => getMissionControl()).catch((error) => ({ error: error instanceof Error ? error.message : "Mission Control unavailable" })),
    timed("deployment management", () => getDeploymentManagement()).catch((error) => ({ error: error instanceof Error ? error.message : "Deployment management unavailable" }))
  ]);

  if ("error" in snapshot) {
    console.warn(`[mission-control] page load failed in ${Date.now() - startedAt}ms`, snapshot.error);
    return (
      <div className="pageStack">
        <PageTitle title="Research Mission Control" description="Multi-asset research operations dashboard." />
        <Card title="Mission Control unavailable" eyebrow="Full error">
          <EmptyState title="Unable to load dashboard data." body={snapshot.error} action={<Link className="button" href="/paper">Open Paper Lab</Link>} />
        </Card>
      </div>
    );
  }

  const researchIntelligence = await timed("research intelligence", () => getResearchIntelligence()).catch((error) => {
    console.warn("[mission-control] research intelligence unavailable", error instanceof Error ? error.message : error);
    return null;
  });
  const deploymentError = "error" in deploymentManagement ? deploymentManagement.error : null;
  console.info(`[mission-control] page load finished in ${Date.now() - startedAt}ms`, {
    snapshot: "loaded",
    deploymentManagement: deploymentError ? "error" : "loaded",
    researchIntelligence: researchIntelligence ? "loaded" : "unavailable",
  });

  return (
    <MissionControlDashboard
      snapshot={snapshot}
      deploymentManagement={"error" in deploymentManagement ? null : deploymentManagement}
      deploymentError={deploymentError}
      researchIntelligence={researchIntelligence}
    />
  );
}

async function timed<T>(label: string, load: () => Promise<T>): Promise<T> {
  const startedAt = Date.now();
  try {
    const value = await load();
    console.info(`[mission-control] ${label} loaded in ${Date.now() - startedAt}ms`);
    return value;
  } catch (error) {
    console.warn(`[mission-control] ${label} failed in ${Date.now() - startedAt}ms`, error instanceof Error ? error.message : error);
    throw error;
  }
}
