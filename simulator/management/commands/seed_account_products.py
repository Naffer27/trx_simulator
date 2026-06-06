"""
Idempotently seed the 4 MVP AccountProducts for Money Broker.

Usage:
    python manage.py seed_account_products
    python manage.py seed_account_products --force-update

Products seeded:
    demo-standard  Demo Standard — Money Broker  (family=DEMO, account_type=DEMO)
    demo-ecn       Demo ECN — Money Broker        (family=DEMO, account_type=DEMO)
    real-standard  Real Standard — Money Broker   (family=REAL, account_type=STANDARD)
    real-ecn       Real ECN — Money Broker        (family=REAL, account_type=ECN)

Identified by code (unique slug). Safe to run multiple times.
"""
from decimal import Decimal

from django.core.management.base import BaseCommand

from simulator.models import AccountProduct

PRODUCTS = [
    {
        "code":                "demo-standard",
        "name":                "Demo Standard",
        "product_type":        AccountProduct.TYPE_DEMO,
        "family":              AccountProduct.FAMILY_DEMO,
        "platform_label":      "Money Broker",
        "description":         "Cuenta de práctica con balance virtual. Sin riesgo real. Spreads estilo Standard.",
        "min_deposit":         Decimal("0.00"),
        "default_balance":     Decimal("10000.00"),
        "max_leverage":        100,
        "typical_spread_pips": Decimal("1.20"),
        "commission_per_lot":  Decimal("0.00"),
        "allowed_symbols":     None,
        "max_lot_size":        None,
        "margin_call_level":   Decimal("100.00"),
        "stopout_level":       Decimal("50.00"),
        "is_popular":          False,
        "sort_order":          10,
        "is_active":           True,
    },
    {
        "code":                "demo-ecn",
        "name":                "Demo ECN",
        "product_type":        AccountProduct.TYPE_DEMO,
        "family":              AccountProduct.FAMILY_DEMO,
        "platform_label":      "Money Broker",
        "description":         "Cuenta de práctica con spreads ECN ultra-bajos y comisión por lote. Sin riesgo real.",
        "min_deposit":         Decimal("0.00"),
        "default_balance":     Decimal("10000.00"),
        "max_leverage":        100,
        "typical_spread_pips": Decimal("0.00"),
        "commission_per_lot":  Decimal("7.00"),
        "allowed_symbols":     None,
        "max_lot_size":        None,
        "margin_call_level":   Decimal("100.00"),
        "stopout_level":       Decimal("50.00"),
        "is_popular":          False,
        "sort_order":          20,
        "is_active":           True,
    },
    {
        "code":                "real-standard",
        "name":                "Real Standard",
        "product_type":        AccountProduct.TYPE_STANDARD,
        "family":              AccountProduct.FAMILY_REAL,
        "platform_label":      "Money Broker",
        "description":         "Cuenta real con spreads Standard. Sin comisión por lote. Depósito mínimo $10.",
        "min_deposit":         Decimal("10.00"),
        "default_balance":     Decimal("0.00"),
        "max_leverage":        100,
        "typical_spread_pips": Decimal("1.20"),
        "commission_per_lot":  Decimal("0.00"),
        "allowed_symbols":     None,
        "max_lot_size":        None,
        "margin_call_level":   Decimal("100.00"),
        "stopout_level":       Decimal("50.00"),
        "is_popular":          True,
        "sort_order":          10,
        "is_active":           True,
    },
    {
        "code":                "real-ecn",
        "name":                "Real ECN",
        "product_type":        AccountProduct.TYPE_ECN,
        "family":              AccountProduct.FAMILY_REAL,
        "platform_label":      "Money Broker",
        "description":         "Cuenta real ECN con spreads raw desde 0.0 pips. Comisión $7 por lote. Depósito mínimo $100.",
        "min_deposit":         Decimal("100.00"),
        "default_balance":     Decimal("0.00"),
        "max_leverage":        100,
        "typical_spread_pips": Decimal("0.00"),
        "commission_per_lot":  Decimal("7.00"),
        "allowed_symbols":     None,
        "max_lot_size":        None,
        "margin_call_level":   Decimal("100.00"),
        "stopout_level":       Decimal("50.00"),
        "is_popular":          False,
        "sort_order":          20,
        "is_active":           True,
    },
]


class Command(BaseCommand):
    help = "Idempotently seed the 4 MVP AccountProducts for Money Broker."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force-update",
            action="store_true",
            help="Update all fields even if the product already exists.",
        )

    def handle(self, *args, **options):
        force = options["force_update"]
        created_count = updated_count = skipped_count = 0

        for spec in PRODUCTS:
            code = spec["code"]
            existing = AccountProduct.objects.filter(code=code).first()

            if existing and not force:
                self.stdout.write(self.style.WARNING(
                    f"  SKIP  {code!r} — already exists (pk={existing.pk}). Use --force-update to refresh."
                ))
                skipped_count += 1
                continue

            if existing:
                for field, value in spec.items():
                    setattr(existing, field, value)
                existing.save()
                self.stdout.write(self.style.SUCCESS(f"  UPDATE {code!r} (pk={existing.pk})"))
                updated_count += 1
            else:
                product = AccountProduct.objects.create(**spec)
                self.stdout.write(self.style.SUCCESS(f"  CREATE {code!r} (pk={product.pk})"))
                created_count += 1

        self.stdout.write("")
        self.stdout.write(f"Done — created={created_count} updated={updated_count} skipped={skipped_count}")
        self._print_table()

    def _print_table(self):
        self.stdout.write("")
        self.stdout.write(
            f"  {'CODE':<20} {'FAMILY':<6} {'TYPE':<10} {'MIN $':>7} {'DEFAULT $':>10} {'LEV':>5} "
            f"{'SPREAD':>7} {'COMM/LOT':>9} {'MC%':>6} {'SO%':>6} {'POP':>4} {'ACTIVE':>6}"
        )
        self.stdout.write("  " + "─" * 100)
        for p in AccountProduct.objects.order_by("family", "sort_order"):
            self.stdout.write(
                f"  {p.code or '—':<20} {p.family:<6} {p.product_type:<10} "
                f"{float(p.min_deposit):>7.2f} {float(p.default_balance):>10.2f} "
                f"{p.max_leverage:>5} {float(p.typical_spread_pips):>7.2f} "
                f"{float(p.commission_per_lot):>9.2f} "
                f"{float(p.margin_call_level):>6.1f} {float(p.stopout_level):>6.1f} "
                f"{'✓' if p.is_popular else '':>4} {'✓' if p.is_active else '✗':>6}"
            )
