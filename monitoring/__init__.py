"""Operational alerting for the OKORO FX backend.

Everything in this app is best-effort: alerts are delivered from a background
daemon thread, every failure is swallowed, and a missing webhook simply turns
reporting off. Nothing here may slow down or break a request.
"""
