"""
market_data/runtime_router/service.py — runtime provider selection
(FOUNDATION-09, updated FOUNDATION-10).

Runs SymbolSpec -> InstrumentProfile -> ProviderRoutePlan ->
ProviderRouter.decide() to pick which provider FeedManager should try for
a symbol. This module only decides — it never opens a connection, never
captures a loop, never touches the DB, and never touches the network.

select_runtime_provider() never raises to its caller: any failure anywhere
in the chain is reported as RuntimeSelectionResult.fallback_to_legacy=True
+ error_code, exactly like market_data/shadow/service.py::
evaluate_shadow_route() (FOUNDATION-08) — same pattern, deliberately, since
this is the same class of "boundary that must never crash a live feed
loop" function.

Whether this module is even called, and for which symbols, is entirely
gated by market_data/feeds.py (settings.MARKET_DATA_ROUTER_ENABLED +
MARKET_DATA_ROUTER_SYMBOLS) — this module has no opinion on that; given a
symbol, it always attempts a decision.

FOUNDATION-10: decisions now come from the process-wide ProviderRouter
singleton in market_data/runtime_router/state.py, not a fresh instance per
call — so a decision here reflects real circuit breaker state fed by
market_data/feeds.py's loops via state.record_provider_success()/
record_provider_failure(). See state.py's module docstring for the
multi-worker limitation this still carries.
"""

from __future__ import annotations

import time
from typing import Optional

from market_data.symbol_specs import get_spec

from .models import RuntimeSelectionResult
from .state import build_plan_for_symbol, get_router


def _fallback(symbol: str, error_code: str) -> RuntimeSelectionResult:
    return RuntimeSelectionResult(
        symbol=symbol,
        selected_provider_id=None,
        selected_provider_symbol=None,
        source_state=None,
        reason_code=None,
        used_new_router=False,
        fallback_to_legacy=True,
        error_code=error_code,
    )


def select_runtime_provider(symbol: str, *, now: Optional[int] = None) -> RuntimeSelectionResult:
    """Never raises. now defaults to the real clock; overridable for tests."""
    evaluated_at = now if now is not None else int(time.time())
    try:
        return _select_runtime_provider_inner(symbol, evaluated_at)
    except Exception as exc:  # last-resort safety net — see module docstring
        return _fallback(symbol, f"unexpected_error: {exc!r}")


def _select_runtime_provider_inner(symbol: str, evaluated_at: int) -> RuntimeSelectionResult:
    try:
        spec = get_spec(symbol)
    except KeyError:
        return _fallback(symbol, f"unknown_symbol: {symbol!r}")

    try:
        plan = build_plan_for_symbol(spec.symbol)
        decision = get_router().decide(plan, now=evaluated_at)
    except Exception as exc:
        return _fallback(spec.symbol, f"route_plan_build_failed: {exc!r}")

    return RuntimeSelectionResult(
        symbol=spec.symbol,
        selected_provider_id=decision.selected_provider_id,
        selected_provider_symbol=decision.selected_provider_symbol,
        source_state=decision.source_state,
        reason_code=decision.reason_code,
        used_new_router=True,
        fallback_to_legacy=False,
        error_code=None,
    )
