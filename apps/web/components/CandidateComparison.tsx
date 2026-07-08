"use client";

import { useMemo, useState } from "react";
import type { CandidateComparisonRow, MetricDefinition } from "@/lib/api";
import { number, percent } from "@/lib/format";

export function CandidateComparison({
  rows,
  metrics
}: {
  rows: CandidateComparisonRow[];
  metrics: Record<string, MetricDefinition>;
}) {
  const defaults = rows.slice(0, 3).map((row) => row.candidate_id);
  const [selected, setSelected] = useState<string[]>(defaults);
  const selectedRows = useMemo(() => rows.filter((row) => selected.includes(row.candidate_id)), [rows, selected]);

  function toggle(candidateId: string) {
    setSelected((current) => (current.includes(candidateId) ? current.filter((item) => item !== candidateId) : [...current, candidateId].slice(-5)));
  }

  return (
    <div className="comparisonWorkspace">
      <div className="comparisonPicker">
        {rows.slice(0, 12).map((row) => (
          <label key={row.candidate_id} className={selected.includes(row.candidate_id) ? "selected" : ""}>
            <input type="checkbox" checked={selected.includes(row.candidate_id)} onChange={() => toggle(row.candidate_id)} />
            <span>{row.candidate_id}</span>
            <small>{row.lifecycle_status}</small>
          </label>
        ))}
      </div>
      <div className="tablePanel">
        <table>
          <thead>
            <tr>
              <th>Candidate</th>
              <HelpHeader label="PF" metric={metrics.profit_factor} />
              <HelpHeader label="Stability" metric={metrics.stability_score} />
              <HelpHeader label="Trades" metric={metrics.trade_count} />
              <HelpHeader label="Drawdown" metric={metrics.drawdown} />
              <HelpHeader label="Score" metric={metrics.research_score} />
              <th>Assets</th>
              <th>Timeframes</th>
              <th>Validation</th>
            </tr>
          </thead>
          <tbody>
            {selectedRows.map((row) => (
              <tr key={row.candidate_id}>
                <td>{row.candidate_id}</td>
                <td>{number(row.profit_factor)}</td>
                <td>{number(row.stability)}</td>
                <td>{row.trade_count}</td>
                <td>{percent(row.drawdown)}</td>
                <td>{number(row.research_score)}</td>
                <td>{row.assets.join(", ") || "N/A"}</td>
                <td>{row.timeframes.join(", ") || "N/A"}</td>
                <td>{row.validation_status}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function HelpHeader({ label, metric }: { label: string; metric?: MetricDefinition }) {
  const title = metric ? `${metric.measures}\nWhy it matters: ${metric.why_it_matters}\nCalculation: ${metric.calculation}` : undefined;
  return (
    <th title={title}>
      <span className="metricHelp">{label}</span>
    </th>
  );
}
