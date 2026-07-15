"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import {
  ArrowUpRight,
  BarChart3,
  Beaker,
  Bot,
  BrainCircuit,
  BriefcaseBusiness,
  CandlestickChart,
  ChevronRight,
  CircleDot,
  FileText,
  FlaskConical,
  Grid2X2,
  Layers3,
  ListChecks,
  Menu,
  Moon,
  Orbit,
  PanelLeftClose,
  Radar,
  Search,
  Settings,
  ShieldCheck,
  Sparkles,
  Sun,
  Target,
  Trophy,
  WalletCards,
  X
} from "lucide-react";
import { useEffect, useState } from "react";
import { CopilotPanel } from "@/components/CopilotPanel";
import { InterfaceModeProvider, useInterfaceMode } from "@/components/InterfaceModeContext";

const advancedPrimaryItems = [
  { href: "/mission-control", label: "Mission Control", icon: Radar },
  { href: "/dashboard", label: "Research", icon: BrainCircuit },
  { href: "/paper", label: "Forward validation", icon: CandlestickChart },
  { href: "/copilot", label: "AI Copilot", icon: Bot }
] as const;

const simplePrimaryItems = [
  { href: "/mission-control", label: "Dashboard", icon: Radar },
  { href: "/research-intelligence", label: "Candidates", icon: Trophy },
  { href: "/reports", label: "Reports", icon: FileText },
  { href: "/paper", label: "Paper Trading", icon: WalletCards }
] as const;

const currentToolGroups = [
  {
    label: "Research pipeline",
    items: [
      { href: "/mission-control", label: "Mission Control", detail: "Authoritative platform state", icon: Radar },
      { href: "/research-intelligence", label: "Candidates", detail: "Candidate lifecycle and evidence", icon: Trophy },
      { href: "/research", label: "Campaigns", detail: "Deterministic campaign execution", icon: Search },
      { href: "/experiments", label: "Experiments", detail: "Campaign jobs and results", icon: FlaskConical },
      { href: "/validation", label: "Validation", detail: "Promotion gates and diagnostics", icon: ShieldCheck },
      { href: "/reports", label: "Research records", detail: "Saved campaign evidence", icon: FileText }
    ]
  },
  {
    label: "Forward validation",
    items: [
      { href: "/paper", label: "Forward overview", detail: "Elite candidate deployments", icon: BriefcaseBusiness },
      { href: "/paper/orders", label: "Candidate orders", detail: "Simulation order lifecycle", icon: ArrowUpRight },
      { href: "/paper/positions", label: "Candidate positions", detail: "Open simulated exposure", icon: Layers3 },
      { href: "/paper/portfolio", label: "Forward performance", detail: "Candidate-linked account evidence", icon: WalletCards },
      { href: "/paper/deployments", label: "Deployments", detail: "Lifecycle and scheduler controls", icon: CandlestickChart }
    ]
  },
  {
    label: "Operations",
    items: [
      { href: "/market-intelligence", label: "Market regimes", detail: "Regime and drift diagnostics", icon: Orbit },
      { href: "/settings", label: "Scheduler", detail: "Workspace controls", icon: Settings },
      { href: "/journal", label: "Audit activity", detail: "Research and deployment timeline", icon: ListChecks },
      { href: "/assets", label: "Data diagnostics", detail: "Asset coverage and candles", icon: Grid2X2 },
      { href: "/copilot", label: "AI Copilot", detail: "Read-only assistant", icon: Bot }
    ]
  }
] as const;

const legacyToolGroups = [
  {
    label: "Historical research",
    items: [
      { href: "/dashboard", label: "Legacy dashboard", detail: "Earlier research overview", icon: BrainCircuit },
      { href: "/alpha", label: "Alpha discovery", detail: "Earlier candidate generation", icon: Sparkles },
      { href: "/strategy-discovery", label: "Strategy mutations", detail: "Generated rule variants", icon: Sparkles },
      { href: "/promising", label: "Parameter explorer", detail: "Cross-asset parameter rankings", icon: Target }
    ]
  },
  {
    label: "Specialist archives",
    items: [
      { href: "/portfolio", label: "Candidate lineage", detail: "Evidence drift and notebooks", icon: Layers3 },
      { href: "/hypotheses", label: "Research questions", detail: "Historical hypothesis records", icon: Beaker },
      { href: "/backtest", label: "Evidence replay", detail: "Replay deterministic strategy logic", icon: BarChart3 }
    ]
  }
] as const;

const toolGroups = [...currentToolGroups, ...legacyToolGroups] as const;

const allItems: Array<{ href: string; label: string }> = [
  ...advancedPrimaryItems.map(({ href, label }) => ({ href, label })),
  ...simplePrimaryItems.map(({ href, label }) => ({ href, label })),
  ...toolGroups.flatMap((group) => group.items.map(({ href, label }) => ({ href, label })))
];

export function AppShell({ children }: { children: React.ReactNode }) {
  return (
    <InterfaceModeProvider>
      <AppShellInner>{children}</AppShellInner>
    </InterfaceModeProvider>
  );
}

function AppShellInner({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const reduceMotion = useReducedMotion();
  const { mode, setMode } = useInterfaceMode();
  const [darkMode, setDarkMode] = useState(true);
  const [copilotOpen, setCopilotOpen] = useState(false);
  const [toolboxOpen, setToolboxOpen] = useState(false);
  const [mobileNavOpen, setMobileNavOpen] = useState(false);

  useEffect(() => {
    if (window.localStorage.getItem("keftrade-theme") === "light") setDarkMode(false);
  }, []);

  useEffect(() => {
    document.documentElement.dataset.theme = darkMode ? "dark" : "light";
    window.localStorage.setItem("keftrade-theme", darkMode ? "dark" : "light");
  }, [darkMode]);

  useEffect(() => {
    setToolboxOpen(false);
    setMobileNavOpen(false);
  }, [pathname]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setCopilotOpen((value) => !value);
      }
      if (event.key === "/" && !isTypingTarget(event.target)) {
        event.preventDefault();
        setToolboxOpen(true);
      }
      if (event.key === "Escape") {
        setToolboxOpen(false);
        setMobileNavOpen(false);
        setCopilotOpen(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const activeItem = [...allItems]
    .sort((a, b) => b.href.length - a.href.length)
    .find((item) => item.href === "/" ? pathname === "/" : pathname === item.href || pathname.startsWith(`${item.href}/`));
  const primaryItems = mode === "simple" ? simplePrimaryItems : advancedPrimaryItems;

  return (
    <div className={`appShell evidenceShell ${copilotOpen ? "withCopilot" : ""}`}>
      <div className="ambientField" aria-hidden="true"><span className="ambientOrb orbOne" /><span className="ambientOrb orbTwo" /><span className="ambientNoise" /></div>
      <aside className={`sidebar commandRail ${mobileNavOpen ? "mobileOpen" : ""}`}>
        <div className="railHeader">
          <Link href="/mission-control" className="brand" aria-label="KefTrade Mission Control"><span className="brandMark"><CircleDot size={20} /></span><span className="brandCopy"><strong>KefTrade</strong><small>{mode === "simple" ? "Simple view" : "Evidence OS"}</small></span></Link>
          <button className="railClose" type="button" onClick={() => setMobileNavOpen(false)} aria-label="Close navigation"><PanelLeftClose size={18} /></button>
        </div>
        <div className="railContext"><span>Workspace 01</span><strong>{mode === "simple" ? "Everyday summary" : "Quant research"}</strong></div>
        <nav className="navList" aria-label="Primary navigation">
          <span className="navSectionLabel">{mode === "simple" ? "Simple workspace" : "Core surfaces"}</span>
          {primaryItems.map((item, index) => {
            const Icon = item.icon;
            const active = pathname === item.href || pathname.startsWith(`${item.href}/`);
            return <Link key={item.href} href={item.href} className={active ? "active" : ""}><span className="navIndex">0{index + 1}</span><span className="navIcon"><Icon size={17} /></span><span>{item.label}</span><ChevronRight className="navArrow" size={14} /></Link>;
          })}
        </nav>
        <button className="toolboxTrigger" type="button" onClick={() => setToolboxOpen(true)}><span className="toolboxIcon"><Grid2X2 size={17} /></span><span><strong>{mode === "simple" ? "Advanced instruments" : "All instruments"}</strong><small>{mode === "simple" ? "Open professional tools" : "Full research platform"}</small></span><span className="keycap">/</span></button>
        <div className="sidebarStatus"><span className="statusPulse"><span /></span><div><strong>Simulation protected</strong><span>Live routing is physically disabled</span></div><ShieldCheck size={16} /></div>
      </aside>

      <div className="shellWorkspace">
        <header className="topBar systemBar">
          <div className="systemIdentity"><button className="mobileMenu" type="button" onClick={() => setMobileNavOpen(true)} aria-label="Open navigation"><Menu size={19} /></button><div className="routeIdentity"><span className="routeCode">KT / {activeItem?.label ?? "Workspace"}</span><strong>Evidence before execution</strong></div></div>
          <div className="systemPulse"><span className="systemPulseDot" /><span>Research engine online</span><span className="systemTime">UTC-07</span></div>
          <div className="topActions"><InterfaceModeToggle mode={mode} setMode={setMode} /><button className="iconButton" type="button" onClick={() => setDarkMode((value) => !value)} aria-label="Toggle color theme">{darkMode ? <Sun size={17} /> : <Moon size={17} />}</button><button className="askKefButton" type="button" onClick={() => setCopilotOpen((value) => !value)}><Sparkles size={15} /><span>Ask Kef</span><kbd>Ctrl K</kbd></button></div>
        </header>
        <AnimatePresence mode="wait" initial={false}>
          <motion.main key={pathname} className="content" initial={reduceMotion ? false : { opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} exit={reduceMotion ? undefined : { opacity: 0, y: -8 }} transition={{ duration: reduceMotion ? 0 : 0.34, ease: [0.22, 1, 0.36, 1] }}>{children}</motion.main>
        </AnimatePresence>
      </div>

      <AnimatePresence>{copilotOpen ? <motion.div className="copilotDock" initial={reduceMotion ? false : { x: 420, opacity: 0 }} animate={{ x: 0, opacity: 1 }} exit={{ x: 420, opacity: 0 }} transition={{ type: "spring", damping: 32, stiffness: 320 }}><button className="copilotClose" type="button" onClick={() => setCopilotOpen(false)} aria-label="Close AI Copilot"><X size={17} /></button><CopilotPanel /></motion.div> : null}</AnimatePresence>

      <AnimatePresence>{toolboxOpen ? <motion.div className="toolboxBackdrop" role="dialog" aria-modal="true" aria-label="Research instruments" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onMouseDown={(event) => { if (event.target === event.currentTarget) setToolboxOpen(false); }}><motion.section className="toolboxPanel" initial={reduceMotion ? false : { opacity: 0, y: 28, scale: 0.97 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0, y: 16, scale: 0.98 }} transition={{ duration: 0.32, ease: [0.22, 1, 0.36, 1] }}><header><div><span className="sectionLabel">{mode === "simple" ? "Professional platform" : "Instrument index"}</span><h2>Current research and forward validation</h2><p>Phase 9.12 candidate evidence, deployment operations, and system controls.</p></div><button className="iconButton" type="button" onClick={() => setToolboxOpen(false)} aria-label="Close instruments"><X size={18} /></button></header><section className="toolboxCurrent" aria-labelledby="current-tools-title"><div className="toolboxSectionHeading"><span id="current-tools-title">Current workflow</span><small>Authoritative candidate lifecycle</small></div><div className="toolboxGroups">{currentToolGroups.map((group) => <ToolboxGroup key={group.label} group={group} />)}</div></section><details className="toolboxLegacy"><summary><span><strong>Legacy and specialist tools</strong><small>Historical discovery, replay, and notebook surfaces</small></span><span className="toolboxLegacyCount">{legacyToolGroups.reduce((count, group) => count + group.items.length, 0)} tools</span><ChevronRight className="toolboxLegacyChevron" size={17} /></summary><div className="toolboxLegacyBody"><div className="toolboxGroups toolboxLegacyGroups">{legacyToolGroups.map((group) => <ToolboxGroup key={group.label} group={group} legacy />)}</div></div></details></motion.section></motion.div> : null}</AnimatePresence>
    </div>
  );
}

function ToolboxGroup({ group, legacy = false }: { group: (typeof currentToolGroups)[number] | (typeof legacyToolGroups)[number]; legacy?: boolean }) {
  return (
    <div className="toolboxGroup">
      <span>{group.label}</span>
      {group.items.map((item) => {
        const Icon = item.icon;
        return <Link href={item.href} key={item.href}><span className="toolLinkIcon"><Icon size={18} /></span><span><strong>{item.label}</strong><small>{item.detail}</small></span>{legacy ? <span className="legacyTag">Legacy</span> : <ArrowUpRight size={14} />}</Link>;
      })}
    </div>
  );
}

function InterfaceModeToggle({ mode, setMode }: { mode: "simple" | "advanced"; setMode: (mode: "simple" | "advanced") => void }) {
  return (
    <div className="modeToggle" role="group" aria-label="Interface mode">
      <button type="button" className={mode === "simple" ? "active" : ""} onClick={() => setMode("simple")} aria-pressed={mode === "simple"}>
        <span>{mode === "simple" ? "◉" : "○"}</span> Simple Mode
      </button>
      <button type="button" className={mode === "advanced" ? "active" : ""} onClick={() => setMode("advanced")} aria-pressed={mode === "advanced"}>
        <span>{mode === "advanced" ? "◉" : "○"}</span> Advanced Mode
      </button>
    </div>
  );
}

function isTypingTarget(target: EventTarget | null) {
  return target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement;
}
