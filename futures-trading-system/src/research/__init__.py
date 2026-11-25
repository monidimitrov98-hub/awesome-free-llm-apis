"""Quant research engine (Sprint 11): strategy registry, immutable parameter versions, research runs with
statistical safeguards, Monte Carlo robustness, evaluation reports and human promotion gates.

Research code proposes and evaluates signals only. It never creates authorization tokens, changes
AccountGuard limits or compliance rules, activates prop firm profiles, promotes strategies by itself or
reaches a real order path: every simulated order goes through the existing BacktestEngine (and, for shadow /
paper, the existing PaperBroker path) with every protection layer.
"""
