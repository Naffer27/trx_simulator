"""Shared ledger allocation and opt-in static pending-order spread pricing.

No writes, no price changes. Dynamic pending pricing requires a validated
quote-level decision shared by WS and daemon and is deliberately rejected
rather than silently charged as static.
"""
from decimal import Decimal, ROUND_HALF_EVEN
import math


class PendingSpreadUnavailable(ValueError):
    pass


def allocate_opening_charges(balance, commission, spread):
    """Match the persisted two-decimal balance, including half-cent ties.

    Return exact balance, posted commission and posted spread. The residual
    belongs to spread when both components exist, as on the manual path.
    """
    balance, commission, spread = map(Decimal, (balance, commission, spread))
    if not all(v.is_finite() for v in (balance, commission, spread)):
        raise ValueError('Non-finite opening charge')
    if commission < 0 or spread < 0:
        raise ValueError('Negative opening charge')
    exact = balance - commission - spread
    posted_total = balance - exact.quantize(Decimal('0.01'), rounding=ROUND_HALF_EVEN)
    if commission > 0 and spread > 0:
        commission_posted = commission.quantize(Decimal('0.01'), rounding=ROUND_HALF_EVEN)
        spread_posted = posted_total - commission_posted
    elif commission > 0:
        commission_posted, spread_posted = posted_total, Decimal('0')
    elif spread > 0:
        commission_posted, spread_posted = Decimal('0'), posted_total
    else:
        commission_posted = spread_posted = Decimal('0')
    return exact, commission_posted, spread_posted


def pending_static_spread_charge(account, symbol, qty):
    """Read authoritative static config once, reuse manual-path formulas.

    Bounds are opt-in exactly as in spread_config_cache. Do not use that
    process-local cache in a daemon, where it may not be hydrated.
    """
    from market_data.symbol_specs import normalize_symbol
    from .models import BrokerSpreadConfig
    from .commercial_pricing import resolve_commercial_pricing_fields
    from .spread_engine import compute_effective_spread_pips, calculate_spread_revenue
    from .pricing_context import build_pricing_context

    symbol = normalize_symbol(symbol)
    cfg = BrokerSpreadConfig.objects.filter(symbol=symbol, enabled=True).first()
    if cfg is not None and cfg.is_dynamic:
        raise PendingSpreadUnavailable('Dynamic pending spread needs a shared quote decision')
    fields = resolve_commercial_pricing_fields(account)
    if fields.get('source') == 'capture_failed':
        raise PendingSpreadUnavailable('Commercial profile unavailable')
    base = float(cfg.spread_pips) if cfg else 0.0
    markup = float(fields.get('spread_markup_pips') or 0.0)
    floor, ceiling = fields.get('min_spread_pips'), fields.get('max_spread_pips')
    if cfg and cfg.spread_bounds_enabled:
        floor = cfg.min_spread if floor is None else floor
        ceiling = cfg.max_spread if ceiling is None else ceiling
    floor = float(floor) if floor is not None else None
    ceiling = float(ceiling) if ceiling is not None else None
    values = [base, markup] + [v for v in (floor, ceiling) if v is not None]
    if any(not math.isfinite(v) or v < 0 for v in values):
        raise PendingSpreadUnavailable('Invalid spread configuration')
    if floor is not None and ceiling is not None and floor > ceiling:
        raise PendingSpreadUnavailable('Spread floor exceeds ceiling')
    before, effective = compute_effective_spread_pips(base, markup, floor, ceiling)
    charge = Decimal(str(calculate_spread_revenue(symbol, qty, effective)))
    if not charge.is_finite() or charge < 0:
        raise PendingSpreadUnavailable('Invalid spread charge')
    context = build_pricing_context(
        base_spread_pips=base, account_markup_pips=markup,
        min_spread_pips=floor, max_spread_pips=ceiling,
        effective_before_bounds=before, effective_after_bounds=effective,
        dynamic_spread_enabled=False, profile_id=fields.get('profile_id'),
        pricing_profile='pending_static_open',
    )
    return charge, context
