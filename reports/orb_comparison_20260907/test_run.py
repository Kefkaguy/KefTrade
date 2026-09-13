"""Execution invariants, independent of vendor data or network."""
import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

spec = importlib.util.spec_from_file_location("orb_comparison", Path(__file__).with_name("run.py"))
orb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(orb)


def test_no_entry_during_opening_bar_or_on_touch_only():
    a=np.array([[100,105,95,101,10000],[100,102,99,101,10000]],float)
    assert orb.simulate_trade(a,1,102,1,1000,1,0) is None


def test_gap_entry_uses_open_and_caps_notional():
    a=np.array([[100,101,99,100.5,10000],[103,104,103,104,10000]],float)
    t=orb.simulate_trade(a,1,101,1,1000,1,0)
    assert t['entry_price']==103
    assert t['shares']==9
    assert t['net_pnl']==pytest.approx(8.3)
    assert t['path'][-1]==t['net_pnl']


def test_gap_stop_exits_at_adverse_open():
    a=np.array([[100,101,99,100.5,10000],[101,102,101,101.5,10000],[98,99,97,98.5,10000]],float)
    t=orb.simulate_trade(a,1,101,1,1000,1,0)
    assert t['exit_price']==98
    assert t['net_pnl']==pytest.approx(-27.7)


def test_same_entry_bar_ambiguity_uses_stop_but_not_pre_entry_open():
    a=np.array([[100,101,99,100.5,10000],[98,103,97,102,10000]],float)
    t=orb.simulate_trade(a,1,101,1,1000,1,0)
    assert t['exit_price']==100
    assert t['exit_reason']=='ambiguous_entry_bar_stop'


def test_short_gap_stop_and_costs():
    a=np.array([[100,101,99,99.5,10000],[99,99,97,98,10000],[103,104,102,103,10000]],float)
    t=orb.simulate_trade(a,-1,99,1,1000,1,0)
    assert t['entry_price']==99
    assert t['exit_price']==103
    assert t['net_pnl']==pytest.approx(-40.7)


def test_optimistic_diagnostic_never_ignores_open_entry_stop():
    a=np.array([[100,101,99,100.5,10000],[103,105,100,104,10000]],float)
    t=orb.simulate_trade(a,1,101,1,1000,1,0,optimistic_intrabar=True)
    assert t['exit_price']==102


def test_one_minute_execution_waits_for_full_five_minute_range(monkeypatch):
    monkeypatch.setattr(orb,'BAR_MINUTES',1)
    a=np.array([[100,103,99,101,10000]]*5+[[100,101,99,100,10000]],float)
    assert orb.simulate_trade(a,1,102,1,1000,1,0) is None


def test_sparse_minutes_require_matching_aggregate_and_cannot_trade():
    ts=pd.date_range('2026-06-01 13:30',periods=5,freq='min',tz='UTC')
    g=pd.DataFrame(dict(timestamp=ts[[0,2,3,4]],open=[10,10,10,10],high=[11]*4,low=[9]*4,close=[10]*4,volume=[100]*4))
    ref=pd.DataFrame(dict(open=[10],high=[11],low=[9],close=[10],volume=[400]),index=ts[:1])
    dense,n=orb.reconcile_minutes(g,ref,ts)
    assert n==1
    assert dense.volume.iloc[1]==0
    assert dense.close.iloc[1]==10
    ref.loc[ts[0],'volume']=401
    with pytest.raises(ValueError):
        orb.reconcile_minutes(g,ref,ts)


def test_features_use_prior_daily_data_and_split_units(monkeypatch):
    dates=pd.bdate_range('2025-01-02',periods=18)
    sessions={d.strftime('%Y-%m-%d'):(d.tz_localize('UTC')+pd.Timedelta(hours=14,minutes=30),d.tz_localize('UTC')+pd.Timedelta(hours=14,minutes=40)) for d in dates}
    daily=[]
    split=[]
    intraday=[]
    for i,(date,(op,cl)) in enumerate(sessions.items()):
        price=100 if i<16 else 50
        volume=2_000_000 if i<16 else 4_000_000
        daily.append(dict(timestamp=pd.Timestamp(date,tz='America/New_York'),open=price,high=price+price*.02,low=price-price*.02,close=price,volume=volume))
        split.append(dict(timestamp=pd.Timestamp(date,tz='America/New_York'),open=50,high=51,low=49,close=50,volume=4_000_000))
        for t in pd.date_range(op,cl,freq='5min',inclusive='left'):
            intraday.append(dict(timestamp=t,open=price,high=price+1,low=price-1,close=price+.5,volume=volume/10))
    raw=pd.DataFrame(daily)
    adjusted=pd.DataFrame(split)
    intra=pd.DataFrame(intraday)
    monkeypatch.setattr(orb,'fetch',lambda s,tf,adj: intra.copy() if tf=='5Min' else (raw.copy() if adj=='raw' else adjusted.copy()))
    before,_,_=orb.prepare_symbol('TEST',sessions)
    on_split=next(x for x in before if x['date']==dates[16].strftime('%Y-%m-%d'))
    assert on_split['atr']==pytest.approx(2)
    assert on_split['rvol']==pytest.approx(1)
    # Mutating current/future high, low and volume cannot affect today's features.
    raw.loc[16:,['high','low','volume']]=[500,1,100]
    after,_,_=orb.prepare_symbol('TEST',sessions)
    same=next(x for x in after if x['date']==on_split['date'])
    assert same==on_split


def test_top20_ranking_and_shared_capital_reconcile():
    dates=['2025-01-02','2025-01-03']
    symbols=[f'S{i:02d}' for i in range(25)]
    sessions={d:(pd.Timestamp(d,tz='UTC'),pd.Timestamp(d,tz='UTC')+pd.Timedelta(minutes=10)) for d in dates}
    a=np.array([[100,101,99,100.5,10000],[101,104,101,103,10000]],float)
    groups={s:{d:a for d in dates} for s in symbols}
    candidates={d:[dict(symbol=s,date=d,side=1,atr=10,rvol=2+i,trigger=101) for i,s in enumerate(symbols)] for d in dates}
    for variant,count in [('base',50),('top20_rvol',40)]:
        r=orb.evaluate(candidates,groups,dates,sessions,15000,1,0,'both_hypothetical_borrow',variant)
        assert r['trades']==count
        assert sum(t['net_pnl'] for t in r['trade_log'])==pytest.approx(r['net_profit'])
        assert sum(d['net_pnl'] for d in r['daily'])==pytest.approx(r['net_profit'])
        for d in r['daily']:
            notional=sum(t['shares']*t['entry_price'] for t in r['trade_log'] if t['date']==d['date'])
            assert notional <= d['start_equity']
        if variant=='top20_rvol':
            assert {t['symbol'] for t in r['trade_log']}==set(symbols[5:])
