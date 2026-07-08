"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { CopilotPanel } from "@/components/CopilotPanel";

const navItems = [
  { href: "/", label: "Home", icon: "H" },
  { href: "/reports", label: "Reports", icon: "R" },
  { href: "/assets", label: "Assets", icon: "A" },
  { href: "/strategies", label: "Strategies", icon: "S" },
  { href: "/copilot", label: "AI Copilot", icon: "C" },
  { href: "/settings", label: "Settings", icon: "T" }
];

const advancedItems = [
  { href: "/dashboard", label: "Research Dashboard" },
  { href: "/portfolio", label: "Research Portfolio" },
  { href: "/research", label: "Strategy Research" },
  { href: "/promising", label: "Promising Candidates" },
  { href: "/experiments", label: "Experiments" },
  { href: "/hypotheses", label: "Hypotheses" },
  { href: "/alpha", label: "Alpha Discovery" },
  { href: "/validation", label: "Validation Runs" },
  { href: "/market-intelligence", label: "Market Intelligence" },
  { href: "/journal", label: "Research Journal" },
  { href: "/backtest", label: "Backtest" }
];

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const [darkMode, setDarkMode] = useState(false);
  const [copilotOpen, setCopilotOpen] = useState(false);

  useEffect(() => {
    const stored = window.localStorage.getItem("keftrade-theme");
    if (stored === "dark") setDarkMode(true);
  }, []);

  useEffect(() => {
    document.documentElement.dataset.theme = darkMode ? "dark" : "light";
    window.localStorage.setItem("keftrade-theme", darkMode ? "dark" : "light");
  }, [darkMode]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setCopilotOpen((value) => !value);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const activeSection = useMemo(() => {
    const match = navItems.find((item) => pathname === item.href || (item.href !== "/" && pathname.startsWith(`${item.href}/`)));
    const advanced = advancedItems.find((item) => pathname === item.href || pathname.startsWith(`${item.href}/`));
    return match?.label ?? advanced?.label ?? "Home";
  }, [pathname]);

  return (
    <div className={`appShell ${copilotOpen ? "withCopilot" : ""}`}>
      <aside className="sidebar">
        <Link href="/" className="brand">
          <span className="brandMark">K</span>
          <span>
            <strong>KefTrade</strong>
            <small>Research intelligence</small>
          </span>
        </Link>
        <nav className="navList" aria-label="Primary navigation">
          {navItems.map((item) => {
            const active = pathname === item.href || pathname.startsWith(`${item.href}/`);
            const isActive = item.href === "/" ? pathname === "/" : active;
            return (
              <Link key={item.href} href={item.href} className={isActive ? "active" : ""}>
                <span className="navIcon" aria-hidden="true">
                  {item.icon}
                </span>
                {item.label}
              </Link>
            );
          })}
        </nav>
        <details className="advancedNav">
          <summary>Advanced Research</summary>
          <div>
            {advancedItems.map((item) => (
              <Link key={item.href} href={item.href} className={pathname === item.href || pathname.startsWith(`${item.href}/`) ? "active" : ""}>
                {item.label}
              </Link>
            ))}
          </div>
        </details>
        <div className="sidebarStatus">
          <span className="pulseDot" />
          <div>
            <strong>Research only</strong>
            <span>No live execution, broker routing, or signal overrides.</span>
          </div>
        </div>
      </aside>

      <main className="content">
        <div className="topBar">
          <div>
            <span className="sectionLabel">{activeSection}</span>
            <strong>Quantitative research workspace</strong>
          </div>
          <div className="topActions">
            <label className="searchBox">
              <span>Search</span>
              <input placeholder="Assets, reports, strategies" />
            </label>
            <button className="iconButton" type="button" onClick={() => setDarkMode((value) => !value)} aria-label="Toggle dark mode">
              {darkMode ? "L" : "D"}
            </button>
            <button className="button compact" type="button" onClick={() => setCopilotOpen((value) => !value)}>
              AI Panel
            </button>
          </div>
        </div>
        {children}
      </main>

      {copilotOpen ? <CopilotPanel /> : null}
    </div>
  );
}
