"""
market_data/shadow/service.py — shadow route evaluation (FOUNDATION-08).

Runs SymbolSpec -> InstrumentProfile -> ProviderRoutePlan -> ProviderRouter
.decide() purely for observation, and compares it against a declarative
expectation of what market_data/feeds.py's real _try_live() would pick.

Guarantees:
  - No network. No DB. No Django ORM.
  - evaluate_shadow_route() never raises to its caller — any failure
    anywhere in the chain is caught and reported as ShadowResult.error_code.
  - legacy_expected_provider() does not re-implement _try_live() (no
    websockets, no retries, no I/O) — it mirrors only its declarative
    provider-preference order, given today's SymbolSpec config and whether
    FINNHUB_API_KEY is set.

Known limitation (documented, not solved in this block): the ProviderRouter
instance used here is fresh on every call, so it never carries real
consecutive-failure state from FeedManager. Every shadow decision reflects a
healthy/CLOSED circuit breaker. This block proves the pipeline wires
together correctly, not real circuit-breaker behavior against live
failures — see docs/MARKET_DATA_ARCHITECTURE.md's shadow-mode section.
"""

from __future__ import annotations

import os
import time
from typing import Optional

from market_data.instruments.bridges import profile_from_symbol_spec
from market_data.instruments.routing import build_route_plan
from market_data.router.router import ProviderRouter
from market_data.symbol_specs import SymbolSpec, get_spec

from .models import ShadowResult

# Re-read directly (not imported from market_data.feeds) to avoid a circular
# import: market_data.feeds imports this module's evaluate_shadow_route from
# inside a method, so this module cannot import market_data.feeds at module
# scope. Same env var, same read pattern as feeds.py's own FINNHUB_API_KEY.
_FINNHUB_API_KEY = (os.getenv("FINNHUB_API_KEY", "") or "").strip()


def legacy_expected_provider(spec: SymbolSpec) -> Optional[str]:
    """
    Declarative mirror of feeds.py::_try_live()'s provider preference order:
    Binance (if exchange_symbol set) -> Kraken (if kraken_symbol set) ->
    Finnhub (if FINNHUB_API_KEY set and the canonical symbol contains "/")
    -> None (legacy would fall to its internal simulation).

    This is NOT a re-implementation of _try_live() — no connection attempt,
    no retry logic, no websockets. Just: given today's config, which
    provider would legacy try first?
    """
    if spec.exchange_symbol:
        return "binance"
    if spec.kraken_symbol:
        return "kraken"
    if _FINNHUB_API_KEY and "/" in spec.symbol:
        return "finnhub"
    return None


def _unavailable(symbol: str, evaluated_at: int, error_code: str, legacy_provider: Optional[str] = None) -> ShadowResult:
    return ShadowResult(
        canonical_symbol=symbol,
        legacy_expected_provider=legacy_provider,
        shadow_selected_provider=None,
        shadow_source_state=None,
        shadow_order_policy=None,
        degraded=None,
        reason_code=None,
        agrees_with_legacy=None,
        evaluated_at=evaluated_at,
        error_code=error_code,
    )


def evaluate_shadow_route(symbol: str, *, now: Optional[int] = None) -> ShadowResult:
    """
    Observational only. Never raises — any failure becomes
    ShadowResult.error_code. Never touches the network or a database.
    """
    evaluated_at = now if now is not None else int(time.time())

    try:
        return _evaluate_shadow_route_inner(symbol, evaluated_at)
    except Exception as exc:  # last-resort safety net — see module docstring
        return _unavailable(symbol, evaluated_at, f"unexpected_error: {exc!r}")


def _evaluate_shadow_route_inner(symbol: str, evaluated_at: int) -> ShadowResult:
    try:
        spec = get_spec(symbol)
    except KeyError:
        return _unavailable(symbol, evaluated_at, f"unknown_symbol: {symbol!r}")

    legacy_provider = legacy_expected_provider(spec)

    try:
        profile = profile_from_symbol_spec(spec)
        plan = build_route_plan(profile)
        decision = ProviderRouter().decide(plan, now=evaluated_at)
    except Exception as exc:
        return _unavailable(spec.symbol, evaluated_at, f"shadow_build_failed: {exc!r}", legacy_provider)

    return ShadowResult(
        canonical_symbol=spec.symbol,
        legacy_expected_provider=legacy_provider,
        shadow_selected_provider=decision.selected_provider_id,
        shadow_source_state=decision.source_state,
        shadow_order_policy=decision.order_policy,
        degraded=decision.degraded,
        reason_code=decision.reason_code,
        agrees_with_legacy=(legacy_provider == decision.selected_provider_id),
        evaluated_at=evaluated_at,
        error_code=None,
    )
