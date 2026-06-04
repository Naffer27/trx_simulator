"""
Idempotently seed the Internal Test Challenge 10K product used for
end-to-end validation of the external webhook flow.

Usage:
    python manage.py seed_test_challenge_product
    python manage.py seed_test_challenge_product --force-update

The product is identified by external_code=TEST_CHALLENGE_10K_2PHASE.
Running the command a second time is safe — it updates fields if --force-update
is passed, otherwise it prints the existing record and exits.
"""
from decimal import Decimal

from django.core.management.base import BaseCommand

EXTERNAL_CODE = "TEST_CHALLENGE_10K_2PHASE"

DEFAULTS = {
    "name":                   "Internal Test Challenge 10K",
    "tier":                   "10K",
    "price_usd":              Decimal("1.00"),
    "account_size":           Decimal("10000.00"),
    "p1_profit_target_pct":   Decimal("8.00"),
    "p1_max_drawdown_pct":    Decimal("10.00"),
    "p1_max_daily_loss_pct":  Decimal("5.00"),
    "p1_min_trading_days":    1,
    "p1_max_duration_days":   30,
    "p2_profit_target_pct":   Decimal("5.00"),
    "p2_max_drawdown_pct":    Decimal("10.00"),
    "p2_max_daily_loss_pct":  Decimal("5.00"),
    "p2_min_trading_days":    1,
    "p2_max_duration_days":   30,
    "profit_split_pct":       Decimal("80.00"),
    "max_lot_size":           Decimal("5.00"),
    "max_open_positions":     30,
    "is_active":              True,
}


class Command(BaseCommand):
    help = "Idempotently seed the Internal Test Challenge 10K for end-to-end webhook testing."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force-update",
            action="store_true",
            help="Update all fields even if the product already exists.",
        )

    def handle(self, *args, **options):
        from simulator.models import ChallengeProduct

        force = options["force_update"]
        existing = ChallengeProduct.objects.filter(external_code=EXTERNAL_CODE).first()

        if existing and not force:
            self.stdout.write(self.style.WARNING(
                f"Product already exists (pk={existing.pk}). "
                "Use --force-update to refresh fields."
            ))
            self._print_summary(existing)
            return

        if existing and force:
            for field, value in DEFAULTS.items():
                setattr(existing, field, value)
            existing.save()
            self.stdout.write(self.style.SUCCESS(
                f"Updated existing product pk={existing.pk}."
            ))
            self._print_summary(existing)
            return

        product = ChallengeProduct.objects.create(
            external_code=EXTERNAL_CODE,
            **DEFAULTS,
        )
        self.stdout.write(self.style.SUCCESS(
            f"Created test product pk={product.pk}."
        ))
        self._print_summary(product)

    def _print_summary(self, product):
        self.stdout.write("")
        self.stdout.write(f"  pk             : {product.pk}")
        self.stdout.write(f"  name           : {product.name}")
        self.stdout.write(f"  external_code  : {product.external_code}")
        self.stdout.write(f"  tier           : {product.tier}")
        self.stdout.write(f"  price_usd      : ${product.price_usd}")
        self.stdout.write(f"  account_size   : ${product.account_size}")
        self.stdout.write(f"  P1 profit tgt  : {product.p1_profit_target_pct}%")
        self.stdout.write(f"  P2 profit tgt  : {product.p2_profit_target_pct}%")
        self.stdout.write(f"  max drawdown   : {product.p1_max_drawdown_pct}%")
        self.stdout.write(f"  profit split   : {product.profit_split_pct}%")
        self.stdout.write(f"  is_active      : {product.is_active}")
        self.stdout.write("")
        self.stdout.write("Ready for webhook testing.")
        self.stdout.write(
            f"  Use --code {product.external_code} with simulate_external_challenge_purchase"
        )
