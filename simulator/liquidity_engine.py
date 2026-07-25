# simulator/liquidity_engine.py
"""
BOOK-05b — Simulated Hedge Pricing.

Two pure functions — no DB, no network, no writes, no side effects of
any kind. This module never touches LiquidityDecision, RoutingDecision,
Position, TradingAccount, or any other model. It never calls
LiquidityProvider.objects.* — the caller (BOOK-05c, not yet built) is
responsible for loading candidate providers and passing them in.

Nothing here is wired into consumers.py, routing_engine.py,
broker_ledger.py, or broker_audit.py. This module has no callers yet —
BOOK-05c is what will eventually call select_simulated_provider() and
evaluate_simulated_hedge() from the real trading flow, gated behind its
own flag (BOOK-05g). Persisting the result (record_liquidity_decision())
is also out of scope here — see docs/BOOK_05_IMPLEMENTATION_PLAN.md,
BOOK-05b.

Costo simulado — resolución de FASE 1 (precisión final de diseño):
`LiquidityProvider.simulated_spread_markup_pips` se mantiene en PIPS,
nunca reinterpretado como basis points — esa interpretación fue evaluada
y descartada por ser técnicamente incorrecta para el conjunto real de
instrumentos de este proyecto (market_data/symbol_specs.py: pip_size
vale 0.0001 para majors, 0.01 para JPY, ~1.0 para cripto — un "pip" no
equivale a 1 bps de precio salvo por coincidencia para pares cercanos a
1.0). La fórmula usada aquí es la misma ya probada en producción por
simulator/spread_engine.py::calculate_spread_revenue() — sin su factor
/2, porque ese factor modela "el bróker captura la mitad del spread
como revenue al abrir", un concepto distinto de "pagar el spread
completo de un LP simulado como costo de cobertura".
"""
from decimal import Decimal

from market_data.symbol_specs import get_spec

# Same dual-versioning rationale as routing_engine.py: ENGINE_VERSION
# tracks the pricing/selection LOGIC, SCHEMA_VERSION tracks the SHAPE of
# inputs_snapshot — independent axes, both start at 1.
ENGINE_VERSION = 1
SCHEMA_VERSION = 1


def select_simulated_provider(providers, symbol: str, exposure_usd: Decimal):
    """
    Pure selection over an already-loaded collection of LiquidityProvider
    objects — never queries the ORM itself. `providers` can be any
    iterable (a list, a pre-evaluated queryset, anything with __iter__).

    Candidate filter: enabled, covers `symbol`, and has enough capacity
    for `exposure_usd`. Among candidates, deterministic order:
        1. lowest simulated_spread_markup_pips (cheapest first)
        2. lowest max_capacity_usd that still covers exposure_usd
           (preserves higher-capacity providers for larger exposure)
        3. name ascending (final, stable tie-break — name is unique=True
           on LiquidityProvider since BOOK-05a, so this always resolves
           to exactly one winner)

    Returns the winning LiquidityProvider, or None if no candidate
    qualifies. Never raises on an empty/no-match `providers`.
    """
    exposure_usd = Decimal(str(exposure_usd))

    candidates = [
        p for p in providers
        if p.enabled
        and symbol in (p.symbols_covered or [])
        and exposure_usd <= p.max_capacity_usd
    ]
    if not candidates:
        return None

    candidates.sort(key=lambda p: (p.simulated_spread_markup_pips, p.max_capacity_usd, p.name))
    return candidates[0]


def evaluate_simulated_hedge(*, symbol: str, side: str, qty, price, provider, routing_profile: str) -> dict:
    """
    Pure calculation — no DB, no network, no writes. `provider` must not
    be None; the caller is responsible for checking
    select_simulated_provider()'s result before calling this.

    exposure_usd = abs(qty * price * contract_size) — same notional
    formula already used by simulator/exposure_engine.py.

    simulated_cost = simulated_spread(pips) * pip_size * abs(qty) * contract_size
    — see module docstring for why this is the definitive formula, not
    a basis-points interpretation of simulated_spread_markup_pips.

    Returns a dict — never a model instance, never persisted. See
    docs/BOOK_05_IMPLEMENTATION_PLAN.md, BOOK-05b for the full contract.
    """
    spec = get_spec(symbol)

    qty_d = Decimal(str(qty))
    price_d = Decimal(str(price))
    contract_size_d = Decimal(str(spec.contract_size))
    pip_size_d = Decimal(str(spec.pip_size))

    exposure_usd = abs(qty_d * price_d * contract_size_d)
    simulated_spread = provider.simulated_spread_markup_pips
    simulated_cost = simulated_spread * pip_size_d * abs(qty_d) * contract_size_d

    return {
        "symbol": symbol,
        "side": side,
        "qty": qty_d,
        "price": price_d,
        "exposure_usd": exposure_usd,
        "provider_id": provider.id,
        "provider_name": provider.name,
        "simulated_spread": simulated_spread,
        "simulated_cost": simulated_cost,
        "routing_profile": routing_profile,
        "engine_version": ENGINE_VERSION,
        "schema_version": SCHEMA_VERSION,
        "inputs_snapshot": {
            "symbol": symbol,
            "side": side,
            "qty": float(qty_d),
            "price": float(price_d),
            "exposure_usd": float(exposure_usd),
            "provider_name": provider.name,
            "routing_profile": routing_profile,
        },
    }
