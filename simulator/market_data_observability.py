"""
simulator/market_data_observability.py — FOUNDATION-13 orchestration.

market_data/observability/ stays Django-free (matching every other
market_data/* package's isolation guarantee — FOUNDATION-12 set this
precedent for market_data/catalog/), so the piece that needs
django.conf.settings and simulator.runtime_instrument_catalog lives here
instead. This is the only module allowed to bridge "the pure observability
snapshot builder" and "Django settings / the DB-aware catalog drift check"
together.

get_market_data_health_snapshot() never raises — see module docstring in
market_data/observability/service.py for the boundary-service pattern
this follows.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from django.conf import settings

from market_data.observability import CatalogDriftLevel, MarketDataHealthSnapshot, build_snapshot
from market_data.symbol_specs import get_all_specs

log = logging.getLogger("simulator.ws")


def router_enabled() -> bool:
    return bool(getattr(settings, "MARKET_DATA_ROUTER_ENABLED", False))


def router_allowlisted(symbol: str) -> bool:
    allowlist = getattr(settings, "MARKET_DATA_ROUTER_SYMBOLS", frozenset())
    return symbol in allowlist


def stale_after_seconds() -> float:
    """Reuses the existing Redis price-cache TTL (market_data/feeds.py) as
    the official staleness threshold — FOUNDATION-13 does not invent a
    second policy. That TTL is already what the Celery daemon relies on
    (simulator/tasks.py::_read_cached_price) to decide a cached price is
    unusable, so a tick older than it is "stale" by the same definition
    the rest of the system already uses."""
    from market_data.feeds import _PRICE_CACHE_TTL
    return float(_PRICE_CACHE_TTL)


def catalog_drift_level(symbol: str) -> CatalogDriftLevel:
    """Gated by the existing MARKET_DATA_CATALOG_DRIFT_CHECK_ENABLED flag
    (FOUNDATION-12) — zero DB queries when it's False. Never raises."""
    try:
        from simulator.runtime_instrument_catalog import (
            check_runtime_catalog_drift,
            runtime_catalog_drift_check_enabled,
        )

        if not runtime_catalog_drift_check_enabled():
            return CatalogDriftLevel.NOT_CHECKED

        report = check_runtime_catalog_drift(symbol)
        if report is None:
            return CatalogDriftLevel.NO_DATA
        if report.critical_differences:
            return CatalogDriftLevel.CRITICAL
        if report.warning_differences:
            return CatalogDriftLevel.WARNING
        return CatalogDriftLevel.MATCH
    except Exception as exc:
        log.debug("[observability] catalog_drift_level failed for %s (non-fatal): %r", symbol, exc)
        return CatalogDriftLevel.UNAVAILABLE


def get_market_data_health_snapshot(symbol: str, *, now: Optional[float] = None) -> MarketDataHealthSnapshot:
    """Assembles a full MarketDataHealthSnapshot for one canonical symbol,
    combining Django settings + the DB-aware catalog drift check with the
    pure market_data.observability.build_snapshot(). Never raises — any
    failure gathering the Django-side inputs falls back to safe defaults
    so build_snapshot() (itself never-raising) still returns a usable,
    degraded snapshot rather than propagating."""
    evaluated_at = now if now is not None else time.time()
    try:
        enabled = router_enabled()
        allowlisted = router_allowlisted(symbol)
    except Exception:
        enabled = False
        allowlisted = False

    try:
        drift = catalog_drift_level(symbol)
    except Exception:
        drift = CatalogDriftLevel.UNAVAILABLE

    try:
        threshold = stale_after_seconds()
    except Exception:
        threshold = 60.0

    return build_snapshot(
        symbol,
        router_enabled=enabled,
        router_allowlisted=allowlisted,
        catalog_drift_level=drift,
        stale_after_seconds=threshold,
        now=evaluated_at,
    )


def get_all_market_data_health_snapshots(*, now: Optional[float] = None) -> list[MarketDataHealthSnapshot]:
    """One snapshot per registered SymbolSpec — used by
    `python manage.py market_data_status --all`."""
    evaluated_at = now if now is not None else time.time()
    return [
        get_market_data_health_snapshot(spec.symbol, now=evaluated_at)
        for spec in get_all_specs()
    ]
