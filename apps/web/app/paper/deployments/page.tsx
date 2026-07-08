import { CreateDeployment, PauseDeploymentButton } from "@/components/PaperActions";
import { Card, DataTable, EmptyState, PageTitle } from "@/components/ResearchUI";
import { getPaperSnapshot } from "@/lib/paper";

export default async function StrategyDeploymentsPage() {
  const snapshot = await getPaperSnapshot();
  return (
    <div className="pageStack">
      <PageTitle title="Strategy Deployments" description="Simulation-only deployment lifecycle. Deployments do not route real orders." />
      {snapshot.account ? (
        <>
          <Card title="Create deployment" eyebrow="Internal simulation">
            <CreateDeployment accountId={snapshot.account.id} />
          </Card>
          <Card title="Deployments" eyebrow={snapshot.account.name}>
            {snapshot.deployments.length ? (
              <DataTable
                columns={["ID", "Strategy", "Symbol", "Timeframe", "Status", "Simulation", "Action"]}
                rows={snapshot.deployments.map((row) => [
                  row.id,
                  `${row.strategy_name} ${row.strategy_version}`,
                  row.symbol,
                  row.timeframe,
                  row.status,
                  row.simulation_only ? "Yes" : "No",
                  row.status === "active" ? <PauseDeploymentButton key={row.id} deploymentId={row.id} /> : "Paused"
                ])}
              />
            ) : (
              <EmptyState title="No deployments." body="Create a simulation-only strategy deployment to track lifecycle state." />
            )}
          </Card>
        </>
      ) : (
        <EmptyState title="No paper account." body="Create a paper account before creating deployments." />
      )}
    </div>
  );
}
