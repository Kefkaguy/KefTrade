"""Phase 12.3 Intraday Strategy Library expansion.

Each module here is one strategy family, self-contained (state dataclass,
strategy class satisfying `StrategyProtocol`, default parameters, candidate
generator) so the library scales by adding files, not by editing existing
ones. `registry.py` is the only place that aggregates them.

Opening-Range Breakout v1 and VWAP Reversion v1 (`labs/intraday/strategy.py`,
`labs/intraday/campaign.py`) are archived research evidence and are not
touched by this package -- `registry.py` imports their factories/blocks
unchanged and merges them in alongside the families defined here.
"""
