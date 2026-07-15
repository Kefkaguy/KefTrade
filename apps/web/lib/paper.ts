import {
  getPaperAccounts,
  getPaperBalances,
  getPaperFills,
  getPaperOrders,
  getPaperPositions,
  getExecutionLogs,
  getDeploymentManagement,
  getEvidenceAlerts,
  getMissionControl,
  getPaperScheduler,
  getSignalReviews,
  getStrategyDeployments,
  type EvidenceAlert,
  type ExecutionLog,
  type PaperAccount,
  type PaperBalance,
  type PaperEquityPoint,
  type PaperFill,
  type PaperOrder,
  type PaperPosition,
  type StrategyDeployment
} from "@/lib/api";

export async function getPaperSnapshot() {
  const [management, missionControl, scheduler, signalReviews] = await Promise.all([
    getDeploymentManagement().catch(() => null),
    getMissionControl().catch(() => null),
    getPaperScheduler().catch(() => null),
    getSignalReviews({ limit: 25 }).catch(() => [])
  ]);
  if (management) {
    const accountSnapshots = normalizeAccountSnapshots(management.account_snapshots);
    const accounts = normalizeArray<PaperAccount>(management.accounts);
    const account = accounts[0] ?? null;
    const activeSnapshot = account ? accountSnapshots.find((row) => row.account.id === account.id) ?? null : null;
    const allDeployments = normalizeArray<StrategyDeployment>(management.deployments);
    const allPositions = normalizeArray<PaperPosition>(management.positions);
    const allOrders = normalizeArray<PaperOrder>(management.orders).sort((a, b) => dateValue(b.submitted_at) - dateValue(a.submitted_at));
    const allFills = normalizeArray<PaperFill>(management.fills).sort((a, b) => dateValue(b.filled_at) - dateValue(a.filled_at));
    const allLogs = normalizeArray<ExecutionLog>(management.logs).sort((a, b) => dateValue(b.created_at) - dateValue(a.created_at));
    const alerts = normalizeArray<EvidenceAlert>(management.alerts);
    return {
      accounts,
      account,
      balances: activeSnapshot?.balances ?? null,
      positions: activeSnapshot?.positions ?? [],
      orders: activeSnapshot?.orders ?? [],
      fills: activeSnapshot?.fills ?? [],
      equity: activeSnapshot?.equity ?? [],
      deployments: account ? allDeployments.filter((row) => row.account_id === account.id) : [],
      logs: activeSnapshot?.logs ?? [],
      scheduler,
      alerts,
      signalReviews,
      allDeployments,
      allPositions,
      allOrders,
      allFills,
      allEquity: accountSnapshots.flatMap((row) => row.equity).sort((a, b) => dateValue(a.timestamp) - dateValue(b.timestamp)),
      allLogs,
      accountSnapshots,
      missionControl
    };
  }

  const accounts = await getPaperAccounts().catch(() => []);
  const account = accounts[0] ?? null;
  const allDeployments = await getStrategyDeployments().catch(() => []);
  if (!account) {
    const alerts = await getEvidenceAlerts({ limit: 100 }).catch(() => []);
    return { accounts, account: null, balances: null, positions: [], orders: [], fills: [], equity: [], deployments: [], logs: [], scheduler, alerts, signalReviews, allDeployments, allPositions: [], allOrders: [], allFills: [], allEquity: [], allLogs: [], accountSnapshots: [], missionControl };
  }
  const [balances, positions, orders, fills, deployments, logs, alerts] = await Promise.all([
    getPaperBalances(account.id).catch(() => null),
    getPaperPositions(account.id).catch(() => []),
    getPaperOrders(account.id).catch(() => []),
    getPaperFills(account.id).catch(() => []),
    getStrategyDeployments(account.id).catch(() => []),
    getExecutionLogs(account.id).catch(() => []),
    getEvidenceAlerts({ limit: 100 }).catch(() => []),
  ]);
  const accountSnapshots = [{ account, balances, positions, orders, fills, equity: [] as PaperEquityPoint[], logs }];
  return {
    accounts,
    account,
    balances,
    positions,
    orders,
    fills,
    equity: [],
    deployments,
    logs,
    scheduler,
    alerts,
    signalReviews,
    allDeployments,
    allPositions: accountSnapshots.flatMap((row) => row.positions),
    allOrders: accountSnapshots.flatMap((row) => row.orders).sort((a, b) => dateValue(b.submitted_at) - dateValue(a.submitted_at)),
    allFills: accountSnapshots.flatMap((row) => row.fills).sort((a, b) => dateValue(b.filled_at) - dateValue(a.filled_at)),
    allEquity: accountSnapshots.flatMap((row) => row.equity).sort((a, b) => dateValue(a.timestamp) - dateValue(b.timestamp)),
    allLogs: accountSnapshots.flatMap((row) => row.logs).sort((a, b) => dateValue(b.created_at) - dateValue(a.created_at)),
    accountSnapshots,
    missionControl
  };
}

type PaperAccountSnapshot = {
  account: PaperAccount;
  balances: PaperBalance | null;
  positions: PaperPosition[];
  orders: PaperOrder[];
  fills: PaperFill[];
  equity: PaperEquityPoint[];
  logs: ExecutionLog[];
};

function normalizeAccountSnapshots(value: unknown): PaperAccountSnapshot[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((row) => {
    if (!row || typeof row !== "object") return [];
    const record = row as Record<string, unknown>;
    const account = record.account;
    if (!account || typeof account !== "object") return [];
    return [{
      account: account as PaperAccount,
      balances: record.balances && typeof record.balances === "object" ? record.balances as PaperBalance : null,
      positions: normalizeArray<PaperPosition>(record.positions),
      orders: normalizeArray<PaperOrder>(record.orders),
      fills: normalizeArray<PaperFill>(record.fills),
      equity: normalizeArray<PaperEquityPoint>(record.equity),
      logs: normalizeArray<ExecutionLog>(record.logs),
    }];
  });
}

function normalizeArray<T>(value: unknown): T[] {
  return Array.isArray(value) ? value as T[] : [];
}

function dateValue(value?: string | null) {
  return value ? new Date(value).getTime() : 0;
}
