-- Migration 062 may already have been applied before executable auction-price
-- fields were introduced. Keep production upgrades additive and idempotent.

ALTER TABLE intraday_auction_imbalances
    ADD COLUMN IF NOT EXISTS midpoint_at_message NUMERIC;

ALTER TABLE intraday_auction_imbalances
    ADD COLUMN IF NOT EXISTS auction_price NUMERIC;
