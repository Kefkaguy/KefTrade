import {
  getPaperAccounts,
  getPaperBalances,
  getPaperEquityCurve,
  getPaperFills,
  getPaperOrders,
  getPaperPositions,
  getExecutionLogs,
  getPaperScheduler,
  getStrategyDeployments
} from "@/lib/api";

export async function getPaperSnapshot() {
  const accounts = await getPaperAccounts().catch(() => []);
  const account = accounts[0] ?? null;
  if (!account) {
    const scheduler = await getPaperScheduler().catch(() => null);
    return { accounts, account: null, balances: null, positions: [], orders: [], fills: [], equity: [], deployments: [], logs: [], scheduler };
  }
  const [balances, positions, orders, fills, equity, deployments, logs, scheduler] = await Promise.all([
    getPaperBalances(account.id).catch(() => null),
    getPaperPositions(account.id).catch(() => []),
    getPaperOrders(account.id).catch(() => []),
    getPaperFills(account.id).catch(() => []),
    getPaperEquityCurve(account.id).catch(() => []),
    getStrategyDeployments(account.id).catch(() => []),
    getExecutionLogs(account.id).catch(() => []),
    getPaperScheduler().catch(() => null)
  ]);
  return { accounts, account, balances, positions, orders, fills, equity, deployments, logs, scheduler };
}
