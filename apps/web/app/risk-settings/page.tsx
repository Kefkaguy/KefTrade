import { RiskForm } from "@/components/RiskForm";

export default function RiskSettingsPage() {
  return (
    <div className="grid">
      <header className="pageHeader">
        <div>
          <h1>Risk settings</h1>
          <p className="muted">Risk controls are capped. Live trading and leverage cannot be enabled in v0.1.</p>
        </div>
      </header>
      <section className="panel">
        <RiskForm />
      </section>
    </div>
  );
}

