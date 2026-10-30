"""Read-only comparison. Never used by execution or to activate an instrument."""
from decimal import Decimal

from market_data.symbol_specs import get_spec, normalize_symbol

# Compare only equivalent fields. Commission and spread have different semantics
# in the reference catalogue and the account-specific execution path.
_FIELDS = (
    ('contract_size', 'Unidades por lote'),
    ('min_lot', 'Lote mínimo'),
    ('max_lot', 'Lote máximo'),
    ('lot_step', 'Paso de lotaje'),
    ('tick_size', 'Movimiento mínimo de precio'),
    ('pip_size', 'Tamaño del pip'),
    ('price_decimals', 'Decimales de precio'),
    ('max_leverage', 'Límite de apalancamiento del instrumento'),
)


def runtime_comparison(instrument):
    """Compare a saved/unsaved reference object without database/network access."""
    try:
        spec = get_spec(normalize_symbol(instrument.symbol))
    except KeyError:
        return None
    rows = []
    for name, label in _FIELDS:
        live = Decimal(str(getattr(spec, name)))
        reference = Decimal(str(getattr(instrument, name)))
        rows.append((label, format(live, 'f'), format(reference, 'f'), live == reference))
    for name, label in (('quote_currency', 'Moneda de cotización'),
                        ('margin_mode', 'Modo de margen')):
        live, reference = getattr(spec, name), getattr(instrument, name)
        rows.append((label, live, reference, live == reference))
    rows.append(('Habilitado en registro', str(spec.enabled),
                 str(instrument.trading_enabled), spec.enabled == instrument.trading_enabled))
    return {'symbol': spec.symbol, 'enabled': spec.enabled, 'rows': rows}
