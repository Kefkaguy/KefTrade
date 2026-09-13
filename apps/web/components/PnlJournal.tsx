"use client";

import { useEffect, useMemo, useRef, useState } from 'react';
import Link from 'next/link';
import { CalendarDays, ChevronLeft, ChevronRight, RefreshCw, TrendingUp, Wallet, X, ArrowUpRight, Download } from 'lucide-react';

type Trade = { id: string; symbol: string; strategy: string; side: string; quantity: number; entry_price: number; exit_price: number; opened_at: string; closed_at: string; day: string; gross_pnl: number };
type Snapshot = {
  month: string; generated_at: string; coverage_start: string | null;
  account: { equity?: number; cash?: number; buying_power?: number; account_number_masked: string; last_successful_sync_at: string | null } | null;
  reconciliation: { status: string; completed_at: string } | null;
  trades: Trade[];
  positions: { symbol: string; strategy: string | null; quantity: number; average_entry_price: number; market_value: number; unrealized_pl: number }[];
  strategies: { name: string; symbol: string | null; state: string }[];
};
const money = (n: unknown) => n == null ? '—' : new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 2 }).format(Number(n));
const dateET = (d: Date) => new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit' }).format(d);
const stamp = (s?: string | null) => s ? new Date(s).toLocaleString('en-US', { timeZone: 'America/New_York', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) + ' ET' : 'Not synced';
const sum = (rows: Trade[]) => rows.reduce((n, t) => n + Number(t.gross_pnl), 0);
const tone = (n: number) => n < 0 ? 'text-copper' : 'text-moss';

export function PnlJournal() {
  const [month, setMonth] = useState('');
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(true);
  const [revision, setRevision] = useState(0);
  const [strategy, setStrategy] = useState('ALL');
  const [day, setDay] = useState<string | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const trigger = useRef<HTMLButtonElement | null>(null);
  useEffect(() => setMonth(dateET(new Date()).slice(0, 7)), []);
  useEffect(() => {
    if (!month) return;
    const controller = new AbortController();
    let pending = false;
    async function load() {
      if (pending) return;
      pending = true; setBusy(true);
      try {
        const response = await fetch(`/api/pnl-journal?month=${month}`, { cache: 'no-store', signal: controller.signal });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error ?? 'Journal unavailable');
        setSnapshot(data); setError('');
      } catch (e) {
        if (!controller.signal.aborted) setError(e instanceof Error ? e.message : 'Journal unavailable');
      } finally { pending = false; if (!controller.signal.aborted) setBusy(false); }
    }
    void load();
    const interval = setInterval(() => void load(), 30000);
    return () => { controller.abort(); clearInterval(interval); };
  }, [month, revision]);
  useEffect(() => {
    if (day) dialog.current?.showModal();
    else dialog.current?.close();
  }, [day]);
  const current = snapshot?.month === month ? snapshot : null;
  const trades = useMemo(() => (current?.trades ?? []).filter(t => strategy === 'ALL' || t.strategy === strategy), [current, strategy]);
  const groups = useMemo(() => {
    const result: Record<string, Trade[]> = {};
    for (const t of trades) (result[t.day] ??= []).push(t);
    return result;
  }, [trades]);
  const strategies = [...new Set([...(current?.strategies ?? []).map(s => s.name), ...(current?.trades ?? []).map(t => t.strategy)])].sort();
  const [year, monthNumber] = (month || '2000-01').split('-').map(Number);
  const first = new Date(Date.UTC(year, monthNumber - 1, 1));
  const offset = (first.getUTCDay() + 6) % 7;
  const days = new Date(Date.UTC(year, monthNumber, 0)).getUTCDate();
  const label = first.toLocaleDateString('en-US', { month: 'long', year: 'numeric', timeZone: 'UTC' });
  const pnl = sum(trades);
  const wins = trades.filter(t => Number(t.gross_pnl) > 0);
  const losses = trades.filter(t => Number(t.gross_pnl) < 0);
  const profitFactor = losses.length ? (sum(wins) / Math.abs(sum(losses))).toFixed(2) : wins.length ? '∞' : '—';
  const sessions = Object.values(groups).map(sum);
  const today = dateET(new Date());
  const cumulative = Array.from({ length: days }, (_, i) => sum(trades.filter(t => Number(t.day.slice(-2)) <= i + 1)));
  const low = Math.min(0, ...cumulative), high = Math.max(0, ...cumulative);
  const points = cumulative.map((v, i) => `${i / Math.max(1, days - 1) * 300},${100 - (v - low) / (high - low || 1) * 85}`).join(' ');
  function move(delta: number) {
    const d = new Date(Date.UTC(year, monthNumber - 1 + delta, 1));
    setMonth(d.toISOString().slice(0, 7)); setDay(null);
  }
  function exportCsv() {
    const escape = (s: unknown) => '"' + String(s ?? '').replace(/^[=+@-]/, "'$&").replaceAll('"', '""') + '"';
    const data = [['Date (ET)', 'Strategy', 'Symbol', 'Side', 'Quantity', 'Entry', 'Exit', 'Gross FIFO P&L'], ...trades.map(t => [t.day, t.strategy, t.symbol, t.side, t.quantity, t.entry_price, t.exit_price, t.gross_pnl])];
    const url = URL.createObjectURL(new Blob([data.map(r => r.map(escape).join(',')).join('\r\n')], { type: 'text/csv;charset=utf-8' }));
    const a = document.createElement('a'); a.href = url; a.download = `keftrade-${month}.csv`; a.click(); URL.revokeObjectURL(url);
  }
  return <div id="pnl-journal">
    <header className="sticky top-0 z-20 bg-paper/95 backdrop-blur-md border-b border-olive/10">
      <div className="max-w-[1440px] mx-auto px-4 lg:px-10 py-5 flex items-center justify-between gap-4 flex-wrap">
        <a href="#overview" className="flex items-center gap-3"><span className="bg-cream rounded-xl p-2 text-olive"><TrendingUp size={24}/></span><span><strong className="block text-ink text-lg">KefTrade</strong><small className="text-muted tracking-widest uppercase">P&L Journal</small></span></a>
        <nav aria-label="Journal navigation" className="flex items-center gap-2 text-sm flex-wrap">
          {['Overview', 'Calendar', 'Strategies', 'Trades'].map(n => <a key={n} href={`#${n.toLowerCase()}`} className="px-3 py-2 rounded-lg hover:bg-cream transition-colors">{n}</a>)}
          <Link href="/paper" className="px-3 py-2 rounded-lg hover:bg-cream">Paper labs <ArrowUpRight size={13} className="inline"/></Link>
        </nav>
        <span className="rounded-full bg-cream px-3 py-1.5 text-xs text-olive">Alpaca · Paper money</span>
      </div>
    </header>
    <main className="max-w-[1440px] mx-auto p-4 lg:p-10 space-y-6" id="overview">
      <section className="journal-panel flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-center gap-3"><Wallet size={23} className="text-olive"/><div><small className="block text-muted">Alpaca paper account</small><strong>{current?.account?.account_number_masked ?? 'Awaiting broker data'}</strong></div></div>
        <div className="flex items-center gap-2 bg-vanilla/60 rounded-lg p-1">
          <button aria-label="Previous month" onClick={() => move(-1)} disabled={!month} className="journal-icon"><ChevronLeft size={20}/></button>
          <CalendarDays size={18} className="text-olive"/><strong className="min-w-36 text-center">{month ? label : 'Loading…'}</strong>
          <button aria-label="Next month" onClick={() => move(1)} disabled={!month || month >= today.slice(0,7)} className="journal-icon"><ChevronRight size={20}/></button>
        </div>
        <label className="text-muted text-xs flex items-center gap-2">Strategy
          <select value={strategy} onChange={e => setStrategy(e.target.value)} className="max-w-56 bg-vanilla/60 rounded-lg p-2 text-ink"><option value="ALL">All strategies</option>{strategies.map(s => <option key={s}>{s}</option>)}</select>
        </label>
        <button onClick={() => setRevision(v => v + 1)} disabled={busy} className="flex items-center gap-2 text-sm text-olive"><RefreshCw size={16} className={busy ? 'animate-spin' : ''}/>{busy ? 'Refreshing' : 'Refresh'}</button>
      </section>
      <div className="flex flex-wrap justify-between gap-2 text-xs text-muted" aria-live="polite"><span>Broker sync: {stamp(current?.account?.last_successful_sync_at)} · Reconciliation: {current?.reconciliation?.status ?? 'unknown'}</span><span>30-second refresh · Sessions in New York time</span></div>
      {error && <div role="alert" className="journal-panel border border-copper text-copper">{error} {current ? 'Showing previously loaded data.' : ''}</div>}
      {!busy && current && !current.account && <div className="journal-panel">No synced Alpaca paper account is available yet.</div>}
      <section className="grid grid-cols-2 lg:grid-cols-6 gap-4">
        <Metric label="Account equity" value={money(current?.account?.equity)} detail={`Cash ${money(current?.account?.cash)}`} wide/>
        <Metric label="Month · gross FIFO" value={current?.account ? money(pnl) : '—'} detail={current?.account ? `${trades.length} matched closed lots` : 'Awaiting data'} color={tone(pnl)}/>
        <Metric label="Today · gross FIFO" value={current?.account && today.startsWith(month) ? money(sum(groups[today] ?? [])) : '—'} detail={today.startsWith(month) ? 'Closed lots only' : 'Outside selected month'}/>
        <Metric label="Win rate" value={trades.length ? `${(wins.length / trades.length * 100).toFixed(1)}%` : '—'} detail="Of matched closed lots"/>
        <Metric label="Profit factor" value={profitFactor} detail="Gross wins ÷ gross losses"/>
      </section>
      <section className="grid grid-cols-1 xl:grid-cols-[1fr_310px] gap-6" id="calendar">
        <div className="journal-panel">
          <div className="flex justify-between flex-wrap gap-3 mb-6"><div><h1 className="text-xl font-semibold text-ink">P&L Performance Matrix</h1><p className="text-xs text-muted mt-1">Select a session to inspect its broker fill matches</p></div><div className="flex gap-3 text-xs items-center text-muted"><span>● Profit</span><span className="text-copper">● Loss</span><span>○ No matches</span></div></div>
          <div className="overflow-x-auto"><div className="min-w-[560px]">
            <div className="grid grid-cols-7 gap-2 mb-2">{['Mon','Tue','Wed','Thu','Fri','Sat','Sun'].map(d => <span key={d} className="text-center text-xs text-muted uppercase py-2">{d}</span>)}</div>
            <div className="grid grid-cols-7 gap-2" aria-label={label}>
              {Array.from({length: Math.ceil((offset + days) / 7) * 7}, (_, i) => {
                const n = i - offset + 1;
                if (n < 1 || n > days) return <div key={i} className="bg-vanilla/25 rounded-lg"/>;
                const key = `${month}-${String(n).padStart(2,'0')}`, rows = groups[key] ?? [], value = sum(rows);
                return <button key={i} disabled={!current?.account} onClick={e => { trigger.current = e.currentTarget; setDay(key); }} aria-label={current?.account ? `${key}, ${rows.length} matched lots, ${money(value)}` : `${key}, data unavailable`} className={`journal-day ${rows.length ? value < 0 ? 'bg-copper/20' : 'bg-cream/80' : 'bg-vanilla/40'} ${today === key ? 'ring-2 ring-olive' : ''}`}>
                  <span className="flex justify-between w-full text-xs"><strong>{n}</strong><span className="text-muted">{rows.length ? `${rows.length} lots` : ''}</span></span>
                  <strong className={`text-sm lg:text-base ${tone(value)}`}>{rows.length ? money(value) : '—'}</strong>
                  <span className="text-[10px] text-muted truncate w-full text-left">{!current?.account ? 'Data unavailable' : rows.length ? rows[0].strategy : key > today ? 'Upcoming' : 'No closed matches'}</span>
                </button>;
              })}
            </div>
          </div></div>
          <p className="text-xs text-muted mt-4">Gross FIFO estimates from available activity fills. No matched lots does not prove no trading activity.</p>
        </div>
        <aside className="journal-panel space-y-6">
          <div><span className="text-xs uppercase tracking-widest text-muted">Monthly summary</span><h2 className="text-lg mt-2">{label}</h2></div>
          <div><small className="text-muted">Cumulative gross P&L</small><strong className={`block text-3xl mt-2 ${tone(pnl)}`}>{current?.account ? money(pnl) : '—'}</strong></div>
          {trades.length ? <svg viewBox="0 0 300 110" role="img" aria-label="Cumulative gross closed-lot P&L"><polyline points={points} fill="none" stroke="#546526" strokeWidth="2.5" vectorEffect="non-scaling-stroke"/></svg> : <div className="rounded-xl bg-vanilla/40 p-6 text-sm text-muted text-center">No matched closed lots for this selection.</div>}
          <Summary label="Profitable sessions" value={current?.account ? String(sessions.filter(n => n > 0).length) : '—'}/>
          <Summary label="Losing sessions" value={current?.account ? String(sessions.filter(n => n < 0).length) : '—'}/>
          <Summary label="Best session" value={sessions.length ? money(Math.max(...sessions)) : '—'}/>
          <Summary label="Worst session" value={sessions.length ? money(Math.min(...sessions)) : '—'}/>
          <Summary label="Average closed lot" value={trades.length ? money(pnl / trades.length) : '—'}/>
          <button className="bg-cream rounded-lg p-3 w-full flex items-center justify-center gap-2 text-sm text-olive" disabled={!trades.length} onClick={exportCsv}><Download size={16}/>Export monthly ledger</button>
        </aside>
      </section>
      <section id="strategies" className="journal-panel"><h2 className="text-xl mb-2">Strategy breakdown</h2><p className="text-xs text-muted mb-5">Current configuration and selected-month FIFO attribution. Recorded ownership is not proof a bot is running.</p><div className="overflow-x-auto"><table className="journal-table"><thead><tr><th>Strategy</th><th>Symbols</th><th>Gross P&L</th><th>Closed lots</th><th>Win rate</th><th>Recorded state</th></tr></thead><tbody>
        {strategies.filter(s => strategy === 'ALL' || strategy === s).map(s => {
          const rows = trades.filter(t => t.strategy === s), configs = current?.strategies.filter(t => t.name === s) ?? [];
          return <tr key={s}><td className="font-semibold">{s}</td><td>{[...new Set(configs.map(c => c.symbol))].join(', ') || '—'}</td><td className={tone(sum(rows))}>{rows.length ? money(sum(rows)) : '—'}</td><td>{rows.length}</td><td>{rows.length ? `${(rows.filter(t => Number(t.gross_pnl)>0).length/rows.length*100).toFixed(1)}%` : '—'}</td><td>{[...new Set(configs.map(c => c.state))].join(', ') || 'Fill history only'}</td></tr>;
        })}
      </tbody></table>{!strategies.length && <p className="text-muted py-6">No strategy records loaded.</p>}</div></section>
      <section id="trades" className="journal-panel"><h2 className="text-xl mb-2">Open Alpaca positions</h2><p className="text-xs text-muted mb-5">Whole account · current broker snapshot, independent of calendar filters</p><div className="overflow-x-auto"><table className="journal-table"><thead><tr><th>Symbol</th><th>Ownership record</th><th>Quantity</th><th>Average entry</th><th>Market value</th><th>Unrealized P&L</th></tr></thead><tbody>{current?.positions.map(p => <tr key={p.symbol}><td className="font-semibold">{p.symbol}</td><td>{p.strategy ?? 'Unattributed'}</td><td>{Number(p.quantity)}</td><td>{money(p.average_entry_price)}</td><td>{money(p.market_value)}</td><td className={tone(Number(p.unrealized_pl))}>{money(p.unrealized_pl)}</td></tr>)}</tbody></table>{!current?.positions.length && <p className="text-muted py-6">{current?.account ? 'No open positions in the broker snapshot.' : 'Positions unavailable.'}</p>}</div></section>
      <div className="text-xs text-muted leading-relaxed">Source: Alpaca paper activity synced to PostgreSQL. Fill history starts {stamp(current?.coverage_start)}. FIFO estimates assume that history includes the opening inventory; missing fills, corporate actions, and shared-symbol strategies can affect attribution. Net P&L and account-return percentages are unavailable until fees and cash-flow reconciliation are supported.</div>
    </main>
    <footer className="bg-paper p-6 text-xs text-muted text-center">KefTrade · Systematic portfolio journal · Fake money, real execution records</footer>
    <dialog ref={dialog} className="journal-drawer" onCancel={() => setDay(null)} onClose={() => { setDay(null); trigger.current?.focus(); }} aria-labelledby="day-title">
      <div className="flex justify-between gap-4 items-center mb-6"><div><small className="uppercase tracking-widest text-muted">Session ledger · ET</small><h2 id="day-title" className="text-2xl mt-2">{day}</h2></div><button autoFocus aria-label="Close session detail" className="journal-icon" onClick={() => setDay(null)}><X size={22}/></button></div>
      <div className="rounded-xl bg-vanilla/60 p-5 mb-6"><small>Gross FIFO estimate</small><strong className={`block text-3xl mt-2 ${tone(sum(groups[day ?? ''] ?? []))}`}>{money(sum(groups[day ?? ''] ?? []))}</strong><p className="text-xs text-muted mt-2">Net P&L unavailable · fees not recorded</p></div>
      <div className="overflow-x-auto"><table className="journal-table"><thead><tr><th>Strategy / symbol</th><th>Side / qty</th><th>Entry</th><th>Exit</th><th>Gross</th></tr></thead><tbody>{(groups[day ?? ''] ?? []).map(t => <tr key={t.id}><td><strong>{t.symbol}</strong><small className="block">{t.strategy}</small></td><td>{t.side} / {Number(t.quantity)}</td><td>{money(t.entry_price)}<small className="block">{stamp(t.opened_at)}</small></td><td>{money(t.exit_price)}<small className="block">{stamp(t.closed_at)}</small></td><td className={tone(Number(t.gross_pnl))}>{money(t.gross_pnl)}</td></tr>)}</tbody></table></div>
      {!groups[day ?? '']?.length && <p className="text-muted mt-8">No matched closed lots in this session for the selected strategy.</p>}
    </dialog>
  </div>;
}

function Metric({ label, value, detail, wide, color = 'text-ink' }: { label: string; value: string; detail: string; wide?: boolean; color?: string }) {
  return <div className={`journal-panel ${wide ? 'col-span-2' : ''}`}><span className="text-xs text-muted">{label}</span><strong className={`block text-2xl my-3 tracking-tight ${color}`}>{value}</strong><small className="text-muted">{detail}</small></div>;
}
function Summary({label, value}: {label: string; value: string}) { return <div className="flex justify-between gap-3 text-sm"><span className="text-muted">{label}</span><strong>{value}</strong></div>; }
