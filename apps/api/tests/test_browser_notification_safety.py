from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
NOTIFICATION_FILES = [
    ROOT / "apps" / "web" / "lib" / "evidence-notifications.ts",
    ROOT / "apps" / "web" / "components" / "PaperActions.tsx",
]


def notification_source() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in NOTIFICATION_FILES)


def test_browser_notification_copy_avoids_forbidden_financial_advice_phrases() -> None:
    source = notification_source().lower()

    forbidden = [
        "buy tsla",
        "sell tsla",
        "guaranteed profit",
        "easy money",
        "strong buy",
        "trade now",
    ]

    assert all(phrase not in source for phrase in forbidden)
    assert "research-only. no trade executed." in source


def test_browser_notifications_do_not_create_orders_or_mutate_deployments() -> None:
    source = notification_source()

    forbidden_calls = [
        "createPaperOrder(",
        "createStrategyDeployment(",
        "deployTslaMomentumBull(",
        "scanStrategyDeployment(",
        "pauseStrategyDeployment(",
        "updatePaperScheduler(",
        "runPaperSchedulerNow(",
        "TradingClient",
        "submit_order",
        "broker",
        "alpaca.trading",
    ]

    start = source.find("export function EvidenceNotificationControls")
    end = source.find("export function CreateDeployment")
    notification_sections = source[start:end]
    assert all(call not in notification_sections for call in forbidden_calls)


def test_browser_notification_settings_are_local_and_simulation_only() -> None:
    source = notification_source()

    assert "localStorage" in source
    assert "browser_notifications_enabled" in source
    assert "alert_min_severity" in source
    assert "notify_on_research_opportunity" in source
    assert "notify_on_exit_risk" in source
    assert "notify_on_scheduler_error" in source
    assert "notify_on_stale_data" in source
    assert "new Notification" in source
