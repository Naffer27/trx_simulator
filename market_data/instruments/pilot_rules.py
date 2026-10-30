"""Proposed demo economics, NOT an execution registry or activation mechanism.

No consumers/feeds import this module. Rules may be previewed without touching
accounts. Provider prices, execution permission and expiry are separate gates.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from types import MappingProxyType

D = Decimal


@dataclass(frozen=True)
class PilotRule:
    symbol: str
    provider_reference: str
    units_per_lot: Decimal
    min_lot: Decimal
    max_lot: Decimal
    lot_step: Decimal
    price_increment: Decimal
    max_leverage: int
    commission_mode: str
    session: str
    blockers: tuple[str, ...]
    specification_basis: str
    # All pilot rules stay non-executable, even with a successful price lookup.
    enabled: bool = False


_RULES = {
    'XAU/USD': PilotRule('XAU/USD', 'C:XAUUSD', D('100'), D('.01'), D('50'), D('.01'),
        D('.01'), 100, 'account_per_lot', 'METALS_23_5',
        ('websocket_quotes_unverified', 'history_execution_consistency', 'demo_end_to_end'),
        'Existing disabled SymbolSpec; Massive quote is USD per troy ounce.'),
    'SOLUSD': PilotRule('SOLUSD', 'X:SOLUSD', D('1'), D('.1'), D('1000'), D('.1'),
        D('.01'), 10, 'notional_pct', 'CRYPTO_24_7',
        ('websocket_bid_ask_unverified', 'demo_symbol_fee_integration', 'demo_end_to_end'),
        'Existing disabled SymbolSpec; 0.01% opening fee approved for demo.'),
    'KASUSD': PilotRule('KASUSD', 'X:KASUSD', D('1'), D('1'), D('100000'), D('1'),
        D('.000001'), 2, 'notional_pct', 'CRYPTO_24_7',
        ('websocket_bid_ask_unverified', 'precision_validation', 'runtime_spec_missing',
         'demo_symbol_fee_integration', 'demo_end_to_end'),
        'New conservative demo proposal, not venue lot limits; 0.01% opening fee approved.'),
    'US30': PilotRule('US30', 'I:DJI', D('1'), D('.1'), D('100'), D('.1'),
        D('1'), 20, 'account_per_lot', 'US_INDICES_CFD',
        ('snapshot_http_403', 'index_value_is_not_bid_ask', 'demo_end_to_end'),
        'Existing disabled SymbolSpec; index source is not a CFD execution quote.'),
    'WTI': PilotRule('WTI', 'CL', D('1000'), D('1'), D('10'), D('1'),
        D('.01'), 10, 'account_per_lot', 'PROVIDER_CONTRACT_SCHEDULE_REQUIRED',
        ('snapshot_http_403', 'dated_contract_selection', 'expiry_and_session_enforcement',
         'runtime_spec_missing', 'demo_end_to_end'),
        'Massive single CL: 1000 barrels, tick .01. Demo leverage/maximum are broker proposals, '
        'not exchange margin. Tracks a dated future; no physical delivery or venue routing.'),
}
PILOT_RULES = MappingProxyType(_RULES)


def _positive(value, name):
    try:
        result = D(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f'{name} must be a finite positive decimal') from exc
    if not result.is_finite() or result <= 0:
        raise ValueError(f'{name} must be a finite positive decimal')
    return result


def preview_economics(symbol, lots, price, account_leverage, tier='ECN'):
    """Illustration only: USD instruments, before spread, swap and price changes.

    Does not authorize orders. No wallet/account/database access. Unrounded
    amounts are returned: ledger rounding must use the existing posting helper.
    """
    rule = PILOT_RULES[symbol]
    qty = _positive(lots, 'lots')
    px = _positive(price, 'price')
    leverage = _positive(account_leverage, 'account_leverage')
    if leverage != leverage.to_integral_value():
        raise ValueError('account_leverage must be an integer')
    if not rule.min_lot <= qty <= rule.max_lot or qty % rule.lot_step:
        raise ValueError('lots outside the proposed range or step')
    if tier not in ('ECN', 'VIP'):
        raise ValueError('tier must be ECN or VIP')
    units = qty * rule.units_per_lot
    notional = units * px
    effective_leverage = min(leverage, D(rule.max_leverage))
    fee = (notional * D('.0001') if rule.commission_mode == 'notional_pct'
           else qty * (D('7') if tier == 'ECN' else D('3.5')))
    return {
        'symbol': symbol, 'enabled': False, 'preview_only': True,
        'units': units, 'notional_usd': notional,
        'effective_leverage': effective_leverage,
        'margin_usd': notional / effective_leverage,
        'opening_commission_usd_unrounded': fee,
        'tick_value_usd': units * rule.price_increment,
        'spread_usd': None, 'overnight_financing_usd': None,
        'blockers': rule.blockers,
    }
