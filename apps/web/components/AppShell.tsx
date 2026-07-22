"use client";

import Image from "next/image";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import {
  Activity,
  ArrowUpRight,
  Bot,
  BrainCircuit,
  CandlestickChart,
  Command,
  FileText,
  FlaskConical,
  Grid2X2,
  Menu,
  Radar,
  ScanSearch,
  Search,
  Settings,
  ShieldCheck,
  Sparkles,
  Trophy,
  X
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useEffect, useState } from "react";
import { CopilotPanel } from "@/components/CopilotPanel";

type NavigationItem = {
  href: string;
  label: string;
  icon: LucideIcon;
  detail?: string;
};

const primaryNavigation: NavigationItem[] = [
  { href: "/", label: "Home", icon: Grid2X2 },
  { href: "/research", label: "Research", icon: FlaskConical },
  { href: "/research-intelligence", label: "Candidates", icon: Trophy },
  { href: "/elite-builder", label: "Elite Builder", icon: BrainCircuit },
  { href: "/paper", label: "Forward validation", icon: CandlestickChart },
  { href: "/reports", label: "Reports", icon: FileText },
  { href: "/mission-control", label: "Mission Control", icon: Radar },
  { href: "/diagnostics", label: "Diagnostics", icon: ScanSearch }
];

const workspaceGroups: Array<{ label: string; items: NavigationItem[] }> = [
  {
    label: "Research",
    items: [
      { href: "/research", label: "Campaigns", detail: "Launch and monitor research campaigns", icon: FlaskConical },
      { href: "/experiments", label: "Experiments", detail: "Inspect active jobs and evidence", icon: BrainCircuit },
      { href: "/validation", label: "Validation", detail: "Review promotion gates", icon: ShieldCheck },
      { href: "/elite-builder", label: "Elite portfolio builder", detail: "Construct constrained diversified portfolios", icon: BrainCircuit },
      { href: "/market-intelligence", label: "Market intelligence", detail: "Regimes, drift, and context", icon: Activity }
    ]
  },
  {
    label: "Operations",
    items: [
      { href: "/assets", label: "Data coverage", detail: "Assets, candles, and freshness", icon: Search },
      { href: "/journal", label: "Activity journal", detail: "Research and deployment timeline", icon: FileText },
      { href: "/settings", label: "Settings", detail: "Scheduler and workspace controls", icon: Settings },
      { href: "/copilot", label: "Kef Copilot", detail: "Explore evidence conversationally", icon: Bot }
    ]
  }
];

const allNavigation = [
  ...primaryNavigation,
  ...workspaceGroups.flatMap((group) => group.items)
];

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const reduceMotion = useReducedMotion();
  const [mobileOpen, setMobileOpen] = useState(false);
  const [workspaceOpen, setWorkspaceOpen] = useState(false);
  const [copilotOpen, setCopilotOpen] = useState(false);

  useEffect(() => {
    setMobileOpen(false);
    setWorkspaceOpen(false);
  }, [pathname]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setWorkspaceOpen((value) => !value);
      }
      if (event.key === "Escape") {
        setMobileOpen(false);
        setWorkspaceOpen(false);
        setCopilotOpen(false);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  const currentRoute = [...allNavigation]
    .sort((a, b) => b.href.length - a.href.length)
    .find((item) => item.href === "/" ? pathname === "/" : pathname === item.href || pathname.startsWith(`${item.href}/`));

  return (
    <div className={`appShell ${copilotOpen ? "withCopilot" : ""}`}>
      <aside className={`appRail ${mobileOpen ? "isOpen" : ""}`}>
        <div className="railBrandRow">
          <Link href="/" className="brand" aria-label="KefTrade home">
            <Image className="brandImage" src="/kefcore-mark.png" alt="" width={48} height={48} priority />
            <span><strong>KefTrade</strong><small>Research workspace</small></span>
          </Link>
          <button className="iconButton mobileOnly" type="button" onClick={() => setMobileOpen(false)} aria-label="Close navigation"><X size={18} /></button>
        </div>

        <nav className="primaryNavigation" aria-label="Primary navigation">
          {primaryNavigation.map((item) => {
            const Icon = item.icon;
            const active = item.href === "/" ? pathname === "/" : pathname === item.href || pathname.startsWith(`${item.href}/`);
            return (
              <Link key={item.href} href={item.href} className={active ? "active" : ""} aria-current={active ? "page" : undefined}>
                <Icon size={18} /><span>{item.label}</span>
                {active ? <motion.i layoutId="navigation-active" transition={{ type: "spring", stiffness: 360, damping: 32 }} /> : null}
              </Link>
            );
          })}
        </nav>

        <button className="workspaceLauncher" type="button" onClick={() => setWorkspaceOpen(true)}>
          <span className="launcherIcon"><Command size={17} /></span>
          <span><strong>Open workspace</strong><small>Tools and utilities</small></span>
          <kbd>Ctrl K</kbd>
        </button>

        <div className="railSafety">
          <span className="liveDot" />
          <span><strong>Simulation protected</strong><small>Live routing disabled</small></span>
          <ShieldCheck size={17} />
        </div>
      </aside>

      <div className="appWorkspace">
        <header className="appHeader">
          <div className="headerIdentity">
            <button className="iconButton menuButton" type="button" onClick={() => setMobileOpen(true)} aria-label="Open navigation"><Menu size={19} /></button>
            <span><small>Workspace</small><strong>{currentRoute?.label ?? "KefTrade"}</strong></span>
          </div>
          <div className="headerActions">
            <span className="engineState"><span className="liveDot" /> Research engine online</span>
            <button className="button secondary compact" type="button" onClick={() => setCopilotOpen((value) => !value)}><Sparkles size={15} /> Ask Kef</button>
          </div>
        </header>

        <AnimatePresence mode="wait" initial={false}>
          <motion.main
            key={pathname}
            className="appContent"
            initial={reduceMotion ? false : { opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            exit={reduceMotion ? undefined : { opacity: 0, y: -6 }}
            transition={{ duration: reduceMotion ? 0 : 0.32, ease: [0.22, 1, 0.36, 1] }}
          >
            {children}
          </motion.main>
        </AnimatePresence>
      </div>

      <AnimatePresence>
        {workspaceOpen ? (
          <motion.div className="dialogBackdrop" role="dialog" aria-modal="true" aria-label="Workspace tools" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onMouseDown={(event) => { if (event.target === event.currentTarget) setWorkspaceOpen(false); }}>
            <motion.section className="commandDialog" initial={reduceMotion ? false : { opacity: 0, y: 20, scale: 0.98 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0, y: 10, scale: 0.985 }} transition={{ type: "spring", stiffness: 320, damping: 30 }}>
              <header><div><span className="eyebrow">Workspace</span><h2>Where do you want to go?</h2></div><button className="iconButton" type="button" onClick={() => setWorkspaceOpen(false)} aria-label="Close workspace"><X size={18} /></button></header>
              <div className="commandGroups">
                {workspaceGroups.map((group) => (
                  <section key={group.label}>
                    <span className="sectionLabel">{group.label}</span>
                    <div className="commandLinks">
                      {group.items.map((item) => {
                        const Icon = item.icon;
                        return <Link key={item.href} href={item.href}><span className="commandIcon"><Icon size={19} /></span><span><strong>{item.label}</strong><small>{item.detail}</small></span><ArrowUpRight size={15} /></Link>;
                      })}
                    </div>
                  </section>
                ))}
              </div>
            </motion.section>
          </motion.div>
        ) : null}
      </AnimatePresence>

      <AnimatePresence>
        {copilotOpen ? (
          <motion.aside className="copilotDock" initial={reduceMotion ? false : { x: 420, opacity: 0 }} animate={{ x: 0, opacity: 1 }} exit={{ x: 420, opacity: 0 }} transition={{ type: "spring", stiffness: 320, damping: 32 }}>
            <button className="iconButton copilotClose" type="button" onClick={() => setCopilotOpen(false)} aria-label="Close Kef Copilot"><X size={18} /></button>
            <CopilotPanel />
          </motion.aside>
        ) : null}
      </AnimatePresence>
    </div>
  );
}
