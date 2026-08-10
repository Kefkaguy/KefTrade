import type { ReactNode } from "react";

export function AppShell({ children }: { children: ReactNode }) {
  return (
    <div className="paperOnlyShell">
      <header className="paperOnlyHeader">
        <div>
          <span className="paperOnlyEyebrow">Alpaca Paper Lab</span>
          <strong>KefTrade</strong>
        </div>
        <span className="paperOnlyBadge">Fake money only</span>
      </header>
      <main className="paperOnlyMain">{children}</main>
    </div>
  );
}
