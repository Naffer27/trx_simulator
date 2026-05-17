from decimal import Decimal
from django.db import migrations, models


def populate_initial_balance(apps, schema_editor):
    """Back-fill initial_balance for existing accounts from tier defaults."""
    TradingAccount = apps.get_model("simulator", "TradingAccount")
    _TIER_INITIAL = {"10K": Decimal("10000"), "50K": Decimal("50000"), "100K": Decimal("100000")}
    for acc in TradingAccount.objects.filter(initial_balance__isnull=True):
        acc.initial_balance = _TIER_INITIAL.get(acc.tier or "10K", Decimal("10000"))
        acc.save(update_fields=["initial_balance"])


class Migration(migrations.Migration):

    dependencies = [
        ("simulator", "0017_tradingaccount_status_choices"),
    ]

    operations = [
        # 1. Add account_type (all existing rows → CHALLENGE)
        migrations.AddField(
            model_name="tradingaccount",
            name="account_type",
            field=models.CharField(
                choices=[
                    ("CHALLENGE", "Challenge"),
                    ("FUNDED",    "Funded"),
                    ("RETAIL",    "Retail"),
                ],
                default="CHALLENGE",
                max_length=10,
            ),
        ),

        # 2. Add initial_balance (nullable — back-filled by data migration below)
        migrations.AddField(
            model_name="tradingaccount",
            name="initial_balance",
            field=models.DecimalField(
                blank=True, null=True, max_digits=12, decimal_places=2
            ),
        ),

        # 3. Make tier nullable (RETAIL accounts have no tier)
        migrations.AlterField(
            model_name="tradingaccount",
            name="tier",
            field=models.CharField(
                blank=True,
                choices=[
                    ("10K",  "Cuenta 10 000"),
                    ("50K",  "Cuenta 50 000"),
                    ("100K", "Cuenta 100 000"),
                ],
                max_length=4,
                null=True,
                help_text="Elige el plan de fondeo (Challenge/Funded only)",
            ),
        ),

        # 4. Make profit_target nullable (RETAIL accounts have no fixed target)
        migrations.AlterField(
            model_name="tradingaccount",
            name="profit_target",
            field=models.DecimalField(
                blank=True, null=True, max_digits=12, decimal_places=2
            ),
        ),

        # 5. Back-fill initial_balance for existing accounts
        migrations.RunPython(populate_initial_balance, migrations.RunPython.noop),
    ]
