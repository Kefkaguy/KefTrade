"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { Aperture, ArrowUpRight, BarChart3, Beaker, Bot, BrainCircuit, BriefcaseBusiness, CandlestickChart, ChevronRight, CircleDot, FlaskConical, Grid2X2, Layers3, Menu, Moon, Orbit, PanelLeftClose, Search, Settings, ShieldCheck, Sparkles, Sun, Target, X } from "lucide-react";
import { useEffect, useState } from "react";
import { CopilotPanel } from "@/components/CopilotPanel";

const primaryItems = [
  { href: "/", label: "Command", icon: Aperture },
  { href: "/dashboard", label: "Research", icon: BrainCircuit },
  { href: "/portfolio", label: "Portfolio", icon: BriefcaseBusiness },
  { href: "/paper", label: "Paper lab", icon: CandlestickChart },
  { href: "/copilot", label: "AI Copilot", icon: Bot }
] as const;

const toolGroups = [
  { label: "Discover", items: [
    { href: "/research", label: "Strategy research", detail: "Run deterministic evidence", icon: Search },
    { href: "/promising", label: "Promising candidates", detail: "Rank cross-asset ideas", icon: Target },
    { href: "/experiments", label: "Experiments", detail: "Inspect strategy sweeps", icon: FlaskConical },
    { href: "/hypotheses", label: "Hypotheses", detail: "Shape research questions", icon: Beaker }
  ] },
  { label: "Validate", items: [
    { href: "/alpha", label: "Alpha discovery", detail: "Generate candidates", icon: Sparkles },
    { href: "/validation", label: "Validation runs", detail: "Challenge the evidence", icon: ShieldCheck },
    { href: "/backtest", label: "Backtest", detail: "Replay strategy logic", icon: BarChart3 },
    { href: "/market-intelligence", label: "Market intelligence", detail: "Regimes and drift", icon: Orbit }
  ] },
  { label: "Operate", items: [
    { href: "/paper/orders", label: "Paper orders", detail: "Simulated order lifecycle", icon: ArrowUpRight },
    { href: "/paper/positions", label: "Paper positions", detail: "Long-only exposure", icon: Layers3 },
    { href: "/assets", label: "Asset library", detail: "Coverage and candles", icon: Grid2X2 },
    { href: "/settings", label: "Settings", detail: "Workspace controls", icon: Settings }
  ] }
] as const;

const allItems: Array<{ href: string; label: string }> = [
  ...primaryItems.map(({ href, label }) => ({ href, label })),
  ...toolGroups.flatMap((group) => group.items.map(({ href, label }) => ({ href, label })))
];

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const reduceMotion = useReducedMotion();
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

  const activeItem = [...allItems].sort((a, b) => b.href.length - a.href.length).find((item) => item.href === "/" ? pathname === "/" : pathname === item.href || pathname.startsWith(`${item.href}/`));

  return (
    <div className={`appShell evidenceShell ${copilotOpen ? "withCopilot" : ""}`}>
      <div className="ambientField" aria-hidden="true"><span className="ambientOrb orbOne" /><span className="ambientOrb orbTwo" /><span className="ambientNoise" /></div>
      <aside className={`sidebar commandRail ${mobileNavOpen ? "mobileOpen" : ""}`}>
        <div className="railHeader">
          <Link href="/" className="brand" aria-label="KefTrade command center"><span className="brandMark"><CircleDot size={20} /></span><span className="brandCopy"><strong>KefTrade</strong><small>Evidence OS</small></span></Link>
          <button className="railClose" type="button" onClick={() => setMobileNavOpen(false)} aria-label="Close navigation"><PanelLeftClose size={18} /></button>
        </div>
        <div className="railContext"><span>Workspace 01</span><strong>Quant research</strong></div>
        <nav className="navList" aria-label="Primary navigation">
          <span className="navSectionLabel">Core surfaces</span>
          {primaryItems.map((item, index) => {
            const Icon = item.icon;
            const active = item.href === "/" ? pathname === "/" : pathname === item.href || pathname.startsWith(`${item.href}/`);
            return <Link key={item.href} href={item.href} className={active ? "active" : ""}><span className="navIndex">0{index + 1}</span><span className="navIcon"><Icon size={17} /></span><span>{item.label}</span><ChevronRight className="navArrow" size={14} /></Link>;
          })}
        </nav>
        <button className="toolboxTrigger" type="button" onClick={() => setToolboxOpen(true)}><span className="toolboxIcon"><Grid2X2 size={17} /></span><span><strong>All instruments</strong><small>12 research tools</small></span><span className="keycap">/</span></button>
        <div className="sidebarStatus"><span className="statusPulse"><span /></span><div><strong>Simulation protected</strong><span>Live routing is physically disabled</span></div><ShieldCheck size={16} /></div>
      </aside>

      <div className="shellWorkspace">
        <header className="topBar systemBar">
          <div className="systemIdentity"><button className="mobileMenu" type="button" onClick={() => setMobileNavOpen(true)} aria-label="Open navigation"><Menu size={19} /></button><div className="routeIdentity"><span className="routeCode">KT / {activeItem?.label ?? "Workspace"}</span><strong>Evidence before execution</strong></div></div>
          <div className="systemPulse"><span className="systemPulseDot" /><span>Research engine online</span><span className="systemTime">UTC−07</span></div>
          <div className="topActions"><button className="iconButton" type="button" onClick={() => setDarkMode((value) => !value)} aria-label="Toggle color theme">{darkMode ? <Sun size={17} /> : <Moon size={17} />}</button><button className="askKefButton" type="button" onClick={() => setCopilotOpen((value) => !value)}><Sparkles size={15} /><span>Ask Kef</span><kbd>⌘K</kbd></button></div>
        </header>
        <AnimatePresence mode="wait" initial={false}>
          <motion.main key={pathname} className="content" initial={reduceMotion ? false : { opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} exit={reduceMotion ? undefined : { opacity: 0, y: -8 }} transition={{ duration: reduceMotion ? 0 : 0.34, ease: [0.22, 1, 0.36, 1] }}>{children}</motion.main>
        </AnimatePresence>
      </div>

      <AnimatePresence>{copilotOpen ? <motion.div className="copilotDock" initial={reduceMotion ? false : { x: 420, opacity: 0 }} animate={{ x: 0, opacity: 1 }} exit={{ x: 420, opacity: 0 }} transition={{ type: "spring", damping: 32, stiffness: 320 }}><button className="copilotClose" type="button" onClick={() => setCopilotOpen(false)} aria-label="Close AI Copilot"><X size={17} /></button><CopilotPanel /></motion.div> : null}</AnimatePresence>

      <AnimatePresence>{toolboxOpen ? <motion.div className="toolboxBackdrop" role="dialog" aria-modal="true" aria-label="Research instruments" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onMouseDown={(event) => { if (event.target === event.currentTarget) setToolboxOpen(false); }}><motion.section className="toolboxPanel" initial={reduceMotion ? false : { opacity: 0, y: 28, scale: 0.97 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0, y: 16, scale: 0.98 }} transition={{ duration: 0.32, ease: [0.22, 1, 0.36, 1] }}><header><div><span className="sectionLabel">Instrument index</span><h2>Move through the evidence.</h2></div><button className="iconButton" type="button" onClick={() => setToolboxOpen(false)} aria-label="Close instruments"><X size={18} /></button></header><div className="toolboxGroups">{toolGroups.map((group) => <div className="toolboxGroup" key={group.label}><span>{group.label}</span>{group.items.map((item) => { const Icon = item.icon; return <Link href={item.href} key={item.href}><span className="toolLinkIcon"><Icon size={18} /></span><span><strong>{item.label}</strong><small>{item.detail}</small></span><ArrowUpRight size={14} /></Link>; })}</div>)}</div></motion.section></motion.div> : null}</AnimatePresence>
    </div>
  );
}

function isTypingTarget(target: EventTarget | null) {
  return target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement;
}
