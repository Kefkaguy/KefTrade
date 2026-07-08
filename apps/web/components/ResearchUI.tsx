import Link from "next/link";

export type MetricCardProps = {
  label: string;
  value: string | number;
  detail?: string;
  tone?: "neutral" | "success" | "warning" | "error";
};

export function MetricCard({ label, value, detail, tone = "neutral" }: MetricCardProps) {
  return (
    <article className={`metricCard ${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      {detail ? <small>{detail}</small> : null}
    </article>
  );
}

export function PageTitle({ title, description, actions }: { title: string; description: string; actions?: React.ReactNode }) {
  return (
    <header className="pageHeader">
      <div>
        <h1>{title}</h1>
        <p className="muted">{description}</p>
      </div>
      {actions ? <div className="toolbar">{actions}</div> : null}
    </header>
  );
}

export function Card({ title, eyebrow, children, action }: { title: string; eyebrow?: string; children: React.ReactNode; action?: React.ReactNode }) {
  return (
    <section className="panel">
      <div className="panelHeader">
        <div>
          {eyebrow ? <span className="sectionLabel">{eyebrow}</span> : null}
          <h2>{title}</h2>
        </div>
        {action}
      </div>
      {children}
    </section>
  );
}

export function LineChart({ values, label }: { values: number[]; label: string }) {
  if (!values.length) {
    return <EmptyState title="No chart data yet" body="Run a sync, experiment, or validation to populate this visualization." />;
  }
  const width = 720;
  const height = 220;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const scaleX = (index: number) => (index / Math.max(1, values.length - 1)) * (width - 36) + 18;
  const scaleY = (value: number) => 18 + ((max - value) / Math.max(1, max - min)) * (height - 36);
  const points = values.map((value, index) => `${scaleX(index)},${scaleY(value)}`).join(" ");
  const last = values.at(-1) ?? 0;

  return (
    <svg className="chart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={label}>
      <path d="M18 18H702V202H18Z" fill="none" stroke="var(--grid-line)" />
      {[0, 1, 2, 3].map((row) => (
        <line key={row} x1="18" x2="702" y1={18 + row * 46} y2={18 + row * 46} stroke="var(--grid-line)" />
      ))}
      <polyline points={points} fill="none" stroke="var(--accent)" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx={scaleX(values.length - 1)} cy={scaleY(last)} r="5" fill="var(--accent)" />
    </svg>
  );
}

export function DrawdownChart({ values, label }: { values: number[]; label: string }) {
  const normalized = values.map((value) => -Math.abs(Number(value) || 0));
  return <LineChart values={normalized.length ? normalized : [0]} label={label} />;
}

export function TradeDistribution({ values, label }: { values: number[]; label: string }) {
  const finite = values.map((value) => Number(value)).filter((value) => Number.isFinite(value));
  if (!finite.length) {
    return <EmptyState title="No trade distribution yet" body="Trade-level PnL data will appear after a backtest or validation run stores trades." />;
  }
  const buckets = bucketValues(finite, 8);
  const max = Math.max(...buckets.map((row) => row.count), 1);
  return (
    <svg className="chart" viewBox="0 0 720 220" role="img" aria-label={label}>
      {buckets.map((bucket, index) => {
        const width = 620 / buckets.length;
        const height = (bucket.count / max) * 160;
        const x = 50 + index * width;
        const y = 190 - height;
        return (
          <g key={bucket.label}>
            <rect x={x + 4} y={y} width={Math.max(8, width - 8)} height={height} rx="5" fill="var(--accent)" />
            <text x={x + width / 2} y="208" textAnchor="middle" fill="var(--muted)" fontSize="10">{index + 1}</text>
          </g>
        );
      })}
      <text x="50" y="24" fill="var(--muted)" fontSize="12">Losses to gains</text>
    </svg>
  );
}

export function Heatmap({ rows, label }: { rows: Array<{ x: string; y: string; value: number }>; label: string }) {
  if (!rows.length) {
    return <EmptyState title="No heatmap data yet" body="Cross-asset research results will populate this view after candidates are evaluated." />;
  }
  const xs = Array.from(new Set(rows.map((row) => row.x)));
  const ys = Array.from(new Set(rows.map((row) => row.y)));
  const max = Math.max(...rows.map((row) => Math.abs(row.value)), 1);
  return (
    <div className="heatmap" style={{ gridTemplateColumns: `minmax(90px, 0.7fr) repeat(${xs.length}, minmax(76px, 1fr))` }} role="img" aria-label={label}>
      <span />
      {xs.map((x) => <strong key={x}>{x}</strong>)}
      {ys.map((y) => (
        <FragmentRow key={y} y={y} xs={xs} rows={rows} max={max} />
      ))}
    </div>
  );
}

function FragmentRow({ y, xs, rows, max }: { y: string; xs: string[]; rows: Array<{ x: string; y: string; value: number }>; max: number }) {
  return (
    <>
      <strong>{y}</strong>
      {xs.map((x) => {
        const value = rows.find((row) => row.x === x && row.y === y)?.value ?? 0;
        const intensity = Math.min(0.82, Math.abs(value) / max);
        const positive = value >= 0;
        return (
          <span
            key={`${x}-${y}`}
            style={{ background: `color-mix(in srgb, ${positive ? "var(--success)" : "var(--error)"} ${Math.round(intensity * 100)}%, var(--panel-soft))` }}
            title={`${x} ${y}: ${value.toFixed(2)}`}
          >
            {value.toFixed(1)}
          </span>
        );
      })}
    </>
  );
}

function bucketValues(values: number[], bucketCount: number) {
  const min = Math.min(...values);
  const max = Math.max(...values);
  const width = Math.max((max - min) / bucketCount, 1);
  return Array.from({ length: bucketCount }, (_item, index) => {
    const start = min + index * width;
    const end = index === bucketCount - 1 ? max : start + width;
    return {
      label: `${start.toFixed(2)}-${end.toFixed(2)}`,
      count: values.filter((value) => (index === bucketCount - 1 ? value >= start && value <= end : value >= start && value < end)).length
    };
  });
}

export function BarList({ rows }: { rows: Array<{ label: string; value: number; meta?: string }> }) {
  if (!rows.length) {
    return <EmptyState title="No distribution yet" body="Research records will appear here after experiments or validation runs exist." />;
  }
  const max = Math.max(...rows.map((row) => row.value), 1);
  return (
    <div className="barList">
      {rows.map((row) => (
        <div className="barRow" key={row.label}>
          <span>{row.label}</span>
          <div className="barTrack">
            <div className="barFill" style={{ width: `${Math.max(6, (row.value / max) * 100)}%` }} />
          </div>
          <strong>{row.meta ?? row.value}</strong>
        </div>
      ))}
    </div>
  );
}

export function Timeline({ items }: { items: Array<{ date: string; title: string; body: string; status?: string }> }) {
  return (
    <div className="timeline">
      {items.map((item, index) => (
        <article key={`${item.date}-${item.title}-${index}`}>
          <time>{item.date}</time>
          <div>
            <h3>{item.title}</h3>
            <p>{item.body}</p>
            {item.status ? <span className="status">{item.status}</span> : null}
          </div>
        </article>
      ))}
    </div>
  );
}

export function DataTable({ columns, rows }: { columns: string[]; rows: Array<Array<React.ReactNode>> }) {
  return (
    <div className="tablePanel">
      <table>
        <thead>
          <tr>{columns.map((column) => <th key={column}>{column}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={index}>
              {row.map((cell, cellIndex) => <td key={cellIndex}>{cell}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function EmptyState({ title, body, action }: { title: string; body: string; action?: React.ReactNode }) {
  return (
    <div className="emptyState">
      <strong>{title}</strong>
      <p>{body}</p>
      {action ? <div className="toolbar">{action}</div> : null}
    </div>
  );
}

export function ActionNote({ title, body }: { title: string; body: string }) {
  return (
    <div className="actionNote">
      <strong>{title}</strong>
      <p>{body}</p>
    </div>
  );
}

export function Toast({ tone, message }: { tone: "success" | "error" | "info"; message: string }) {
  if (!message) return null;
  return <div className={`toast ${tone}`} role="status">{message}</div>;
}

export function AssetLink({ symbol, label }: { symbol: string; label?: string }) {
  return <Link className="assetLink" href={`/assets/${symbol}`}>{label ?? symbol}</Link>;
}

export function EvidenceBadges({ refs }: { refs: string[] }) {
  if (!refs.length) {
    return <span className="status">No refs</span>;
  }
  return (
    <div className="evidenceList">
      {refs.map((ref) => (
        <Link key={ref} href={`/copilot?evidence=${encodeURIComponent(ref)}`}>
          {ref}
        </Link>
      ))}
    </div>
  );
}
