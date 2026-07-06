import { DataActions } from "@/components/DataActions";
import { Metric } from "@/components/Metric";
import { getCandles, getSignal } from "@/lib/api";
import { money, number } from "@/lib/format";

export default async function DashboardPage() {
  const candles = await getCandles(2).catch(() => []);
  const signal = await getSignal().catch(() => null);
  const latest = candles.at(-1);

  return (
    <div className="grid">
      <header className="pageHeader">
        <div>
          <h1>KefTrade research dashboard</h1>
          <p className="muted">US stock research architecture. BTCUSDT 4h is the current deterministic data environment.</p>
        </div>
        <DataActions />
      </header>

      <div className="grid cols3">
        <Metric label="Latest close" value={latest ? money(latest.close) : "No candles"} />
        <Metric label="Signal" value={signal?.signal ?? "No signal"} />
        <Metric label="Risk / reward" value={signal?.risk_reward ? number(signal.risk_reward) : "N/A"} />
      </div>

      <div className="grid cols2">
        <section className="panel">
          <h2>Current research signal</h2>
          {signal ? (
            <div className="grid">
              <span className={`status ${signal.signal}`}>{signal.signal}</span>
              <ul className="list">
                {signal.explanation.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            </div>
          ) : (
            <p className="muted">Sync candles, calculate features, then request a signal.</p>
          )}
        </section>
        <section className="panel">
          <h2>MVP constraints</h2>
          <ul className="list">
            <li>BTCUSDT and 4h only for v0.1 validation.</li>
            <li>Market data flows through a provider abstraction.</li>
            <li>No Model Engine or trained model in v0.1.</li>
            <li>No paper trading or live execution.</li>
            <li>Backtests include fees, slippage, and walk-forward validation.</li>
          </ul>
        </section>
      </div>
    </div>
  );
}
