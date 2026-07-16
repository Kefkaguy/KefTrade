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
          'research_campaign_scheduler'
      );

    IF runtime_tables IS NOT NULL THEN
        EXECUTE 'TRUNCATE TABLE ' || runtime_tables || ' RESTART IDENTITY CASCADE';
    END IF;
END
$$;
