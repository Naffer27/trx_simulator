"""
Read-only operational snapshot of the Market Data Engine — FOUNDATION-13.

Usage:
    python manage.py market_data_status --symbol BTCUSD
    python manage.py market_data_status --all
    python manage.py market_data_status --all --json

No network calls. No DB writes. No secrets in the output.

IMPORTANT — per-process limitation: market_data/observability/'s event
store (last tick timestamp, active provider, failover count) lives in the
memory of whichever process is actually running FeedManager (the ASGI/
Daphne server). This command runs in its own separate process, so it
NEVER sees that live in-memory state — those fields will show as empty
("-" / null) here even while the server is actively ticking. What this
command CAN always show correctly, in any process, is everything that is
recomputed fresh and deterministically: router_enabled/allowlisted
(Django settings), market session state (pure calendar rules), circuit
breaker state (FOUNDATION-10's runtime_router singleton — also
per-process, same caveat), and catalog drift (DB-backed, process-
independent). Sharing the observability store across processes (Redis, or
a dedicated market-data service) is future work, same as FOUNDATION-02
§3.4 and FOUNDATION-10's circuit breaker state.
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from market_data.symbol_specs import get_spec
from simulator.market_data_observability import (
    get_all_market_data_health_snapshots,
    get_market_data_health_snapshot,
)

_PER_PROCESS_NOTE = (
    "Note: last_tick_at / active_provider / failover_count reflect this command's OWN "
    "process, not a running ASGI server — see this file's module docstring. Session, "
    "circuit breaker, and drift fields are always fresh regardless of process."
)


def _snapshot_to_dict(snap) -> dict:
    return {
        "canonical_symbol": snap.canonical_symbol,
        "active_provider_id": snap.active_provider_id,
        "active_provider_symbol": snap.active_provider_symbol,
        "router_enabled": snap.router_enabled,
        "router_allowlisted": snap.router_allowlisted,
        "source_state": snap.source_state.value if snap.source_state else None,
        "order_policy": snap.order_policy.value if snap.order_policy else None,
        "market_session_state": snap.market_session_state.value if snap.market_session_state else None,
        "market_calendar_id": snap.market_calendar_id.value if snap.market_calendar_id else None,
        "circuit_states": [
            {
                "provider_id": c.provider_id,
                "health": c.health.value,
                "consecutive_failures": c.consecutive_failures,
                "opened_at": c.opened_at,
                "last_failure_at": c.last_failure_at,
                "last_error_code": c.last_error_code,
            }
            for c in snap.circuit_states
        ],
        "last_tick_at": snap.last_tick_at,
        "tick_age_seconds": snap.tick_age_seconds,
        "stale": snap.stale,
        "synthetic": snap.synthetic,
        "degraded": snap.degraded,
        "last_failover_at": snap.last_failover_at,
        "failover_count": snap.failover_count,
        "catalog_drift_level": snap.catalog_drift_level.value,
        "error_code": snap.error_code,
        "evaluated_at": snap.evaluated_at,
    }


def _breaker_column(snap) -> str:
    if not snap.circuit_states:
        return "-"
    return ",".join(f"{c.provider_id}:{c.health.value}" for c in snap.circuit_states)


def _fmt(value, width) -> str:
    return f"{'' if value is None else value!s:<{width}}"


class Command(BaseCommand):
    help = (
        "Read-only Market Data Engine status: selected provider, circuit breaker, "
        "market session, tick freshness, live/simulation mode, and catalog drift "
        "per symbol. No network calls, no DB writes."
    )

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--symbol", type=str, help="Canonical symbol, e.g. BTCUSD or EUR/USD.")
        group.add_argument("--all", action="store_true", help="Show every registered SymbolSpec.")
        parser.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")

    def handle(self, *args, **options):
        json_mode = options["json"]

        if options["all"]:
            snapshots = get_all_market_data_health_snapshots()
        else:
            symbol = options["symbol"]
            try:
                get_spec(symbol)
            except KeyError:
                raise CommandError(f"unknown_symbol: {symbol!r} is not in market_data/symbol_specs.py")
            snapshots = [get_market_data_health_snapshot(symbol)]

        if json_mode:
            payload = {"snapshots": [_snapshot_to_dict(s) for s in snapshots], "note": _PER_PROCESS_NOTE}
            self.stdout.write(json.dumps(payload, indent=2))
            return

        header = (
            f"  {'SYMBOL':<10} {'SESSION':<12} {'PROVIDER':<10} {'SOURCE':<12} "
            f"{'BREAKER':<24} {'LAST TICK':<10} {'STALE':<6} {'DEGRADED':<9} DRIFT"
        )
        self.stdout.write(header)
        self.stdout.write("  " + "─" * (len(header) - 2))
        for snap in snapshots:
            session = snap.market_session_state.value if snap.market_session_state else "-"
            source = snap.source_state.value if snap.source_state else "-"
            tick_age = f"{snap.tick_age_seconds:.0f}s" if snap.tick_age_seconds is not None else "-"
            style = self.style.WARNING if (snap.stale or snap.degraded) else self.style.SUCCESS
            self.stdout.write(style(
                f"  {_fmt(snap.canonical_symbol, 10)} {_fmt(session, 12)} "
                f"{_fmt(snap.active_provider_id or '-', 10)} {_fmt(source, 12)} "
                f"{_fmt(_breaker_column(snap), 24)} {_fmt(tick_age, 10)} "
                f"{_fmt(snap.stale, 6)} {_fmt(snap.degraded, 9)} {snap.catalog_drift_level.value}"
            ))

        self.stdout.write(f"\n{_PER_PROCESS_NOTE}")
