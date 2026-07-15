import { ResearchCommandCenterDashboard } from "@/components/ResearchCommandCenter";

export default function ResearchPage() {
  return (
    <div className="grid researchPage">
      <header className="pageHeader">
        <div>
          <h1>Research Command Center</h1>
          <p className="muted">Phase 9.6 candidate quality optimization from authoritative campaign evidence.</p>
        </div>
      </header>
      <ResearchCommandCenterDashboard />
    </div>
  );
}
