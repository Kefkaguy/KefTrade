import { AlphaValidationRunner } from "@/components/AlphaValidationRunner";

export default function ValidationPage() {
  return (
    <div className="grid">
      <header className="pageHeader">
        <div>
          <h1>Alpha Validation</h1>
          <p className="muted">Validate discovered alpha across assets, timeframes, regimes, bootstrap samples, and Monte Carlo paths.</p>
        </div>
      </header>
      <AlphaValidationRunner />
    </div>
  );
}
