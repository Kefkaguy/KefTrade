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
        {overview?.pilot ? (
          <div className="intradayTeaserStats">
            <span><strong>{overview.pilot.trades.toLocaleString()}</strong> simulated trades</span>
            <span><strong>15m</strong> / <strong>30m</strong> timeframes</span>
            <span className="warn"><ShieldAlert size={12} /> Archived negative result</span>
          </div>
        ) : null}
      </div>
      <span className="intradayTeaserLink">Open the Intraday Lab <ArrowRight size={14} /></span>
    </Link>
  );
}
