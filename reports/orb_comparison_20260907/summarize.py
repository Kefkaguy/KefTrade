"""Reconcile saved ledgers and generate an inspectable result report."""
from pathlib import Path
import json

import numpy as np
import pandas as pd

HERE=Path(__file__).resolve().parent


def main():
    results=json.loads((HERE/'results.json').read_text())
    quality=json.loads((HERE/'quality.json').read_text())
    manifest=json.loads((HERE/'manifest.json').read_text())
    checked=[]
    for r in results:
        key=f"{r['capital']}_{r['leverage']}x_{r['adverse_bps_per_side']}bps_{r['direction']}_{r['variant']}"
        trades=pd.read_csv(HERE/f'trades_{key}.csv')
        daily=pd.read_csv(HERE/f'daily_{key}.csv')
        assert abs(trades.net_pnl.sum()-r['net_profit'])<1e-6
        assert abs(daily.net_pnl.sum()-r['net_profit'])<1e-6
        assert abs(daily.equity.iloc[-1]-r['ending_equity'])<1e-6
        assert len(trades)==r['trades']
        notionals=(trades.shares*trades.entry_price).groupby(trades.date).sum()
        caps=daily.set_index('date').start_equity*r['leverage']
        assert (notionals <= caps.reindex(notionals.index)+1e-6).all()
        assert (trades.entry_bar>=1).all()
        assert (trades.exit_bar>=trades.entry_bar).all()
        checked.append(key)
    text=['# Opening-range breakout pilot: actual results', '',
          f"Run completed {manifest['created_at']}. Historical simulation only; no orders submitted.", '',
          f"Universe: 50 fixed current stock names. Dates: {manifest['first_session']}–{manifest['last_session']}. "
          f"Common complete sessions: {quality['common_complete_test_sessions']} of {quality['expected_test_sessions']}; "
          f"excluded dates: {len(quality['excluded_test_dates'])}.", '',
          'This does not reproduce the original paper’s full historical universe or period. Current-name selection/survivorship bias remains. Both arms use identical data and simulation mechanics. Full assumptions were recorded in [PROTOCOL.md](PROTOCOL.md) before portfolio results were examined.', '',
          '## Primary cost scenario', '',
          'Adverse price movement of 2.5 bps per side plus modeled commissions. These costs are assumptions, not measured quotes/fills. Long/short assumes borrow availability and omits borrow/locate costs.', '',
          '| Capital | Entry cap | Direction | Strategy | Net profit | Annualized | 5-min drawdown | Trades | PF | Average month | Worst month | Months ≥ $1k |',
          '|---:|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for r in results:
        if r['adverse_bps_per_side']!=2.5:
            continue
        text.append(f"| ${r['capital']:,} | {r['leverage']}x | {r['direction']} | {r['variant']} | ${r['net_profit']:,.2f} | {r['annualized_pct']:.2f}% | {r['max_5min_close_drawdown_pct']:.2f}% | {r['trades']:,} | {r['profit_factor']:.3f} | ${r['average_month']:,.2f} | ${r['worst_month']:,.2f} | {r['months_at_least_1000']}/{r['months']} |")
    text+=['','## Cost sensitivity: $20,000, 1x, hypothetical long/short','','| Adverse bps per side | Strategy | Net profit | Annualized | Drawdown |','|---:|---|---:|---:|---:|']
    for r in results:
        if r['capital']==20000 and r['leverage']==1 and r['direction']=='both_hypothetical_borrow':
            text.append(f"| {r['adverse_bps_per_side']} | {r['variant']} | ${r['net_profit']:,.2f} | {r['annualized_pct']:.2f}% | {r['max_5min_close_drawdown_pct']:.2f}% |")
    text+=['','## Paired selection difference','','Circular 20-session block bootstrap, 2,000 draws, fixed seed 20260907. Difference is filtered minus base in mean daily account-return basis points, preserving same-day dependence between arms. This is exploratory uncertainty on this selected sample, not a multiple-testing-adjusted edge certificate.']
    estimates=[]
    for direction in ('both_hypothetical_borrow','long_only'):
        frames=[]
        for variant in ('base','top20_rvol'):
            d=pd.read_csv(HERE/f'daily_20000_1x_2.5bps_{direction}_{variant}.csv')
            frames.append(d.set_index('date').net_pnl/d.set_index('date').start_equity)
        paired=pd.concat(frames,axis=1)
        assert not paired.isna().any().any()
        delta=(paired.iloc[:,1]-paired.iloc[:,0]).to_numpy()*10000
        rng=np.random.default_rng(20260907)
        starts=rng.integers(0,len(delta),size=(2000,int(np.ceil(len(delta)/20))))
        indices=((starts[:,:,None]+np.arange(20))%len(delta)).reshape(2000,-1)[:,:len(delta)]
        lo,hi=np.quantile(delta[indices].mean(axis=1),[.025,.975])
        est=dict(direction=direction,mean_daily_delta_bps=float(delta.mean()),lower95=float(lo),upper95=float(hi))
        estimates.append(est)
        text+=['',f"- {direction}: {delta.mean():.3f} bps/day; approximate 95% interval [{lo:.3f}, {hi:.3f}]."]
    text+=['','## Interpretation limits','',
           '- A positive result is evidence only for this limited historical pilot. It cannot establish the paper’s reported 41.6% return, an untouched holdout result, or live profitability.',
           '- Five-minute bars cannot establish event ordering. Entry-bar stops are deliberately adverse assumptions. Drawdown uses five-minute closes, not the worst tick. End-of-session close is an execution proxy.',
           '- Capital is allocated equally across selected sleeves, not risked at 1% of the entire account on every trade. Unused sleeves remain cash. 4x scenarios do not model broker margin calls or financing/borrow constraints.',
           '- Monthly profits are compounded, not withdrawn. Counts above $1k/$2k describe simulated months; they are not dependable income or a withdrawal analysis.',
           '- A 30% drawdown stop was not imposed. Historical drawdown cannot guarantee future loss stays below 30%.',
           '- Original-universe membership, delistings, historical borrow, actual spread/impact, exchange/regulatory charges, and post-download revisions are unresolved.',
           '', '## Verification and artifacts','',
           f"All {len(checked)} scenario ledgers reconcile trade P&L to daily equity and summary profit. Entry-notional caps and entry/exit sequencing checked on every saved trade.", '',
           '- [All scenario metrics](summary.csv)',
           '- [Detailed results including monthly P&L](results.json)',
           '- [Data completeness and SHA-256 hashes](quality.json)',
           '- [Runner](run.py), [execution tests](test_run.py), [frozen protocol](PROTOCOL.md)',
           '- Per-scenario trade and daily-equity CSV files are retained beside this report. Downloaded market data are cached in `data/`.',
           '', '## Sources','',
           '- [Original strategy paper](https://concretumgroup.com/wp-content/uploads/2026/02/A-Profitable-Day-Trading-Strategy-For-The-U.S.-Equity-Market.pdf)',
           '- [Alpaca historical bars API](https://docs.alpaca.markets/us/reference/stockbars)', '']
    (HERE/'RESULTS.md').write_text('\n'.join(text),encoding='utf-8')
    (HERE/'verification.json').write_text(json.dumps(dict(reconciled_scenarios=len(checked),paired_bootstrap=estimates),indent=2),encoding='utf-8')
    print(json.dumps(dict(reconciled_scenarios=len(checked),paired_bootstrap=estimates),indent=2))


if __name__=='__main__':
    main()
