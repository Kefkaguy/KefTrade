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

export function EmptyState({ title, body }: { title: string; body: string }) {
  return (
    <div className="emptyState">
      <strong>{title}</strong>
      <p>{body}</p>
    </div>
  );
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
