"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { ArrowRight, Clock, ShieldAlert } from "lucide-react";
import { getIntradayLabOverview, type IntradayLabOverview } from "@/lib/api";

export function IntradayLabTeaser() {
  const [overview, setOverview] = useState<IntradayLabOverview | null>(null);

  useEffect(() => {
    let active = true;
    getIntradayLabOverview()
      .then((result) => {
        if (active) setOverview(result);
      })
      .catch(() => undefined);
    return () => {
      active = false;
    };
  }, []);

  const archived = (overview?.strategies ?? []).filter((s) => s.status === "archived");
  const planned = (overview?.strategies ?? []).filter((s) => s.status === "planned");
  const totalTrades = archived.reduce((sum, strategy) => sum + (strategy.trades ?? 0), 0);

  return (
    <Link href="/intraday-research" className="intradayTeaser">
      <div className="intradayTeaserHeader">
        <span className="eyebrow"><Clock size={13} /> Intraday Research Lab</span>
        <span className="intradayTeaserBadge">Infrastructure Complete</span>
      </div>
      <div className="intradayTeaserBody">
        <div className="intradayTeaserStrategies">
          {archived.map((strategy) => (
            <span key={strategy.id} className="intradayTeaserStrategy archived">{strategy.name} {strategy.version} · Archived</span>
          ))}
          {planned.map((strategy) => (
            <span key={strategy.id} className="intradayTeaserStrategy">{strategy.name} · Planned</span>
          ))}
        </div>
        {archived.length ? (
          <div className="intradayTeaserStats">
            <span><strong>{totalTrades.toLocaleString()}</strong> simulated trades</span>
            <span><strong>15m</strong> / <strong>30m</strong> timeframes</span>
            <span className="warn"><ShieldAlert size={12} /> {archived.length} archived negative result{archived.length === 1 ? "" : "s"}</span>
          </div>
        ) : null}
      </div>
      <span className="intradayTeaserLink">Open the Intraday Lab <ArrowRight size={14} /></span>
    </Link>
  );
}
