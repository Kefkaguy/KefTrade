import { CandleChart } from "@/components/CandleChart";
import { DataActions } from "@/components/DataActions";
import { Metric } from "@/components/Metric";
import { getCandles, getSignal } from "@/lib/api";
import { money } from "@/lib/format";

export default async function SymbolPage() {
  const candles = await getCandles(220).catch(() => []);
  const signal = await getSignal().catch(() => null);
  const latest = candles.at(-1);

  return (
    <div className="grid">
      <header className="pageHeader">
        <div>
          <h1>BTCUSDT / 4h</h1>
          <p className="muted">Candles, signal context, and trend pullback research state.</p>
        </div>
        <DataActions />
      </header>

      <div className="grid cols3">
        <Metric label="Open" value={latest ? money(latest.open) : "N/A"} />
        <Metric label="High" value={latest ? money(latest.high) : "N/A"} />
        <Metric label="Close" value={latest ? money(latest.close) : "N/A"} />
      </div>

      <section className="panel">
        <h2>Price chart</h2>
        <CandleChart candles={candles} />
      </section>

      <section className="panel">
        <h2>Signal explanation</h2>
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
          <p className="muted">No signal available yet.</p>
        )}
      </section>
    </div>
  );
}

