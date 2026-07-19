CREATE OR REPLACE FUNCTION prevent_broker_evidence_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'immutable broker evidence cannot be updated or deleted';
END;
$$ LANGUAGE plpgsql;

CREATE TABLE IF NOT EXISTS broker_adapter_releases (
    id BIGSERIAL PRIMARY KEY,
    provider TEXT NOT NULL DEFAULT 'alpaca',
    adapter_version TEXT NOT NULL,
    adapter_contract_version TEXT NOT NULL,
    provider_api_version TEXT NOT NULL,
    normalization_version TEXT NOT NULL,
    behavior_version TEXT NOT NULL,
    change_class TEXT NOT NULL,
    compatible_from TEXT NOT NULL,
    manifest JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(provider, adapter_version),
    CONSTRAINT broker_adapter_release_provider_check CHECK (provider = 'alpaca'),
    CONSTRAINT broker_adapter_release_change_check CHECK (change_class IN ('compatible_patch', 'normalization', 'behavioral', 'incompatible'))
);

CREATE TABLE IF NOT EXISTS broker_accounts (
    id BIGSERIAL PRIMARY KEY,
    provider TEXT NOT NULL DEFAULT 'alpaca',
    environment TEXT NOT NULL DEFAULT 'paper',
    external_account_id TEXT NOT NULL,
    account_number_masked TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'unknown',
    latest_error TEXT,
    last_successful_sync_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(provider, environment, external_account_id),
    CONSTRAINT broker_accounts_provider_check CHECK (provider = 'alpaca'),
    CONSTRAINT broker_accounts_environment_check CHECK (environment = 'paper')
);

CREATE TABLE IF NOT EXISTS broker_sync_runs (
    id BIGSERIAL PRIMARY KEY,
    broker_account_id BIGINT REFERENCES broker_accounts(id) ON DELETE SET NULL,
    trace_id UUID NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'running',
    provider_api_version TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    normalization_version TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    required_components JSONB NOT NULL DEFAULT '[]'::jsonb,
    completed_components JSONB NOT NULL DEFAULT '[]'::jsonb,
    completeness JSONB NOT NULL DEFAULT '{}'::jsonb,
    error JSONB,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    CONSTRAINT broker_sync_runs_status_check CHECK (status IN ('running', 'complete', 'incomplete', 'incompatible', 'failed'))
);

CREATE INDEX IF NOT EXISTS broker_sync_runs_account_created_idx
    ON broker_sync_runs(broker_account_id, started_at DESC);

CREATE TABLE IF NOT EXISTS broker_raw_ingest_events (
    id BIGSERIAL,
    sync_run_id BIGINT NOT NULL REFERENCES broker_sync_runs(id) ON DELETE RESTRICT,
    trace_id UUID NOT NULL,
    provider TEXT NOT NULL DEFAULT 'alpaca',
    environment TEXT NOT NULL DEFAULT 'paper',
    endpoint_class TEXT NOT NULL,
    request_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    response_status INTEGER NOT NULL,
    payload JSONB NOT NULL,
    payload_hash TEXT NOT NULL,
    provider_api_version TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    normalization_version TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(id, received_at),
    CONSTRAINT broker_raw_provider_check CHECK (provider = 'alpaca'),
    CONSTRAINT broker_raw_environment_check CHECK (environment = 'paper'),
    CONSTRAINT broker_raw_payload_hash_check CHECK (length(payload_hash) = 64),
    UNIQUE(sync_run_id, endpoint_class, payload_hash, received_at)
) PARTITION BY RANGE(received_at);

DO $$
DECLARE
    month_offset INTEGER;
    range_start DATE;
    range_end DATE;
    partition_name TEXT;
BEGIN
    FOR month_offset IN -1..24 LOOP
        range_start := (date_trunc('month', CURRENT_DATE) + (month_offset || ' months')::interval)::date;
        range_end := (range_start + INTERVAL '1 month')::date;
        partition_name := 'broker_raw_ingest_events_' || to_char(range_start, 'YYYY_MM');
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS %I PARTITION OF broker_raw_ingest_events FOR VALUES FROM (%L) TO (%L)',
            partition_name,
            range_start,
            range_end
        );
    END LOOP;
END $$;

CREATE TABLE IF NOT EXISTS broker_raw_ingest_events_default
    PARTITION OF broker_raw_ingest_events DEFAULT;

CREATE INDEX IF NOT EXISTS broker_raw_ingest_trace_idx
    ON broker_raw_ingest_events(trace_id, received_at DESC);

CREATE TABLE IF NOT EXISTS broker_raw_archive_manifests (
    id BIGSERIAL PRIMARY KEY,
    archive_key TEXT NOT NULL UNIQUE,
    storage_location TEXT NOT NULL,
    record_count BIGINT NOT NULL,
    range_start TIMESTAMPTZ NOT NULL,
    range_end TIMESTAMPTZ NOT NULL,
    content_hash TEXT NOT NULL,
    verified_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT broker_raw_archive_count_check CHECK (record_count >= 0),
    CONSTRAINT broker_raw_archive_hash_check CHECK (length(content_hash) = 64)
);

CREATE TABLE IF NOT EXISTS broker_daily_summaries (
    summary_date DATE PRIMARY KEY,
    provider TEXT NOT NULL DEFAULT 'alpaca',
    environment TEXT NOT NULL DEFAULT 'paper',
    summary JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT broker_daily_summary_provider_check CHECK (provider = 'alpaca'),
    CONSTRAINT broker_daily_summary_environment_check CHECK (environment = 'paper')
);

CREATE TABLE IF NOT EXISTS broker_account_state (
    broker_account_id BIGINT PRIMARY KEY REFERENCES broker_accounts(id) ON DELETE CASCADE,
    sync_run_id BIGINT NOT NULL REFERENCES broker_sync_runs(id) ON DELETE RESTRICT,
    raw_event_id BIGINT NOT NULL,
    status TEXT NOT NULL,
    currency TEXT NOT NULL,
    cash NUMERIC NOT NULL,
    equity NUMERIC NOT NULL,
    buying_power NUMERIC NOT NULL,
    trading_blocked BOOLEAN NOT NULL,
    account_blocked BOOLEAN NOT NULL,
    trade_suspended_by_user BOOLEAN NOT NULL,
    normalized JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT broker_account_state_values_check CHECK (cash >= 0 AND equity >= 0 AND buying_power >= 0)
);

CREATE TABLE IF NOT EXISTS broker_clock_state (
    broker_account_id BIGINT PRIMARY KEY REFERENCES broker_accounts(id) ON DELETE CASCADE,
    sync_run_id BIGINT NOT NULL REFERENCES broker_sync_runs(id) ON DELETE RESTRICT,
    raw_event_id BIGINT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    is_open BOOLEAN NOT NULL,
    next_open TIMESTAMPTZ NOT NULL,
    next_close TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS broker_account_snapshots (
    id BIGSERIAL PRIMARY KEY,
    broker_account_id BIGINT NOT NULL REFERENCES broker_accounts(id) ON DELETE CASCADE,
    sync_run_id BIGINT NOT NULL REFERENCES broker_sync_runs(id) ON DELETE RESTRICT,
    trace_id UUID NOT NULL,
    raw_event_id BIGINT NOT NULL,
    state JSONB NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS broker_account_snapshots_captured_idx
    ON broker_account_snapshots(broker_account_id, captured_at DESC);

CREATE TABLE IF NOT EXISTS broker_clock_snapshots (
    id BIGSERIAL PRIMARY KEY,
    broker_account_id BIGINT NOT NULL REFERENCES broker_accounts(id) ON DELETE CASCADE,
    sync_run_id BIGINT NOT NULL REFERENCES broker_sync_runs(id) ON DELETE RESTRICT,
    trace_id UUID NOT NULL,
    raw_event_id BIGINT NOT NULL,
    state JSONB NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS broker_clock_snapshots_captured_idx
    ON broker_clock_snapshots(broker_account_id, captured_at DESC);

CREATE TABLE IF NOT EXISTS broker_orders (
    id BIGSERIAL PRIMARY KEY,
    broker_account_id BIGINT NOT NULL REFERENCES broker_accounts(id) ON DELETE CASCADE,
    broker_order_id TEXT NOT NULL,
    client_order_id TEXT NOT NULL,
    sync_run_id BIGINT NOT NULL REFERENCES broker_sync_runs(id) ON DELETE RESTRICT,
    raw_event_id BIGINT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    order_type TEXT NOT NULL,
    time_in_force TEXT NOT NULL,
    requested_quantity NUMERIC NOT NULL,
    filled_quantity NUMERIC NOT NULL DEFAULT 0,
    filled_average_price NUMERIC,
    status TEXT NOT NULL,
    submitted_at TIMESTAMPTZ,
    filled_at TIMESTAMPTZ,
    canceled_at TIMESTAMPTZ,
    expired_at TIMESTAMPTZ,
    normalized JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(broker_account_id, broker_order_id),
    UNIQUE(broker_account_id, client_order_id),
    CONSTRAINT broker_orders_side_check CHECK (side IN ('buy', 'sell')),
    CONSTRAINT broker_orders_quantity_check CHECK (requested_quantity > 0 AND filled_quantity >= 0),
    CONSTRAINT broker_orders_fill_price_check CHECK (filled_average_price IS NULL OR filled_average_price > 0)
);

CREATE INDEX IF NOT EXISTS broker_orders_status_idx
    ON broker_orders(broker_account_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS broker_fills (
    id BIGSERIAL PRIMARY KEY,
    broker_account_id BIGINT NOT NULL REFERENCES broker_accounts(id) ON DELETE CASCADE,
    broker_order_id TEXT NOT NULL,
    broker_activity_id TEXT NOT NULL,
    sync_run_id BIGINT NOT NULL REFERENCES broker_sync_runs(id) ON DELETE RESTRICT,
    raw_event_id BIGINT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity NUMERIC NOT NULL,
    price NUMERIC NOT NULL,
    cumulative_quantity NUMERIC,
    leaves_quantity NUMERIC,
    source TEXT NOT NULL,
    reconstructed BOOLEAN NOT NULL DEFAULT FALSE,
    transaction_at TIMESTAMPTZ NOT NULL,
    normalized JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE(broker_account_id, broker_activity_id),
    CONSTRAINT broker_fills_side_check CHECK (side IN ('buy', 'sell')),
    CONSTRAINT broker_fills_values_check CHECK (quantity > 0 AND price > 0),
    CONSTRAINT broker_fills_source_check CHECK (source IN ('alpaca_account_activity', 'order_aggregate_reconstruction')),
    CONSTRAINT broker_fills_reconstruction_check CHECK ((source = 'order_aggregate_reconstruction') = reconstructed)
);

CREATE TABLE IF NOT EXISTS broker_positions (
    id BIGSERIAL PRIMARY KEY,
    broker_account_id BIGINT NOT NULL REFERENCES broker_accounts(id) ON DELETE CASCADE,
    symbol TEXT NOT NULL,
    sync_run_id BIGINT NOT NULL REFERENCES broker_sync_runs(id) ON DELETE RESTRICT,
    raw_event_id BIGINT NOT NULL,
    quantity NUMERIC NOT NULL,
    average_entry_price NUMERIC NOT NULL,
    market_value NUMERIC NOT NULL,
    unrealized_pl NUMERIC NOT NULL DEFAULT 0,
    normalized JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(broker_account_id, symbol),
    CONSTRAINT broker_positions_values_check CHECK (quantity >= 0 AND average_entry_price >= 0 AND market_value >= 0)
);

CREATE TABLE IF NOT EXISTS broker_position_snapshots (
    id BIGSERIAL PRIMARY KEY,
    broker_account_id BIGINT NOT NULL REFERENCES broker_accounts(id) ON DELETE CASCADE,
    sync_run_id BIGINT NOT NULL REFERENCES broker_sync_runs(id) ON DELETE RESTRICT,
    trace_id UUID NOT NULL,
    raw_event_id BIGINT NOT NULL,
    symbol TEXT NOT NULL,
    state JSONB NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS broker_position_snapshots_captured_idx
    ON broker_position_snapshots(broker_account_id, captured_at DESC);

CREATE TABLE IF NOT EXISTS risk_policy_versions (
    id BIGSERIAL PRIMARY KEY,
    version TEXT NOT NULL UNIQUE,
    policy JSONB NOT NULL,
    policy_hash TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT risk_policy_hash_check CHECK (length(policy_hash) = 64)
);

CREATE TABLE IF NOT EXISTS eligibility_policy_versions (
    id BIGSERIAL PRIMARY KEY,
    version TEXT NOT NULL UNIQUE,
    policy JSONB NOT NULL,
    policy_hash TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT eligibility_policy_hash_check CHECK (length(policy_hash) = 64)
);

CREATE TABLE IF NOT EXISTS deployment_configuration_versions (
    id BIGSERIAL PRIMARY KEY,
    internal_deployment_id BIGINT NOT NULL REFERENCES strategy_deployments(id) ON DELETE RESTRICT,
    version INTEGER NOT NULL,
    campaign_id BIGINT NOT NULL REFERENCES research_campaigns(id) ON DELETE RESTRICT,
    elite_candidate_id BIGINT NOT NULL REFERENCES elite_research_candidates(id) ON DELETE RESTRICT,
    candidate_id TEXT NOT NULL,
    candidate_fingerprint TEXT NOT NULL,
    strategy_name TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    frozen_configuration JSONB NOT NULL,
    approved_by TEXT NOT NULL,
    approved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(internal_deployment_id, version),
    CONSTRAINT deployment_candidate_hash_check CHECK (length(candidate_fingerprint) = 64)
);

CREATE TABLE IF NOT EXISTS external_paper_deployments (
    id BIGSERIAL PRIMARY KEY,
    internal_deployment_id BIGINT NOT NULL REFERENCES strategy_deployments(id) ON DELETE RESTRICT,
    broker_account_id BIGINT NOT NULL REFERENCES broker_accounts(id) ON DELETE RESTRICT,
    campaign_id BIGINT NOT NULL REFERENCES research_campaigns(id) ON DELETE RESTRICT,
    elite_candidate_id BIGINT NOT NULL REFERENCES elite_research_candidates(id) ON DELETE RESTRICT,
    candidate_id TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'disabled',
    active_configuration_version_id BIGINT REFERENCES deployment_configuration_versions(id) ON DELETE RESTRICT,
    approval_ref TEXT,
    approved_at TIMESTAMPTZ,
    latest_blockers JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(internal_deployment_id, broker_account_id),
    CONSTRAINT external_deployment_state_check CHECK (state IN ('disabled', 'readiness_blocked', 'approved', 'enabled_observe_only', 'enabled_execution', 'risk_halted', 'reconciliation_halted', 'manually_halted', 'invalidated')),
    CONSTRAINT external_deployment_approval_check CHECK (state NOT IN ('approved', 'enabled_observe_only', 'enabled_execution') OR (active_configuration_version_id IS NOT NULL AND approval_ref IS NOT NULL AND approved_at IS NOT NULL)),
    CONSTRAINT external_execution_unreachable_check CHECK (state <> 'enabled_execution')
);

CREATE TABLE IF NOT EXISTS external_execution_epochs (
    id BIGSERIAL PRIMARY KEY,
    external_deployment_id BIGINT NOT NULL REFERENCES external_paper_deployments(id) ON DELETE RESTRICT,
    sequence_number INTEGER NOT NULL,
    deployment_configuration_version_id BIGINT NOT NULL REFERENCES deployment_configuration_versions(id) ON DELETE RESTRICT,
    eligibility_policy_version_id BIGINT NOT NULL REFERENCES eligibility_policy_versions(id) ON DELETE RESTRICT,
    risk_policy_version_id BIGINT NOT NULL REFERENCES risk_policy_versions(id) ON DELETE RESTRICT,
    adapter_release_id BIGINT NOT NULL REFERENCES broker_adapter_releases(id) ON DELETE RESTRICT,
    candidate_fingerprint TEXT NOT NULL,
    activation_operator TEXT NOT NULL,
    feature_flags JSONB NOT NULL,
    starting_state JSONB NOT NULL DEFAULT '{}'::jsonb,
    activated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at TIMESTAMPTZ,
    closing_state TEXT,
    closing_reason TEXT,
    UNIQUE(external_deployment_id, sequence_number),
    CONSTRAINT execution_epoch_sequence_check CHECK (sequence_number > 0),
    CONSTRAINT execution_epoch_hash_check CHECK (length(candidate_fingerprint) = 64)
);

CREATE UNIQUE INDEX IF NOT EXISTS external_execution_epochs_one_open_idx
    ON external_execution_epochs(external_deployment_id) WHERE closed_at IS NULL;

CREATE TABLE IF NOT EXISTS external_deployment_transitions (
    id BIGSERIAL PRIMARY KEY,
    external_deployment_id BIGINT NOT NULL REFERENCES external_paper_deployments(id) ON DELETE RESTRICT,
    execution_epoch_id BIGINT REFERENCES external_execution_epochs(id) ON DELETE RESTRICT,
    trace_id UUID NOT NULL,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    operator TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS adapter_compatibility_validations (
    id BIGSERIAL PRIMARY KEY,
    trace_id UUID NOT NULL UNIQUE,
    from_release_id BIGINT REFERENCES broker_adapter_releases(id) ON DELETE RESTRICT,
    to_release_id BIGINT NOT NULL REFERENCES broker_adapter_releases(id) ON DELETE RESTRICT,
    status TEXT NOT NULL,
    comparison JSONB NOT NULL,
    validated_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT adapter_validation_status_check CHECK (status IN ('passed', 'failed', 'blocked'))
);

CREATE TABLE IF NOT EXISTS eligibility_decisions (
    id BIGSERIAL PRIMARY KEY,
    external_deployment_id BIGINT NOT NULL REFERENCES external_paper_deployments(id) ON DELETE RESTRICT,
    execution_epoch_id BIGINT REFERENCES external_execution_epochs(id) ON DELETE RESTRICT,
    trace_id UUID NOT NULL,
    sync_run_id BIGINT REFERENCES broker_sync_runs(id) ON DELETE RESTRICT,
    eligibility_policy_version_id BIGINT NOT NULL REFERENCES eligibility_policy_versions(id) ON DELETE RESTRICT,
    risk_policy_version_id BIGINT NOT NULL REFERENCES risk_policy_versions(id) ON DELETE RESTRICT,
    deployment_configuration_version_id BIGINT NOT NULL REFERENCES deployment_configuration_versions(id) ON DELETE RESTRICT,
    adapter_release_id BIGINT NOT NULL REFERENCES broker_adapter_releases(id) ON DELETE RESTRICT,
    eligible BOOLEAN NOT NULL,
    operational_phase TEXT NOT NULL,
    checks JSONB NOT NULL,
    candidate_fingerprint TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT eligibility_phase_check CHECK (operational_phase IN ('eligible_to_start_forward_validation', 'forward_validation_in_progress', 'forward_validated', 'blocked'))
);

CREATE TABLE IF NOT EXISTS execution_risk_decisions (
    id BIGSERIAL PRIMARY KEY,
    external_deployment_id BIGINT NOT NULL REFERENCES external_paper_deployments(id) ON DELETE RESTRICT,
    execution_epoch_id BIGINT NOT NULL REFERENCES external_execution_epochs(id) ON DELETE RESTRICT,
    trace_id UUID NOT NULL,
    eligibility_decision_id BIGINT NOT NULL REFERENCES eligibility_decisions(id) ON DELETE RESTRICT,
    risk_policy_version_id BIGINT NOT NULL REFERENCES risk_policy_versions(id) ON DELETE RESTRICT,
    eligibility_policy_version_id BIGINT NOT NULL REFERENCES eligibility_policy_versions(id) ON DELETE RESTRICT,
    deployment_configuration_version_id BIGINT NOT NULL REFERENCES deployment_configuration_versions(id) ON DELETE RESTRICT,
    adapter_release_id BIGINT NOT NULL REFERENCES broker_adapter_releases(id) ON DELETE RESTRICT,
    approved BOOLEAN NOT NULL,
    requested_quantity NUMERIC NOT NULL,
    approved_quantity NUMERIC NOT NULL DEFAULT 0,
    expected_risk NUMERIC NOT NULL DEFAULT 0,
    projected_exposure NUMERIC NOT NULL DEFAULT 0,
    checks JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT execution_risk_values_check CHECK (requested_quantity >= 0 AND approved_quantity >= 0 AND expected_risk >= 0 AND projected_exposure >= 0)
);

CREATE TABLE IF NOT EXISTS external_execution_signals (
    id BIGSERIAL PRIMARY KEY,
    external_deployment_id BIGINT NOT NULL REFERENCES external_paper_deployments(id) ON DELETE RESTRICT,
    execution_epoch_id BIGINT NOT NULL REFERENCES external_execution_epochs(id) ON DELETE RESTRICT,
    trace_id UUID NOT NULL,
    execution_key TEXT NOT NULL UNIQUE,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    completed_bar_timestamp TIMESTAMPTZ NOT NULL,
    signal_type TEXT NOT NULL,
    signal JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS proposed_broker_orders (
    id BIGSERIAL PRIMARY KEY,
    external_deployment_id BIGINT NOT NULL REFERENCES external_paper_deployments(id) ON DELETE RESTRICT,
    execution_epoch_id BIGINT NOT NULL REFERENCES external_execution_epochs(id) ON DELETE RESTRICT,
    signal_id BIGINT NOT NULL REFERENCES external_execution_signals(id) ON DELETE RESTRICT,
    eligibility_decision_id BIGINT NOT NULL REFERENCES eligibility_decisions(id) ON DELETE RESTRICT,
    risk_decision_id BIGINT NOT NULL REFERENCES execution_risk_decisions(id) ON DELETE RESTRICT,
    trace_id UUID NOT NULL,
    client_order_id TEXT NOT NULL UNIQUE,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity NUMERIC NOT NULL,
    reference_price NUMERIC NOT NULL,
    stop_price NUMERIC NOT NULL,
    target_price NUMERIC,
    expected_risk NUMERIC NOT NULL,
    status TEXT NOT NULL DEFAULT 'shadow_only',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT proposed_order_side_check CHECK (side = 'buy'),
    CONSTRAINT proposed_order_values_check CHECK (quantity > 0 AND reference_price > 0 AND stop_price > 0 AND expected_risk >= 0),
    CONSTRAINT proposed_order_shadow_check CHECK (status = 'shadow_only')
);

CREATE TABLE IF NOT EXISTS shadow_executions (
    id BIGSERIAL PRIMARY KEY,
    external_deployment_id BIGINT NOT NULL REFERENCES external_paper_deployments(id) ON DELETE RESTRICT,
    execution_epoch_id BIGINT NOT NULL REFERENCES external_execution_epochs(id) ON DELETE RESTRICT,
    proposed_order_id BIGINT REFERENCES proposed_broker_orders(id) ON DELETE RESTRICT,
    trace_id UUID NOT NULL,
    would_submit BOOLEAN NOT NULL,
    rejection_reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
    decision JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS shadow_executions_epoch_idx
    ON shadow_executions(execution_epoch_id, created_at DESC);

CREATE TABLE IF NOT EXISTS broker_reconciliation_runs (
    id BIGSERIAL PRIMARY KEY,
    broker_account_id BIGINT NOT NULL REFERENCES broker_accounts(id) ON DELETE RESTRICT,
    sync_run_id BIGINT NOT NULL UNIQUE REFERENCES broker_sync_runs(id) ON DELETE RESTRICT,
    trace_id UUID NOT NULL UNIQUE,
    status TEXT NOT NULL,
    summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    CONSTRAINT broker_reconciliation_status_check CHECK (status IN ('running', 'clean', 'findings', 'failed'))
);

CREATE TABLE IF NOT EXISTS broker_reconciliation_findings (
    id BIGSERIAL PRIMARY KEY,
    reconciliation_run_id BIGINT NOT NULL REFERENCES broker_reconciliation_runs(id) ON DELETE RESTRICT,
    execution_epoch_id BIGINT REFERENCES external_execution_epochs(id) ON DELETE RESTRICT,
    trace_id UUID NOT NULL,
    finding_key TEXT NOT NULL,
    finding_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    details JSONB NOT NULL,
    resolved_at TIMESTAMPTZ,
    resolution JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(reconciliation_run_id, finding_key),
    CONSTRAINT reconciliation_finding_severity_check CHECK (severity IN ('info', 'warning', 'critical')),
    CONSTRAINT reconciliation_finding_scope_check CHECK (scope_type IN ('deployment', 'asset', 'account', 'global'))
);

CREATE TABLE IF NOT EXISTS execution_halts (
    id BIGSERIAL PRIMARY KEY,
    trace_id UUID NOT NULL,
    scope_type TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    severity TEXT NOT NULL,
    evidence JSONB NOT NULL,
    occurrence_count INTEGER NOT NULL DEFAULT 1,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    cleared_at TIMESTAMPTZ,
    cleared_by TEXT,
    clearance_reason TEXT,
    CONSTRAINT execution_halt_scope_check CHECK (scope_type IN ('deployment', 'asset', 'account', 'global')),
    CONSTRAINT execution_halt_severity_check CHECK (severity IN ('warning', 'critical')),
    CONSTRAINT execution_halt_count_check CHECK (occurrence_count > 0)
);

CREATE UNIQUE INDEX IF NOT EXISTS execution_halts_active_unique_idx
    ON execution_halts(scope_type, scope_key, reason_code) WHERE cleared_at IS NULL;

CREATE TABLE IF NOT EXISTS broker_audit_events (
    id BIGSERIAL PRIMARY KEY,
    trace_id UUID NOT NULL,
    event_type TEXT NOT NULL,
    operator TEXT NOT NULL,
    phase TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'alpaca',
    environment TEXT NOT NULL DEFAULT 'paper',
    broker_account_id BIGINT REFERENCES broker_accounts(id) ON DELETE RESTRICT,
    external_deployment_id BIGINT REFERENCES external_paper_deployments(id) ON DELETE RESTRICT,
    execution_epoch_id BIGINT REFERENCES external_execution_epochs(id) ON DELETE RESTRICT,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT broker_audit_provider_check CHECK (provider = 'alpaca'),
    CONSTRAINT broker_audit_environment_check CHECK (environment = 'paper'),
    CONSTRAINT broker_audit_phase_check CHECK (phase IN ('before', 'after', 'automatic'))
);

CREATE TABLE IF NOT EXISTS external_paper_closed_trade_evidence (
    id BIGSERIAL PRIMARY KEY,
    evidence_key TEXT NOT NULL UNIQUE,
    broker_account_id BIGINT NOT NULL REFERENCES broker_accounts(id) ON DELETE RESTRICT,
    external_deployment_id BIGINT NOT NULL REFERENCES external_paper_deployments(id) ON DELETE RESTRICT,
    execution_epoch_id BIGINT NOT NULL REFERENCES external_execution_epochs(id) ON DELETE RESTRICT,
    campaign_id BIGINT NOT NULL REFERENCES research_campaigns(id) ON DELETE RESTRICT,
    candidate_id TEXT NOT NULL,
    entry_order_id BIGINT NOT NULL REFERENCES broker_orders(id) ON DELETE RESTRICT,
    exit_order_id BIGINT NOT NULL REFERENCES broker_orders(id) ON DELETE RESTRICT,
    evidence JSONB NOT NULL,
    opened_at TIMESTAMPTZ NOT NULL,
    closed_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DO $$
DECLARE
    table_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'broker_adapter_releases', 'broker_raw_ingest_events',
        'broker_raw_archive_manifests', 'risk_policy_versions',
        'eligibility_policy_versions', 'deployment_configuration_versions',
        'external_deployment_transitions',
        'adapter_compatibility_validations', 'eligibility_decisions',
        'execution_risk_decisions', 'external_execution_signals',
        'proposed_broker_orders', 'shadow_executions', 'broker_audit_events',
        'external_paper_closed_trade_evidence'
    ]
    LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS %I_immutable_trigger ON %I', table_name, table_name);
        EXECUTE format('CREATE TRIGGER %I_immutable_trigger BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION prevent_broker_evidence_mutation()', table_name, table_name);
    END LOOP;
END $$;
