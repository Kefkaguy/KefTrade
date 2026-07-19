# Phase 10 runtime schema inventory

This report compares runtime schema helpers with migrations `001` through `031` before external broker integration.

## Missing from migrations

| Object | Runtime owner | Resolution |
| --- | --- | --- |
| `research_command_center_snapshots` | `research_campaigns` | Added by migration `032` |
| `research_candidate_objects` | `research_learning` | Added by migration `032` |
| `research_global_learning_snapshots` | `research_learning` | Added by migration `032` |
| `candidate_missing_evidence_plans` | `research_command_center` | Added by migration `032` |

Migration `030` also referenced worker ownership and heartbeat columns that were only supplied by runtime fallback DDL. The migration now adds those columns before creating its live-worker index so a fresh migration chain is valid.

## Already migrated

Runtime helpers for campaign, worker, research architecture, automation, strategy discovery, production validation, candidate lifecycle, deployment management, ranking snapshots, paper lineage, and forward-evidence tables duplicate migrations `009`, `018`, `019`, `020`, `021`, `022`-`028`, and `030`-`031`. These helpers must perform no schema writes after migration `032`.

## Validation rule

Application startup, requests, workers, services, and CLI commands may query schema metadata but may not execute DDL. Production schema changes are applied only by ordered SQL migrations.
