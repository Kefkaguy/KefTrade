import { AlphaDiscoveryRunner } from "@/components/AlphaDiscoveryRunner";

export default function AlphaPage() {
  return (
    <div className="grid">
      <header className="pageHeader">
        <div>
          <h1>Alpha Discovery Lab</h1>
          <p className="muted">Generate deterministic strategy candidates and reject unstable edges before any Model Engine work.</p>
        </div>
      </header>
      <AlphaDiscoveryRunner />
    </div>
  );
}
