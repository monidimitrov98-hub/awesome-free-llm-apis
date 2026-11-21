"""Live market data (Sprint 6): provider-independent ingestion with a read-only NinjaTrader 8 bridge.

READ-ONLY: nothing in this package can submit, modify or cancel an order anywhere. Market data
enters the existing paper runtime through :class:`src.paper.feeds.MarketDataFeed`, so every event
passes the same EventValidator, DataHealth, journal, BarBuilder and protections as replay data.
"""
