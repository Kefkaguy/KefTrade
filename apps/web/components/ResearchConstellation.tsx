"use client";

import { motion, useReducedMotion } from "framer-motion";

const nodes = [
  { label: "Price", x: 45, y: 24, delay: 0 },
  { label: "Regime", x: 82, y: 39, delay: 0.12 },
  { label: "Risk", x: 75, y: 77, delay: 0.24 },
  { label: "Alpha", x: 29, y: 81, delay: 0.36 },
  { label: "Evidence", x: 16, y: 45, delay: 0.48 }
] as const;

export function ResearchConstellation() {
  const reduceMotion = useReducedMotion();
  return (
    <div className="constellation" aria-hidden="true">
      <div className="constellationMeta"><span>Live research graph</span><strong>05 signals connected</strong></div>
      <svg className="constellationLines" viewBox="0 0 100 100" preserveAspectRatio="none">{nodes.map((node) => <motion.path key={node.label} d={`M50 52 L${node.x} ${node.y}`} fill="none" stroke="currentColor" strokeWidth="0.35" initial={reduceMotion ? false : { pathLength: 0, opacity: 0 }} animate={{ pathLength: 1, opacity: 0.55 }} transition={{ duration: 1.1, delay: node.delay + 0.2 }} />)}</svg>
      <motion.div className="constellationCore" initial={reduceMotion ? false : { scale: 0.75, opacity: 0 }} animate={{ scale: 1, opacity: 1 }} transition={{ type: "spring", stiffness: 170, damping: 18 }}><span className="coreOrbit orbitA" /><span className="coreOrbit orbitB" /><span className="corePulse" /><strong>KT</strong><small>Research core</small></motion.div>
      {nodes.map((node) => <motion.div className="constellationNode" key={node.label} style={{ left: `${node.x}%`, top: `${node.y}%` }} initial={reduceMotion ? false : { scale: 0.5, opacity: 0 }} animate={{ scale: 1, opacity: 1 }} transition={{ type: "spring", stiffness: 220, damping: 19, delay: node.delay + 0.35 }}><span />{node.label}</motion.div>)}
      <motion.div className="constellationReadout" initial={reduceMotion ? false : { opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.9 }}><span>System confidence</span><strong>Evidence first</strong></motion.div>
    </div>
  );
}
