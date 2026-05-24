"""
0030 — Normalize BrokerSpreadConfig.symbol to canonical registry format.
Converts any slash-stripped variants (e.g. 'EURUSD') to the canonical form
('EUR/USD') used everywhere else in the system (Position, Trade, Redis, etc.).
"""

from django.db import migrations


def _normalize(symbol: str) -> str:
    """Inline normalization — avoids importing app models at migration time."""
    try:
        from market_data.symbol_specs import normalize_symbol
        return normalize_symbol(symbol)
    except Exception:
        return symbol


def normalize_spread_symbols(apps, schema_editor):
    BrokerSpreadConfig = apps.get_model("simulator", "BrokerSpreadConfig")
    # Use update() per row to avoid triggering model save() (historical model has no overrides)
    for cfg in BrokerSpreadConfig.objects.all():
        canonical = _normalize(cfg.symbol)
        if canonical != cfg.symbol:
            # Handle unique constraint: if canonical already exists, delete the dupe
            if BrokerSpreadConfig.objects.filter(symbol=canonical).exclude(pk=cfg.pk).exists():
                cfg.delete()
            else:
                BrokerSpreadConfig.objects.filter(pk=cfg.pk).update(symbol=canonical)


class Migration(migrations.Migration):

    dependencies = [
        ("simulator", "0029_broker_spread_config"),
    ]

    operations = [
        migrations.RunPython(normalize_spread_symbols, migrations.RunPython.noop),
    ]
