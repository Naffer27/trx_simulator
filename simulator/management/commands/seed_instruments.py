"""
Idempotently seed the Instrument catalog from the current intent of
market_data/symbol_specs.py — the runtime source of truth. This does NOT
change symbol_specs.py or wire Instrument into trading; it only populates the
catalog table for future admin management.

Usage:
    python manage.py seed_instruments
    python manage.py seed_instruments --force-update

Identified by symbol (unique). Safe to run multiple times.
"""
from decimal import Decimal

from django.core.management.base import BaseCommand

from simulator.models import Instrument

INSTRUMENTS = [
    # ── Forex — Majors (enabled) ────────────────────────────────────────────
    {
        "symbol": "EURUSD", "display_name": "EUR/USD", "asset_class": Instrument.ASSET_FOREX,
        "base_currency": "EUR", "quote_currency": "USD",
        "pip_size": Decimal("0.0001"), "tick_size": Decimal("0.00001"), "price_decimals": 5,
        "lot_step": Decimal("0.01"), "min_lot": Decimal("0.01"), "max_lot": Decimal("100.00"),
        "contract_size": Decimal("100000.0000"),
        "default_spread": Decimal("1.5000"), "spread_unit": Instrument.SPREAD_PIPS,
        "commission_per_lot": Decimal("0.00"), "commission_pct": Decimal("0.000000"),
        "max_leverage": 500, "margin_mode": Instrument.MARGIN_LEVERAGE, "pnl_mode": Instrument.PNL_STANDARD,
        "market_data_provider": Instrument.PROVIDER_FINNHUB, "provider_symbol": "FX:EURUSD",
        "trading_enabled": True, "session": "24/5",
    },
    {
        "symbol": "GBPUSD", "display_name": "GBP/USD", "asset_class": Instrument.ASSET_FOREX,
        "base_currency": "GBP", "quote_currency": "USD",
        "pip_size": Decimal("0.0001"), "tick_size": Decimal("0.00001"), "price_decimals": 5,
        "lot_step": Decimal("0.01"), "min_lot": Decimal("0.01"), "max_lot": Decimal("100.00"),
        "contract_size": Decimal("100000.0000"),
        "default_spread": Decimal("1.8000"), "spread_unit": Instrument.SPREAD_PIPS,
        "commission_per_lot": Decimal("0.00"), "commission_pct": Decimal("0.000000"),
        "max_leverage": 500, "margin_mode": Instrument.MARGIN_LEVERAGE, "pnl_mode": Instrument.PNL_STANDARD,
        "market_data_provider": Instrument.PROVIDER_FINNHUB, "provider_symbol": "FX:GBPUSD",
        "trading_enabled": True, "session": "24/5",
    },
    {
        "symbol": "USDJPY", "display_name": "USD/JPY", "asset_class": Instrument.ASSET_FOREX,
        "base_currency": "USD", "quote_currency": "JPY",
        "pip_size": Decimal("0.01"), "tick_size": Decimal("0.001"), "price_decimals": 3,
        "lot_step": Decimal("0.01"), "min_lot": Decimal("0.01"), "max_lot": Decimal("100.00"),
        "contract_size": Decimal("100000.0000"),
        "default_spread": Decimal("1.8000"), "spread_unit": Instrument.SPREAD_PIPS,
        "commission_per_lot": Decimal("0.00"), "commission_pct": Decimal("0.000000"),
        "max_leverage": 500, "margin_mode": Instrument.MARGIN_LEVERAGE, "pnl_mode": Instrument.PNL_INVERSE,
        "market_data_provider": Instrument.PROVIDER_FINNHUB, "provider_symbol": "FX:USDJPY",
        "trading_enabled": True, "session": "24/5",
    },
    {
        "symbol": "AUDUSD", "display_name": "AUD/USD", "asset_class": Instrument.ASSET_FOREX,
        "base_currency": "AUD", "quote_currency": "USD",
        "pip_size": Decimal("0.0001"), "tick_size": Decimal("0.00001"), "price_decimals": 5,
        "lot_step": Decimal("0.01"), "min_lot": Decimal("0.01"), "max_lot": Decimal("100.00"),
        "contract_size": Decimal("100000.0000"),
        "default_spread": Decimal("1.7000"), "spread_unit": Instrument.SPREAD_PIPS,
        "commission_per_lot": Decimal("0.00"), "commission_pct": Decimal("0.000000"),
        "max_leverage": 500, "margin_mode": Instrument.MARGIN_LEVERAGE, "pnl_mode": Instrument.PNL_STANDARD,
        "market_data_provider": Instrument.PROVIDER_FINNHUB, "provider_symbol": "FX:AUDUSD",
        "trading_enabled": True, "session": "24/5",
    },
    # ── Crypto (enabled) ─────────────────────────────────────────────────────
    {
        "symbol": "BTCUSD", "display_name": "BTC/USD", "asset_class": Instrument.ASSET_CRYPTO,
        "base_currency": "BTC", "quote_currency": "USD",
        "pip_size": Decimal("1.00"), "tick_size": Decimal("0.01"), "price_decimals": 2,
        "lot_step": Decimal("0.001"), "min_lot": Decimal("0.001"), "max_lot": Decimal("10.00"),
        "contract_size": Decimal("1.0000"),
        "default_spread": Decimal("15.0000"), "spread_unit": Instrument.SPREAD_PIPS,
        "commission_per_lot": Decimal("0.00"), "commission_pct": Decimal("0.000100"),
        "max_leverage": 20, "margin_mode": Instrument.MARGIN_LEVERAGE, "pnl_mode": Instrument.PNL_STANDARD,
        "market_data_provider": Instrument.PROVIDER_BINANCE, "provider_symbol": "BTCUSDT",
        "trading_enabled": True, "session": "24/7",
    },
    {
        "symbol": "ETHUSD", "display_name": "ETH/USD", "asset_class": Instrument.ASSET_CRYPTO,
        "base_currency": "ETH", "quote_currency": "USD",
        "pip_size": Decimal("1.00"), "tick_size": Decimal("0.01"), "price_decimals": 2,
        "lot_step": Decimal("0.01"), "min_lot": Decimal("0.01"), "max_lot": Decimal("100.00"),
        "contract_size": Decimal("1.0000"),
        "default_spread": Decimal("3.0000"), "spread_unit": Instrument.SPREAD_PIPS,
        "commission_per_lot": Decimal("0.00"), "commission_pct": Decimal("0.000100"),
        "max_leverage": 20, "margin_mode": Instrument.MARGIN_LEVERAGE, "pnl_mode": Instrument.PNL_STANDARD,
        "market_data_provider": Instrument.PROVIDER_BINANCE, "provider_symbol": "ETHUSDT",
        "trading_enabled": True, "session": "24/7",
    },
    # ── Forex — Additional Pairs (disabled) ─────────────────────────────────
    {
        "symbol": "USDCAD", "display_name": "USD/CAD", "asset_class": Instrument.ASSET_FOREX,
        "base_currency": "USD", "quote_currency": "CAD",
        "pip_size": Decimal("0.0001"), "tick_size": Decimal("0.00001"), "price_decimals": 5,
        "lot_step": Decimal("0.01"), "min_lot": Decimal("0.01"), "max_lot": Decimal("100.00"),
        "contract_size": Decimal("100000.0000"),
        "default_spread": Decimal("2.0000"), "spread_unit": Instrument.SPREAD_PIPS,
        "commission_per_lot": Decimal("0.00"), "commission_pct": Decimal("0.000000"),
        "max_leverage": 500, "margin_mode": Instrument.MARGIN_LEVERAGE, "pnl_mode": Instrument.PNL_INVERSE,
        "market_data_provider": Instrument.PROVIDER_FINNHUB, "provider_symbol": "FX:USDCAD",
        "trading_enabled": False, "session": "24/5",
    },
    {
        "symbol": "USDCHF", "display_name": "USD/CHF", "asset_class": Instrument.ASSET_FOREX,
        "base_currency": "USD", "quote_currency": "CHF",
        "pip_size": Decimal("0.0001"), "tick_size": Decimal("0.00001"), "price_decimals": 5,
        "lot_step": Decimal("0.01"), "min_lot": Decimal("0.01"), "max_lot": Decimal("100.00"),
        "contract_size": Decimal("100000.0000"),
        "default_spread": Decimal("1.8000"), "spread_unit": Instrument.SPREAD_PIPS,
        "commission_per_lot": Decimal("0.00"), "commission_pct": Decimal("0.000000"),
        "max_leverage": 500, "margin_mode": Instrument.MARGIN_LEVERAGE, "pnl_mode": Instrument.PNL_INVERSE,
        "market_data_provider": Instrument.PROVIDER_FINNHUB, "provider_symbol": "FX:USDCHF",
        "trading_enabled": False, "session": "24/5",
    },
    {
        "symbol": "NZDUSD", "display_name": "NZD/USD", "asset_class": Instrument.ASSET_FOREX,
        "base_currency": "NZD", "quote_currency": "USD",
        "pip_size": Decimal("0.0001"), "tick_size": Decimal("0.00001"), "price_decimals": 5,
        "lot_step": Decimal("0.01"), "min_lot": Decimal("0.01"), "max_lot": Decimal("100.00"),
        "contract_size": Decimal("100000.0000"),
        "default_spread": Decimal("2.0000"), "spread_unit": Instrument.SPREAD_PIPS,
        "commission_per_lot": Decimal("0.00"), "commission_pct": Decimal("0.000000"),
        "max_leverage": 500, "margin_mode": Instrument.MARGIN_LEVERAGE, "pnl_mode": Instrument.PNL_STANDARD,
        "market_data_provider": Instrument.PROVIDER_FINNHUB, "provider_symbol": "FX:NZDUSD",
        "trading_enabled": False, "session": "24/5",
    },
    # ── Crypto (disabled) ────────────────────────────────────────────────────
    {
        "symbol": "SOLUSD", "display_name": "SOL/USD", "asset_class": Instrument.ASSET_CRYPTO,
        "base_currency": "SOL", "quote_currency": "USD",
        "pip_size": Decimal("0.01"), "tick_size": Decimal("0.01"), "price_decimals": 2,
        "lot_step": Decimal("0.1"), "min_lot": Decimal("0.1"), "max_lot": Decimal("1000.00"),
        "contract_size": Decimal("1.0000"),
        "default_spread": Decimal("20.0000"), "spread_unit": Instrument.SPREAD_PIPS,
        "commission_per_lot": Decimal("0.00"), "commission_pct": Decimal("0.000100"),
        "max_leverage": 10, "margin_mode": Instrument.MARGIN_LEVERAGE, "pnl_mode": Instrument.PNL_STANDARD,
        "market_data_provider": Instrument.PROVIDER_SIM, "provider_symbol": "",
        "trading_enabled": False, "session": "24/7",
    },
    # ── Metals (disabled) ────────────────────────────────────────────────────
    {
        "symbol": "XAUUSD", "display_name": "XAU/USD", "asset_class": Instrument.ASSET_METAL,
        "base_currency": "XAU", "quote_currency": "USD",
        "pip_size": Decimal("0.01"), "tick_size": Decimal("0.01"), "price_decimals": 2,
        "lot_step": Decimal("0.01"), "min_lot": Decimal("0.01"), "max_lot": Decimal("50.00"),
        "contract_size": Decimal("100.0000"),
        "default_spread": Decimal("30.0000"), "spread_unit": Instrument.SPREAD_PIPS,
        "commission_per_lot": Decimal("0.00"), "commission_pct": Decimal("0.000000"),
        "max_leverage": 100, "margin_mode": Instrument.MARGIN_LEVERAGE, "pnl_mode": Instrument.PNL_STANDARD,
        "market_data_provider": Instrument.PROVIDER_FINNHUB, "provider_symbol": "OANDA:XAU_USD",
        "trading_enabled": False, "session": "24/5",
    },
    {
        "symbol": "XAGUSD", "display_name": "XAG/USD", "asset_class": Instrument.ASSET_METAL,
        "base_currency": "XAG", "quote_currency": "USD",
        "pip_size": Decimal("0.001"), "tick_size": Decimal("0.001"), "price_decimals": 3,
        "lot_step": Decimal("0.01"), "min_lot": Decimal("0.01"), "max_lot": Decimal("50.00"),
        "contract_size": Decimal("5000.0000"),
        "default_spread": Decimal("30.0000"), "spread_unit": Instrument.SPREAD_PIPS,
        "commission_per_lot": Decimal("0.00"), "commission_pct": Decimal("0.000000"),
        "max_leverage": 100, "margin_mode": Instrument.MARGIN_LEVERAGE, "pnl_mode": Instrument.PNL_STANDARD,
        "market_data_provider": Instrument.PROVIDER_SIM, "provider_symbol": "",
        "trading_enabled": False, "session": "24/5",
    },
    # ── Indices (disabled) ───────────────────────────────────────────────────
    {
        "symbol": "US30", "display_name": "US30", "asset_class": Instrument.ASSET_INDEX,
        "base_currency": "US30", "quote_currency": "USD",
        "pip_size": Decimal("1.00"), "tick_size": Decimal("1.00"), "price_decimals": 0,
        "lot_step": Decimal("0.1"), "min_lot": Decimal("0.1"), "max_lot": Decimal("100.00"),
        "contract_size": Decimal("1.0000"),
        "default_spread": Decimal("3.0000"), "spread_unit": Instrument.SPREAD_POINTS,
        "commission_per_lot": Decimal("0.00"), "commission_pct": Decimal("0.000000"),
        "max_leverage": 20, "margin_mode": Instrument.MARGIN_LEVERAGE, "pnl_mode": Instrument.PNL_STANDARD,
        "market_data_provider": Instrument.PROVIDER_SIM, "provider_symbol": "",
        "trading_enabled": False, "session": "24/5",
    },
    {
        "symbol": "US500", "display_name": "US500", "asset_class": Instrument.ASSET_INDEX,
        "base_currency": "US500", "quote_currency": "USD",
        "pip_size": Decimal("0.25"), "tick_size": Decimal("0.25"), "price_decimals": 2,
        "lot_step": Decimal("0.1"), "min_lot": Decimal("0.1"), "max_lot": Decimal("100.00"),
        "contract_size": Decimal("10.0000"),
        "default_spread": Decimal("2.0000"), "spread_unit": Instrument.SPREAD_POINTS,
        "commission_per_lot": Decimal("0.00"), "commission_pct": Decimal("0.000000"),
        "max_leverage": 20, "margin_mode": Instrument.MARGIN_LEVERAGE, "pnl_mode": Instrument.PNL_STANDARD,
        "market_data_provider": Instrument.PROVIDER_SIM, "provider_symbol": "",
        "trading_enabled": False, "session": "24/5",
    },
    {
        "symbol": "NAS100", "display_name": "NAS100", "asset_class": Instrument.ASSET_INDEX,
        "base_currency": "NAS100", "quote_currency": "USD",
        "pip_size": Decimal("0.25"), "tick_size": Decimal("0.25"), "price_decimals": 2,
        "lot_step": Decimal("0.1"), "min_lot": Decimal("0.1"), "max_lot": Decimal("100.00"),
        "contract_size": Decimal("1.0000"),
        "default_spread": Decimal("6.0000"), "spread_unit": Instrument.SPREAD_POINTS,
        "commission_per_lot": Decimal("0.00"), "commission_pct": Decimal("0.000000"),
        "max_leverage": 20, "margin_mode": Instrument.MARGIN_LEVERAGE, "pnl_mode": Instrument.PNL_STANDARD,
        "market_data_provider": Instrument.PROVIDER_SIM, "provider_symbol": "",
        "trading_enabled": False, "session": "24/5",
    },
]


class Command(BaseCommand):
    help = "Idempotently seed the Instrument catalog from symbol_specs.py intent."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force-update",
            action="store_true",
            help="Update all fields on existing instruments (default: skip existing).",
        )

    def handle(self, *args, **options):
        force = options["force_update"]
        created_count = updated_count = skipped_count = 0

        for spec in INSTRUMENTS:
            symbol = spec["symbol"]
            existing = Instrument.objects.filter(symbol=symbol).first()

            if existing and not force:
                self.stdout.write(self.style.WARNING(
                    f"  SKIP   {symbol!r} — already exists (pk={existing.pk}). Use --force-update to refresh."
                ))
                skipped_count += 1
                continue

            if existing:
                for field, value in spec.items():
                    setattr(existing, field, value)
                existing.save()
                self.stdout.write(self.style.SUCCESS(f"  UPDATE {symbol!r} (pk={existing.pk})"))
                updated_count += 1
            else:
                instrument = Instrument.objects.create(**spec)
                self.stdout.write(self.style.SUCCESS(f"  CREATE {symbol!r} (pk={instrument.pk})"))
                created_count += 1

        self.stdout.write("")
        self.stdout.write(f"Done — created={created_count} updated={updated_count} skipped={skipped_count}")
        self._print_table()

    def _print_table(self):
        self.stdout.write("")
        self.stdout.write(
            f"  {'SYMBOL':<8} {'CLASS':<7} {'PROVIDER':<8} {'LEV':>5} {'SPREAD':>8} {'ENABLED':>8}"
        )
        self.stdout.write("  " + "─" * 55)
        for i in Instrument.objects.order_by("asset_class", "symbol"):
            self.stdout.write(
                f"  {i.symbol:<8} {i.asset_class:<7} {i.market_data_provider:<8} "
                f"{i.max_leverage:>5} {float(i.default_spread):>8.4f} "
                f"{'✓' if i.trading_enabled else '✗':>8}"
            )
