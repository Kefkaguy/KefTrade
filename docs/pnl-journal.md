# Alpaca paper P&L journal

The Next.js homepage is a responsive Tailwind conversion of the supplied cream/olive
calendar design. `/paper` preserves the existing intraday paper monitor.

The browser calls the same-origin `/api/pnl-journal?month=YYYY-MM` route. Next.js
forwards this to FastAPI's `/broker/pnl-journal`, using `API_INTERNAL_URL` (preferred)
or `NEXT_PUBLIC_API_URL`. For the existing Vercel/VPS split, set the server-side
`API_INTERNAL_URL` to `https://keftrade.duckdns.org`. Broker credentials stay in the
existing API/broker-worker environment, never in the browser bundle.

Deploy the updated API together with the website. Existing database migrations
through 082 are required; this change adds no migrations and changes no trading
authority. The backend reads the latest paper account's synced account state,
positions, activity fills, ownership records, RUG deployments, and configured
established/intraday strategies. Configurations without account-specific identity
are displayed as configuration records, not proof of account activity.

The broker worker must already be syncing Alpaca. Page refresh reads the database;
it does not force a broker sync or submit orders. The UI shows the broker timestamp,
latest reconciliation status, and API errors. Requests refresh every 30 seconds.

## Accounting boundaries

- Calendar dates use America/New_York and actual activity timestamps.
- Gross P&L is a FIFO reconstruction of stored activity fills, including partial
  lots, short covers, and position reversals. It is an estimate, not audited P&L.
- All stored fills before the selected month's end establish entry basis; only
  matches closed in the selected month appear in the calendar.
- Aggregate/reconstructed fills are excluded to avoid duplicate executions.
- Missing opening inventory, corporate actions, and incomplete history can make
  FIFO estimates unreliable. The first available fill timestamp is visible.
- Entry order records attribute strategies when available. Conflicting strategy
  identities are labeled shared/unattributed; missing identity stays unattributed.
- Matched lots are not presented as independent full strategy trades. No research
  backtest profit is included. A strategy with zero matches has no proven profit.
- Net P&L is unavailable because complete fees are not exposed by this ledger.
  Account-return percentages and equity drawdown are not fabricated from gross P&L.
- Equity, cash and open positions are whole-account snapshots. Calendar and
  strategy filters only affect the closed-lot view.

## Verification

`python -m pytest apps/api/tests/test_pnl_journal.py -q`

From `apps/web`: `npx tsc --noEmit` and `npm run build`.

Live smoke check after deployment: GET `/broker/pnl-journal?month=2026-09` and
compare equity/positions to the same broker sync timestamp. Exercise month and
strategy selection, daily detail, and CSV export. Verify outage states show
unavailable data rather than invented sample numbers.
