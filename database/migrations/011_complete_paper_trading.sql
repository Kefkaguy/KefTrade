ALTER TABLE paper_orders
    ADD COLUMN IF NOT EXISTS trigger_price NUMERIC,
    ADD COLUMN IF NOT EXISTS parent_order_id BIGINT REFERENCES paper_orders(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS stop_loss_price NUMERIC,
    ADD COLUMN IF NOT EXISTS take_profit_price NUMERIC;

ALTER TABLE paper_orders DROP CONSTRAINT IF EXISTS paper_orders_type_check;
ALTER TABLE paper_orders ADD CONSTRAINT paper_orders_type_check
    CHECK (order_type IN ('market', 'limit', 'stop_loss', 'take_profit'));

ALTER TABLE paper_orders DROP CONSTRAINT IF EXISTS paper_orders_trigger_price_check;
ALTER TABLE paper_orders DROP CONSTRAINT IF EXISTS paper_orders_stop_loss_price_check;
ALTER TABLE paper_orders DROP CONSTRAINT IF EXISTS paper_orders_take_profit_price_check;

ALTER TABLE paper_orders ADD CONSTRAINT paper_orders_trigger_price_check
    CHECK (trigger_price IS NULL OR trigger_price > 0);
ALTER TABLE paper_orders ADD CONSTRAINT paper_orders_stop_loss_price_check
    CHECK (stop_loss_price IS NULL OR stop_loss_price > 0);
ALTER TABLE paper_orders ADD CONSTRAINT paper_orders_take_profit_price_check
    CHECK (take_profit_price IS NULL OR take_profit_price > 0);

CREATE INDEX IF NOT EXISTS paper_orders_pending_idx ON paper_orders(status, submitted_at);
CREATE INDEX IF NOT EXISTS paper_orders_parent_idx ON paper_orders(parent_order_id);
CREATE INDEX IF NOT EXISTS execution_logs_account_created_idx ON execution_logs(account_id, created_at DESC);
