import { BacktestRunner } from "@/components/BacktestRunner";

export default function BacktestPage() {
  return (
    <div className="grid">
      <header className="pageHeader">
        <div>
          <h1>Backtest</h1>
          <p className="muted">Run trend_pullback_v1 with fees, slippage, expectancy, and walk-forward validation.</p>
        </div>
      </header>
      <BacktestRunner />
    </div>
  );
}

