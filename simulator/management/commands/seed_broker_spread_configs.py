"""
Idempotently seed BrokerSpreadConfig for the currently-enabled symbols.

Usage:
    python manage.py seed_broker_spread_configs
    python manage.py seed_broker_spread_configs --force-update

Seeds one row per symbol_specs.allowed_symbols() (currently 6: EUR/USD,
GBP/USD, USD/JPY, AUD/USD, BTCUSD, ETHUSD — never a hardcoded list, so
this command tracks the registry if it changes). base spread_pips is
derived from each SymbolSpec's own spread/pip_size (already the realistic
per-symbol value used for simulated pricing) — never a flat guess.

Every seeded row is intentionally inert:
  - is_dynamic=False        — SPREAD-05's dynamic engine stays opt-in.
  - spread_bounds_enabled=False — floor/ceiling stay opt-in (SPREAD-04
    correction).
  - manual_multiplier=1.000, manual_reason="", manual_expires_at=None.
Seeding never activates a symbol that symbol_specs itself has disabled —
only allowed_symbols() (enabled=True) are considered.

Identified by symbol. Safe to run multiple times: without --force-update,
an existing row is left completely untouched (never overwrites an admin's
manual edits, e.g. the pre-existing EUR/USD row from earlier blocks).
"""
from decimal import Decimal

from django.core.management.base import BaseCommand

from market_data.symbol_specs import allowed_symbols, get_spec
from simulator.models import BrokerSpreadConfig


class Command(BaseCommand):
    help = "Idempotently seed BrokerSpreadConfig for the currently-enabled symbols."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force-update",
            action="store_true",
            help="Overwrite spread_pips on rows that already exist. Never touches "
                 "is_dynamic/spread_bounds_enabled/min_spread/max_spread/manual_* "
                 "on an existing row, even with this flag — those are operator "
                 "decisions, not seed data.",
        )

    def handle(self, *args, **options):
        force = options["force_update"]
        created_count = updated_count = skipped_count = 0

        for symbol in sorted(allowed_symbols()):
            spec = get_spec(symbol)
            spread_pips = Decimal(str(round(spec.spread / spec.pip_size, 2)))
            existing = BrokerSpreadConfig.objects.filter(symbol=symbol).first()

            if existing and not force:
                self.stdout.write(self.style.WARNING(
                    f"  SKIP  {symbol!r} — already exists (pk={existing.pk}). Use --force-update to refresh."
                ))
                skipped_count += 1
                continue

            if existing:
                existing.spread_pips = spread_pips
                existing.save(update_fields=["spread_pips"])
                self.stdout.write(self.style.SUCCESS(
                    f"  UPDATE {symbol!r} (pk={existing.pk}) spread_pips={spread_pips}"
                ))
                updated_count += 1
            else:
                row = BrokerSpreadConfig.objects.create(
                    symbol=symbol,
                    spread_pips=spread_pips,
                    is_dynamic=False,
                    spread_bounds_enabled=False,
                )
                self.stdout.write(self.style.SUCCESS(
                    f"  CREATE {symbol!r} (pk={row.pk}) spread_pips={spread_pips}"
                ))
                created_count += 1

        self.stdout.write("")
        self.stdout.write(f"Done — created={created_count} updated={updated_count} skipped={skipped_count}")
