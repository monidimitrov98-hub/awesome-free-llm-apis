"""Prop firm account protection and compliance engine (Sprint 9).

Profiles are data (``config/prop_firms/profiles/<id>/<version>.toml``); the engine is generic. A
profile is operational only when VERIFIED and activated by a human for its exact content. Nothing in
this package sends orders, changes accounts or submits payouts.
"""
