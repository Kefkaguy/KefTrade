-- Reset KefTrade to a fresh runtime state while preserving schema and configuration.
-- Reference tables and scheduler settings are intentionally excluded.
DO $$
DECLARE
    runtime_tables TEXT;
BEGIN
    SELECT string_agg(format('%I.%I', schemaname, tablename), ', ' ORDER BY tablename)
    INTO runtime_tables
    FROM pg_tables
    WHERE schemaname = 'public'
      AND tablename NOT IN (
          'symbols',
          'strategy_versions',
          'risk_settings',
          'paper_scan_scheduler',
          'research_campaign_scheduler',
          -- Immutable scientific evidence must survive an operational reset.
          'research_dataset_manifests',
          'research_dataset_candles',
          'asset_profile_versions',
          'asset_cluster_versions',
          'asset_cluster_members',
          'research_hypothesis_versions',
          'research_validation_policy_versions',
          'research_campaign_archives'
      );

    IF runtime_tables IS NOT NULL THEN
        EXECUTE 'TRUNCATE TABLE ' || runtime_tables || ' RESTART IDENTITY CASCADE';
    END IF;
END
$$;
