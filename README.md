# KefTrade Quant Research Platform

<img width="1024" height="1024" alt="image" src="https://github.com/user-attachments/assets/420dccf3-2360-4955-9c7d-e4b8794e17ed" />


KefTrade is a research-first quantitative strategy platform for deterministic strategy discovery, validation, candidate lifecycle management, and simulation-only forward validation.

The platform is designed around one rule: strategies advance only when stored evidence supports the promotion. Research thresholds, validation gates, elite promotion requirements, and forward-validation requirements are not weakened to force progress.

New campaigns use the reproducible research architecture: exact dataset snapshots, versioned asset profiles, measured clusters, evidence-scored hypotheses, focused 70/20/10 candidate generation, explicit specialist/elite levels, complete rejection funnels, and checksum-verified campaign archives. See [Reproducible Research Architecture](docs/reproducible-research-architecture.md).

KefTrade is still research and simulation only with respect to real capital: it does not trade live capital and does not support leverage, margin, shorting, or automatic live execution. It connects to an Alpaca **Paper** account (fake money, real broker API) for read-only synchronization, deterministic reconciliation, shadow execution, and — as of Phase 11 — actual Alpaca Paper order submission for a deployment that has been explicitly promoted via CLI.

## Current State

The graduation pipeline is `promoted jobs → research champions → validated elites → portfolio → paper trading`, and it is driven from the Elite Builder page.

Champion import (Phase 13.8) turns promoted research jobs into deduped **research champions** — winners inside the exact backtest that found them, not deployable elites. Champion validation (Phase 13.9, `apps/api/app/services/champion_validation.py`) is the gate between the two: it re-runs each champion where the search never looked and graduates only the survivors to `promotion_state = 'elite'`, which is the only state the portfolio solver reads. See [Champion validation and graduation](docs/champion-validation-graduation.md).

### We will still continue from here!
