"use client";

import { useEffect, useMemo, useState } from "react";
import {
  getIntradayPaperLabMonitor,
  type IntradayPaperLabMonitor,
  type IntradayPaperLabTrade
} from "@/lib/api";

type Props = {
  initial: IntradayPaperLabMonitor | null;
  experimentId?: number;
  initialError?: string | null;
};

function numberValue(value: unknown): number {
  if (typeof value === "number") return value;
  if (typeof value === "string" && value.trim() !== "") return Number(value);
  return 0;
}

function money(value: unknown) {
  const numeric = numberValue(value);
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(numeric);
}

function fixed(value: unknown, decimals = 3) {
  const numeric = numberValue(value);
  if (!Number.isFinite(numeric)) return "—";
  return numeric.toFixed(decimals);
}

function dateTime(value: unknown) {
  if (!value || typeof value !== "string") return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString(undefined, {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  });
}

function tradePnlClass(trade: IntradayPaperLabTrade) {
  const pnl = numberValue(trade.realized_pnl);
  if (!trade.realized_pnl) return "";
  return pnl >= 0 ? "positive" : "negative";
}

export function PaperLabDashboard({ initial, experimentId = 1, initialError = null }: Props) {
  const [snapshot, setSnapshot] = useState<IntradayPaperLabMonitor | null>(initial);
  const [error, setError] = useState<string | null>(initialError);
  const [loading, setLoading] = useState(false);
  const [lastRefresh, setLastRefresh] = useState<Date | null>(initial ? new Date() : null);

  async function refresh() {
    setLoading(true);
    try {
      const next = await getIntradayPaperLabMonitor(experimentId);
      setSnapshot(next);
      setError(null);
      setLastRefresh(new Date());
    } catch (refreshError) {
      setError(refreshError instanceof Error ? refreshError.message : String(refreshError));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    const id = window.setInterval(() => void refresh(), 30_000);
    return () => window.clearInterval(id);
  }, [experimentId]);

  const pnl = useMemo(() => numberValue(snapshot?.pnl?.realized_pnl), [snapshot]);
  const trades = snapshot?.trades ?? [];
  const recentDecisions = snapshot?.recent_decisions ?? [];
  const orders = snapshot?.orders ?? [];
  const experiment = snapshot?.experiment ?? {};
  const summary = snapshot?.summary ?? {
    decisions: 0,
    entries_submitted: 0,
    exits_submitted: 0,
    skips: 0,
    errors: 0
  };

  return (
    <section className="paperDashboard">
      <div className="paperHero">
        <div>
          <span className="paperOnlyEyebrow">Experiment #{experimentId}</span>
          <h1>{String(experiment.name ?? "Signed imbalance paper lab")}</h1>
          <p>
            Backend-only Alpaca Paper monitor for the signed trade-imbalance experiment.
            This page shows decisions, submitted orders, filled entry/exit prices, and realized P/L when broker sync data is available.
          </p>
        </div>
        <div className="paperHeroActions">
          <button className="button" type="button" onClick={() => void refresh()} disabled={loading}>
            {loading ? "Refreshing…" : "Refresh now"}
          </button>
          <span>Auto-refreshes every 30 seconds</span>
        </div>
      </div>

      {error ? <div className="paperAlert">API status unavailable: {error}</div> : null}

      <div className="paperMetricGrid">
        <Metric label="Realized P/L" value={money(pnl)} tone={pnl >= 0 ? "positive" : "negative"} detail={`${snapshot?.pnl?.realized_trades ?? 0} closed trade(s)`} />
        <Metric label="Open trades" value={String(snapshot?.pnl?.open_trades ?? 0)} detail={`${snapshot?.positions?.length ?? 0} position bucket(s)`} />
        <Metric label="Entries submitted" value={String(summary.entries_submitted ?? 0)} detail={`${summary.exits_submitted ?? 0} exits submitted`} />
        <Metric label="Decisions" value={String(summary.decisions ?? 0)} detail={`${summary.skips ?? 0} skips · ${summary.errors ?? 0} errors`} />
        <Metric label="Threshold" value={fixed(experiment.threshold, 2)} detail={String(experiment.factor_key ?? "signed imbalance")} />
        <Metric label="Status" value={String(experiment.status ?? "unknown")} detail={`Last decision ${dateTime(summary.last_decision_at)}`} />
      </div>

      <div className="paperStatusStrip">
        <span>Trading date <strong>{String(experiment.trading_date ?? "—")}</strong></span>
        <span>Timeframe <strong>{String(experiment.timeframe ?? "30m")}</strong></span>
        <span>Symbols <strong>{Array.isArray(experiment.symbols) ? experiment.symbols.length : "—"}</strong></span>
        <span>Broker sync <strong>{snapshot?.broker_sync?.status ? String(snapshot.broker_sync.status) : "not synced"}</strong></span>
        <span>Awaiting sync <strong>{snapshot?.pnl?.awaiting_broker_sync_items ?? 0}</strong></span>
        <span>Page refresh <strong>{lastRefresh ? dateTime(lastRefresh.toISOString()) : "—"}</strong></span>
      </div>

      <div className="paperGridTwo">
        <section className="paperPanel">
          <header>
            <span className="paperOnlyEyebrow">Trades</span>
            <h2>Entry, exit, and P/L</h2>
          </header>
          {trades.length ? (
            <div className="paperTableWrap">
              <table className="paperTable">
                <thead>
                  <tr>
                    <th>Symbol</th>
                    <th>Side</th>
                    <th>Status</th>
                    <th>Entry</th>
                    <th>Exit</th>
                    <th>P/L</th>
                  </tr>
                </thead>
                <tbody>
                  {trades.map((trade) => (
                    <tr key={trade.id}>
                      <td>{trade.symbol}</td>
                      <td>{trade.side}</td>
                      <td>{trade.status}</td>
                      <td>
                        <strong>{trade.entry_price ? money(trade.entry_price) : "—"}</strong>
                        <small>{trade.entry_status ?? "awaiting sync"}</small>
                      </td>
                      <td>
                        <strong>{trade.exit_price ? money(trade.exit_price) : "—"}</strong>
                        <small>{trade.exit_status ?? "not exited/synced"}</small>
                      </td>
                      <td className={tradePnlClass(trade)}>{trade.realized_pnl ? money(trade.realized_pnl) : "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : <Empty text="No lab positions have been created yet." />}
        </section>

        <section className="paperPanel">
          <header>
            <span className="paperOnlyEyebrow">Orders</span>
            <h2>Synced Alpaca orders</h2>
          </header>
          {orders.length ? (
            <div className="paperOrderList">
              {orders.slice(0, 12).map((order) => (
                <div key={order.client_order_id} className="paperOrderRow">
                  <div>
                    <strong>{order.symbol} · {order.side}</strong>
                    <span>{order.status} · qty {String(order.filled_quantity)}/{String(order.requested_quantity)}</span>
                  </div>
                  <div>
                    <strong>{order.filled_average_price ? money(order.filled_average_price) : "—"}</strong>
                    <span>{dateTime(order.filled_at ?? order.submitted_at)}</span>
                  </div>
                </div>
              ))}
            </div>
          ) : <Empty text="No synced lab orders yet. Run broker sync after orders are submitted." />}
        </section>
      </div>

      <section className="paperPanel">
        <header>
          <span className="paperOnlyEyebrow">Latest decisions</span>
          <h2>Signal decisions</h2>
        </header>
        {recentDecisions.length ? (
          <div className="paperDecisionGrid">
            {recentDecisions.map((decision) => (
              <div key={decision.id} className={`paperDecision ${decision.action}`}>
                <div>
                  <strong>{decision.symbol}</strong>
                  <span>{decision.action}{decision.side ? ` · ${decision.side}` : ""}</span>
                </div>
                <div>
                  <strong>{fixed(decision.signed_trade_imbalance, 3)}</strong>
                  <span>{decision.reason ?? decision.broker_status ?? "submitted"}</span>
                </div>
                <small>{dateTime(decision.created_at)}</small>
              </div>
            ))}
          </div>
        ) : <Empty text="No decisions yet. The runner only acts after a completed 30m market-hours bar." />}
      </section>
    </section>
  );
}

function Metric({ label, value, detail, tone }: { label: string; value: string; detail?: string; tone?: "positive" | "negative" }) {
  return (
    <div className={`paperMetric ${tone ?? ""}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      {detail ? <small>{detail}</small> : null}
    </div>
  );
}

function Empty({ text }: { text: string }) {
  return <div className="paperEmpty">{text}</div>;
}
