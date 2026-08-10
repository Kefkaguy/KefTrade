-- Alpaca Paper reports short positions with negative quantity. The broker
-- evidence layer must preserve that signed exposure instead of rejecting it.

ALTER TABLE broker_positions
DROP CONSTRAINT IF EXISTS broker_positions_values_check;

ALTER TABLE broker_positions
ADD CONSTRAINT broker_positions_values_check
CHECK (average_entry_price >= 0 AND market_value >= 0);
