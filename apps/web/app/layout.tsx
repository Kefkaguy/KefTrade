import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "KefTrade",
  description: "BTCUSDT 4h trading intelligence research MVP"
};

const navItems = [
  { href: "/dashboard", label: "Dashboard" },
  { href: "/symbol/BTCUSDT", label: "BTCUSDT" },
  { href: "/backtest", label: "Backtest" },
  { href: "/risk-settings", label: "Risk" }
];

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>
        <div className="shell">
          <aside className="sidebar">
            <Link href="/dashboard" className="brand">
              <span className="brandMark">SF</span>
              <span>
                <strong>KefTrade</strong>
                <small>BTC research</small>
              </span>
            </Link>
            <nav>
              {navItems.map((item) => (
                <Link key={item.href} href={item.href}>
                  {item.label}
                </Link>
              ))}
            </nav>
            <div className="guardrail">
              <strong>Execution locked</strong>
              <span>No Model Engine, paper trading, live orders, leverage, or futures in v0.1.</span>
            </div>
          </aside>
          <main className="content">{children}</main>
        </div>
      </body>
    </html>
  );
}
