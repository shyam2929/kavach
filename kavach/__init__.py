"""KAVACH — a risk-first, volatility-edge trading system for Nifty weekly options.

Design rule zero: the strategy proposes, the governor disposes. Nothing reaches a
broker without passing kavach.governor.RiskGovernor, and the governor's limits can
only be loosened by a human, with a one-trading-day cooling-off period.
"""
__version__ = "0.1.0"
