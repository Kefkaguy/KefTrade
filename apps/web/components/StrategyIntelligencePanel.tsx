"use client";

import { useEffect, useState } from "react";
import { Dna, FlaskConical, Info, ShieldAlert } from "lucide-react";
import {
  getPhase13Analytics,
  getStrategyDna,
  type Phase13CampaignAnalytics,
  type StrategyDnaResponse,
} from "@/lib/api";
import { Card, DataTable, EmptyState } from "@/components/ResearchUI";

function num(value: number | null | undefined, digits = 2) {
  if (value == null) return "—";
  if (value === Infinity) return "∞";
  return value.toFixed(digits);
}

function pct(value: number | null | undefined, digits = 1) {
  return value == null ? "—" : `${(value * 100).toFixed(digits)}%`;
}

/** Evidence tiers are the core UI contract of Phase 13.5: a reader must never
 *  mistake a suggestive result for a reliable one. Tier reflects sample size
 *  and spread only — never how good the numbers look. */
const TIER_LABEL: Record<string, string> = {
  statistically_reliable: "Statistically reliable",
  descriptive: "Descriptive",
  exploratory: "Exploratory",
  insufficient_sample: "Insufficient sample",
};

const TIER_TONE: Record<string, string> = {
  statistically_reliable: "good",
  descriptive: "",
  exploratory: "warn",
  insufficient_sample: "muted",
};

function TierBadge({ tier }: { tier: string }) {
  return <em className={`familyTag ${TIER_TONE[tier] ?? "muted"}`}>{TIER_LABEL[tier] ?? tier}</em>;
}

export function StrategyIntelligencePanel({ campaignId }: { campaignId: number | null }) {
  const [dna, setDna] = useState<StrategyDnaResponse | null>(null);
  const [analytics, setAnalytics] = useState<Phase13CampaignAnalytics | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    getStrategyDna()
      .then((result) => active && setDna(result))
      .catch((reason) => active && setError(reason instanceof Error ? reason.message : "Could not load Strategy DNA."));
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    if (!campaignId) return;
    let active = true;
    getPhase13Analytics(campaignId)
      .then((result) => active && setAnalytics(result))
      .catch(() => {
        /* A campaign with no Phase 13 evidence is a normal state, not an error. */
      });
    return () => {
      active = false;
    };
  }, [campaignId]);

  return (
    <>
      <Card title="Strategy DNA" eyebrow="Behavioral fingerprints, Phase 13.1">
        <p className="intradayStrategySummary">
          DNA describes what a strategy <em>does to the market</em> — entry structure, dependencies, regime
          requirements — separately from its parameters. Two families can share a parameter shape without
          behaving alike, so behavioral similarity is measured independently of parameter similarity.
        </p>
        {error ? <div className="strategyLibraryError" role="alert">{error}</div> : null}
        {!dna ? <EmptyState title="Loading Strategy DNA" body="Reading the behavioral registry." /> : null}

        {dna ? (
          <>
            <div style={{ marginTop: 16 }}>
              <DataTable
                columns={["Family", "Version", "Behavior", "Entry structure", "Session", "Regime", "Evidence", "Execution"]}
                rows={dna.families.map((record) => [
                  record.family_architecture,
                  record.strategy_version,
                  String(record.dna.behavior_class ?? "—").replaceAll("_", " "),
                  String(record.dna.entry_structure ?? "—").replaceAll("_", " "),
                  String(record.dna.session_dependency ?? "—").replaceAll("_", " "),
                  (record.dna.required_regime ?? []).join(", "),
                  String(record.dna.evidence_confidence ?? "—").replaceAll("_", " "),
                  <em key="x" className="familyTag muted">{String(record.dna.execution_capability ?? "—").replaceAll("_", " ")}</em>,
                ])}
              />
            </div>

            {dna.behavioral_similarity.length ? (
              <div style={{ marginTop: 20 }}>
                <span className="sectionLabel">Most behaviorally similar pairs</span>
                <DataTable
                  columns={["Family A", "Family B", "Behavioral similarity"]}
                  rows={dna.behavioral_similarity.slice(0, 8).map((pair) => [
                    pair.a,
                    pair.b,
                    num(pair.behavioral_similarity),
                  ])}
                />
              </div>
            ) : null}
          </>
        ) : null}
      </Card>

      {analytics ? (
        <Card title="Strategy analytics" eyebrow={`Evidence tiers · Campaign ${analytics.campaign_id}`}>
          <div className="intradayPilotNote">
            <Info size={14} />
            <span>
              Every tier below reflects <strong>sample size and spread only</strong>, never how favorable the
              numbers are — a strong result on a handful of trades is still an insufficient sample.
            </span>
          </div>

          <div style={{ marginTop: 16 }}>
            <DataTable
              columns={["Family", "Jobs", "Promoted", "Trades", "Avg PF", "Avg expectancy", "Evidence"]}
              rows={analytics.families.map((family) => [
                family.architecture,
                family.jobs,
                `${family.promoted_jobs} (${pct(family.promotion_rate)})`,
                family.trades.toLocaleString(),
                num(family.avg_profit_factor),
                num(family.avg_expectancy),
                <TierBadge key="t" tier={family.evidence_tier} />,
              ])}
            />
          </div>

          <div className="intradayCandidateWhy" style={{ marginTop: 18 }}>
            <span>Diagnostic buckets</span>
            <ul>
              <li>
                Profitable but under-evidenced: {analytics.candidate_buckets.profitable_but_under_evidenced.length}
              </li>
              <li>Frequent but unprofitable: {analytics.candidate_buckets.frequent_but_unprofitable.length}</li>
              <li>Near-pass (just below the unchanged gate): {analytics.candidate_buckets.near_pass.length}</li>
            </ul>
          </div>

          <p className="intradayStrategyReason" style={{ marginTop: 14 }}>
            <ShieldAlert size={13} /> {analytics.causal_claims_disclaimer}
          </p>
        </Card>
      ) : null}
    </>
  );
}
