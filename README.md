# KefTrade Quant Research Platform

<img width="1024" height="1024" alt="image" src="https://github.com/user-attachments/assets/420dccf3-2360-4955-9c7d-e4b8794e17ed" />


KefTrade is a research-first quantitative strategy platform for deterministic strategy discovery, validation, candidate lifecycle management, and simulation-only forward validation.

The platform is designed around one rule: strategies advance only when stored evidence supports the promotion. Research thresholds, validation gates, elite promotion requirements, and forward-validation requirements are not weakened to force progress.

New campaigns use the reproducible research architecture: exact dataset snapshots, versioned asset profiles, measured clusters, evidence-scored hypotheses, focused 70/20/10 candidate generation, explicit specialist/elite levels, complete rejection funnels, and checksum-verified campaign archives. See [Reproducible Research Architecture](docs/reproducible-research-architecture.md).

KefTrade is still research and simulation only with respect to real capital: it does not trade live capital and does not support leverage, margin, shorting, or automatic live execution. It connects to an Alpaca **Paper** account (fake money, real broker API) for read-only synchronization, deterministic reconciliation, shadow execution, and — as of Phase 11 — actual Alpaca Paper order submission for a deployment that has been explicitly promoted via CLI.

## Current State
### Everything from VPS will be deleted! The project had over 60 migrations. The project will continue to exist in "https://github.com/Kefkaguy/keftradev1"