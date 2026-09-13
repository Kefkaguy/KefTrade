"""Zero-fee diagnostic for the minute replay, not a calibrated broker model.

Alpaca offers commission-free trading for eligible accounts. Remove modeled
commissions entirely to determine whether the rejection depends on those fees.
Regulatory/borrow/financing charges are omitted, so this is a favorable bound.
"""
import json
import run as orb

orb.OFFLINE=True
orb.BAR_MINUTES=1
orb.START='2026-05-01'
orb.TEST_START='2026-06-01'
orb.OUT=orb.OUT/'minute_check'
sessions=orb.calendar()
groups={}
candidates={}
valid={d for d in sessions if d>=orb.TEST_START}
for symbol in orb.SYMBOLS:
    rows,complete,issues=orb.prepare_symbol(symbol,sessions)
    groups[symbol]={d:g[['open','high','low','close','volume']].to_numpy(float) for d,g in complete.items()}
    valid &= set(complete)
    valid -= {x['date'] for x in issues if x['reason'] in {'missing_daily_history','missing_opening_history'}}
    for row in rows:
        candidates.setdefault(row['date'],[]).append(row)
original=orb.simulate_trade


def no_fee(*args):
    t=original(*args)
    if t is not None:
        fee=t['commissions']
        t['net_pnl']+=fee
        t['path'][t['entry_bar']:t['exit_bar']]+=fee/2
        t['path'][t['exit_bar']:]+=fee
        t['commissions']=0.
    return t


orb.simulate_trade=no_fee
results=[]
for bps in (0,2.5,5):
    for direction in ('both_hypothetical_borrow','long_only'):
        for variant in ('base','top20_rvol'):
            r=orb.evaluate(candidates,groups,sorted(valid),sessions,20000,1,bps,direction,variant)
            r.pop('daily')
            r.pop('trade_log')
            r['commission_model']='zero_all_fees_favorable_diagnostic'
            results.append(r)
orb.save_json(orb.OUT/'fee_sensitivity.json',results)
print(json.dumps([{k:r[k] for k in ('variant','direction','adverse_bps_per_side','net_profit','return_pct','profit_factor')} for r in results],indent=2))
