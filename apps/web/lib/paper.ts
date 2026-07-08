import {
  getPaperAccounts,
  getPaperBalances,
  getPaperEquityCurve,
  getPaperFills,
  getPaperOrders,
  getPaperPositions,
  getStrategyDeployments
} from "@/lib/api";

export async function getPaperSnapshot() {
  const accounts = await getPaperAccounts().catch(() => []);
  const account = accounts[0] ?? null;
  if (!account) {
    return { accounts, account: null, balances: null, positions: [], orders: [], fills: [], equity: [], deployments: [] };
  }
  const [balances, positions, orders, fills, equity, deployments] = await Promise.all([
    getPaperBalances(account.id).catch(() => null),
    getPaperPositions(account.id).catch(() => []),
    getPaperOrders(account.id).catch(() => []),
    getPaperFills(account.id).catch(() => []),
    getPaperEquityCurve(account.id).catch(() => []),
    getStrategyDeployments(account.id).catch(() => [])
  ]);
  return { accounts, account, balances, positions, orders, fills, equity, deployments };
}
