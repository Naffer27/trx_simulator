"""Administrative spread units, not a quote or a second pricing engine."""
from decimal import Decimal
from market_data.symbol_specs import get_spec, normalize_symbol


def spread_units(config):
    try:
        spec = get_spec(normalize_symbol(config.symbol))
    except (KeyError, AttributeError):
        return None
    pip = Decimal(str(spec.pip_size))
    extra = Decimal(str(config.spread_pips)) * pip
    return {
        'symbol': spec.symbol, 'pip': pip, 'extra': extra,
        'per_side': extra / 2,
        'price_decimals': spec.price_decimals,
        'rounding_required': extra / 2 != (extra / 2).quantize(
            Decimal(1).scaleb(-spec.price_decimals)),
        'instrument_enabled': spec.enabled,
    }
