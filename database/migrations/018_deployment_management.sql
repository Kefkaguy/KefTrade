ALTER TABLE strategy_deployments
ADD COLUMN IF NOT EXISTS scan_cadence TEXT NOT NULL DEFAULT 'scheduler',
ADD COLUMN IF NOT EXISTS max_simulated_exposure_pct NUMERIC(8, 6) NOT NULL DEFAULT 0.100000,
ADD COLUMN IF NOT EXISTS health_status TEXT NOT NULL DEFAULT 'unknown',
ADD COLUMN IF NOT EXISTS health_checked_at TIMESTAMPTZ,
ADD COLUMN IF NOT EXISTS resumed_at TIMESTAMPTZ;

ALTER TABLE strategy_deployments
DROP CONSTRAINT IF EXISTS strategy_deployments_scan_cadence_check;

ALTER TABLE strategy_deployments
ADD CONSTRAINT strategy_deployments_scan_cadence_check
CHECK (scan_cadence IN ('scheduler', 'manual', '15m', '30m', '60m', 'daily'));

ALTER TABLE strategy_deployments
DROP CONSTRAINT IF EXISTS strategy_deployments_exposure_limit_check;

ALTER TABLE strategy_deployments
ADD CONSTRAINT strategy_deployments_exposure_limit_check
CHECK (max_simulated_exposure_pct > 0 AND max_simulated_exposure_pct <= 1);

CREATE INDEX IF NOT EXISTS idx_strategy_deployments_control_center
ON strategy_deployments(status, simulation_only, symbol, timeframe, strategy_name);

CREATE INDEX IF NOT EXISTS idx_execution_logs_deployment_audit
ON execution_logs(deployment_id, created_at DESC)
WHERE simulation_only = TRUE;
