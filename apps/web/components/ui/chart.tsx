"use client";

import * as React from "react";
import { ResponsiveContainer, Tooltip as RechartsTooltip } from "recharts";

export type ChartConfig = Record<string, { label?: React.ReactNode; color?: string }>;

const ChartContext = React.createContext<ChartConfig>({});

export function ChartContainer({
  config,
  className,
  children,
}: React.HTMLAttributes<HTMLDivElement> & { config: ChartConfig; children: React.ReactNode }) {
  const variables = Object.entries(config).reduce<React.CSSProperties>((style, [key, item]) => {
    if (item.color) (style as Record<string, string>)[`--color-${key}`] = item.color;
    return style;
  }, {});

  return (
    <ChartContext.Provider value={config}>
      <div className={["chartContainer", className].filter(Boolean).join(" ")} style={variables}>
        <ResponsiveContainer>{children}</ResponsiveContainer>
      </div>
    </ChartContext.Provider>
  );
}

export const ChartTooltip = RechartsTooltip;

export function ChartTooltipContent({
  active,
  payload,
  label,
  valueFormatter,
}: {
  active?: boolean;
  payload?: Array<{ dataKey?: string | number; name?: string; value?: unknown; color?: string }>;
  label?: React.ReactNode;
  valueFormatter?: (value: number, dataKey: string) => string;
}) {
  const config = React.useContext(ChartContext);
  if (!active || !payload?.length) return null;

  return (
    <div className="chartTooltipContent">
      <strong>{label}</strong>
      {payload.map((item) => {
        const key = String(item.dataKey ?? item.name ?? "value");
        const value = Number(item.value);
        return (
          <div key={key}>
            <i style={{ background: item.color ?? config[key]?.color }} />
            <span>{config[key]?.label ?? item.name ?? key}</span>
            <b>{valueFormatter && Number.isFinite(value) ? valueFormatter(value, key) : String(item.value ?? "—")}</b>
          </div>
        );
      })}
    </div>
  );
}
