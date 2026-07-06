import { StrategyResearchRunner } from "@/components/StrategyResearchRunner";

export default function ResearchPage() {
  return (
    <div className="grid">
      <header className="pageHeader">
        <div>
          <h1>Strategy Research</h1>
          <p className="muted">Compare deterministic strategy parameter sets under identical BTCUSDT 4h conditions.</p>
        </div>
      </header>
      <StrategyResearchRunner />
    </div>
  );
}
