import {
  getPaperAccounts,
  getPaperBalances,
  getPaperEquityCurve,
  getPaperFills,
  getPaperOrders,
  getPaperPositions,
  getExecutionLogs,
  getEvidenceAlerts,
  getPaperScheduler,
  getSignalReviews,
  getStrategyDeployments
} from "@/lib/api";

export async function getPaperSnapshot() {
  const accounts = await getPaperAccounts().catch(() => []);
  const account = accounts[0] ?? null;
  if (!account) {
    const scheduler = await getPaperScheduler().catch(() => null);
    const alerts = await getEvidenceAlerts({ limit: 100 }).catch(() => []);
    const signalReviews = await getSignalReviews({ limit: 25 }).catch(() => []);
    return { accounts, account: null, balances: null, positions: [], orders: [], fills: [], equity: [], deployments: [], logs: [], scheduler, alerts, signalReviews };
  }
  const [balances, positions, orders, fills, equity, deployments, logs, scheduler, alerts, signalReviews] = await Promise.all([
    getPaperBalances(account.id).catch(() => null),
    getPaperPositions(account.id).catch(() => []),
    getPaperOrders(account.id).catch(() => []),
    getPaperFills(account.id).catch(() => []),
    getPaperEquityCurve(account.id).catch(() => []),
    getStrategyDeployments(account.id).catch(() => []),
    getExecutionLogs(account.id).catch(() => []),
    getPaperScheduler().catch(() => null),
    getEvidenceAlerts({ limit: 100 }).catch(() => []),
    getSignalReviews({ accountId: account.id, limit: 25 }).catch(() => [])
  ]);
  return { accounts, account, balances, positions, orders, fills, equity, deployments, logs, scheduler, alerts, signalReviews };
}
