import { StrategyResearchRunner } from "@/components/StrategyResearchRunner";

export default function ResearchPage() {
  return (
    <div className="grid">
      <header className="pageHeader">
        <div>
          <h1>Strategy Research</h1>
          <p className="muted">Compare deterministic strategy definitions under identical BTCUSDT 4h research conditions.</p>
        </div>
      </header>
      <StrategyResearchRunner />
    </div>
  );
}
