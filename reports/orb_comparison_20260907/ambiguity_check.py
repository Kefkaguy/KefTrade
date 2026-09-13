"""Quantify entry-bar ordering sensitivity without selecting new strategy rules."""
import json
import run as orb

orb.OFFLINE=True
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
results=[]
for optimistic in (False,True):
    orb.simulate_trade=lambda *a: original(*a,optimistic_intrabar=optimistic)
    for variant in ('base','top20_rvol'):
        r=orb.evaluate(candidates,groups,sorted(valid),sessions,20000,1,2.5,'both_hypothetical_borrow',variant)
        r.pop('daily')
        r.pop('trade_log')
        r['optimistic_entry_bar_ordering']=optimistic
        results.append(r)
orb.save_json(orb.OUT/'ambiguity_check.json',results)
print(json.dumps([{k:r[k] for k in ('variant','optimistic_entry_bar_ordering','net_profit','return_pct','profit_factor','win_rate_pct')} for r in results],indent=2))
