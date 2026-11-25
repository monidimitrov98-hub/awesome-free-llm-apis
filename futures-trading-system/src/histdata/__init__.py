"""Historical market-data infrastructure (Sprint 12): provider-neutral ingestion of MES / MNQ bar data into
immutable, versioned, validated datasets.

Data quality component only. Nothing in this package places, authorizes or simulates orders, and nothing
here can change AccountGuard limits, prop firm profiles, NewsGuard, RiskManager or strategy versions.
Provider adapters only PARSE files that were obtained with the provider's own tools; there is no network
client in this package and no credentials are read.
"""
