import {
  getPaperAccounts,
  getPaperBalances,
  getPaperEquityCurve,
  getPaperFills,
  getPaperOrders,
  getPaperPositions,
  getExecutionLogs,
  getStrategyDeployments
} from "@/lib/api";

export async function getPaperSnapshot() {
  const accounts = await getPaperAccounts().catch(() => []);
  const account = accounts[0] ?? null;
  if (!account) {
    return { accounts, account: null, balances: null, positions: [], orders: [], fills: [], equity: [], deployments: [], logs: [] };
  }
  const [balances, positions, orders, fills, equity, deployments, logs] = await Promise.all([
    getPaperBalances(account.id).catch(() => null),
    getPaperPositions(account.id).catch(() => []),
    getPaperOrders(account.id).catch(() => []),
    getPaperFills(account.id).catch(() => []),
    getPaperEquityCurve(account.id).catch(() => []),
    getStrategyDeployments(account.id).catch(() => []),
    getExecutionLogs(account.id).catch(() => [])
  ]);
  return { accounts, account, balances, positions, orders, fills, equity, deployments, logs };
}
