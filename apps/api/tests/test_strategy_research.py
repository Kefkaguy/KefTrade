from app.services.strategy_research import build_parameter_sweep, run_strategy_research


def test_parameter_sweep_generates_valid_variants() -> None:
    base = {
        "ema_fast": 20,
        "ema_slow": 50,
        "rsi_min": 40,
        "rsi_max": 60,
        "volume_change_min": -0.25,
        "entry_distance_to_ema20_max": 0.015,
        "swing_lookback": 5,
        "risk_reward": 2,
        "fee_rate": 0.001,
        "slippage_rate": 0.0005,
        "risk_per_trade": 0.01,
        "initial_equity": 10000,
        "walk_forward_train_ratio": 0.7,
    }
    sweep = {
        "ema_fast": [10, 60],
        "ema_slow": [50],
        "rsi_min": [35],
        "rsi_max": [65],
        "risk_reward": [2],
        "swing_lookback": [5],
    }

    variants = build_parameter_sweep(base, sweep)

    assert len(variants) == 1
    assert variants[0]["ema_fast"] == 10
    assert variants[0]["ema_slow"] == 50


def test_strategy_research_ranks_runs_from_identical_data(monkeypatch) -> None:
    from app.services import strategy_research

    base = {
        "ema_fast": 20,
        "ema_slow": 50,
        "rsi_min": 40,
        "rsi_max": 60,
        "volume_change_min": -0.25,
        "entry_distance_to_ema20_max": 0.015,
        "swing_lookback": 5,
        "risk_reward": 2,
        "fee_rate": 0.001,
        "slippage_rate": 0.0005,
        "risk_per_trade": 0.01,
        "initial_equity": 10000,
        "walk_forward_train_ratio": 0.7,
    }
    sweep = {
        "ema_fast": [10],
        "ema_slow": [50],
        "rsi_min": [35],
        "rsi_max": [65],
        "risk_reward": [1.5, 2.5],
        "swing_lookback": [5],
    }

    def fake_backtest(candles, features, params):
        assert candles == ["same candles"]
        assert features == ["same features"]
        profit_factor = 2 if params["risk_reward"] == 2.5 else 1
        return {
            "metrics": {
                "profit_factor": profit_factor,
                "expectancy_per_trade": profit_factor * 10,
                "max_drawdown": 0.05,
                "sharpe_ratio": profit_factor,
                "win_rate": 0.5,
                "number_of_trades": 12,
                "walk_forward": {"enabled": True},
            },
            "trades": [],
        }

    monkeypatch.setattr(strategy_research, "run_backtest", fake_backtest)

    report = run_strategy_research(
        candles=["same candles"],
        features=["same features"],
        strategy_name="trend_pullback",
        strategy_version="v1",
        base_params=base,
        sweep=sweep,
    )

    assert report["run_count"] == 2
    assert report["ranking_table"][0]["parameters"]["risk_reward"] == 2.5
    assert "profit_factor" in report["charts"]
