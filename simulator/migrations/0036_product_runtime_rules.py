# simulator/migrations/0036_product_runtime_rules.py
# Phase 6B — Product Runtime Rules
#
# TradingAccount: account_product FK (nullable) + 9 snapshot fields (all nullable).
# AccountProduct: allowed_symbols, max_lot_size, margin_call_level, stopout_level.
#
# All fields are nullable or have safe defaults — zero impact on existing rows.

from decimal import Decimal

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("simulator", "0035_account_product_catalog_fields"),
    ]

    operations = [
        # ── AccountProduct: risk parameters ───────────────────────────────────
        migrations.AddField(
            model_name="accountproduct",
            name="allowed_symbols",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="accountproduct",
            name="max_lot_size",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True),
        ),
        migrations.AddField(
            model_name="accountproduct",
            name="margin_call_level",
            field=models.DecimalField(decimal_places=2, default=Decimal("100.00"), max_digits=5),
        ),
        migrations.AddField(
            model_name="accountproduct",
            name="stopout_level",
            field=models.DecimalField(decimal_places=2, default=Decimal("50.00"), max_digits=5),
        ),

        # ── TradingAccount: FK ─────────────────────────────────────────────────
        migrations.AddField(
            model_name="tradingaccount",
            name="account_product",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="trading_accounts",
                to="simulator.accountproduct",
            ),
        ),

        # ── TradingAccount: snapshot fields ───────────────────────────────────
        migrations.AddField(
            model_name="tradingaccount",
            name="product_code_snapshot",
            field=models.CharField(blank=True, max_length=32, null=True),
        ),
        migrations.AddField(
            model_name="tradingaccount",
            name="product_name_snapshot",
            field=models.CharField(blank=True, max_length=64, null=True),
        ),
        migrations.AddField(
            model_name="tradingaccount",
            name="leverage_snapshot",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="tradingaccount",
            name="spread_pips_snapshot",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=6, null=True),
        ),
        migrations.AddField(
            model_name="tradingaccount",
            name="commission_per_lot_snapshot",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=8, null=True),
        ),
        migrations.AddField(
            model_name="tradingaccount",
            name="allowed_symbols_snapshot",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="tradingaccount",
            name="max_lot_size_snapshot",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True),
        ),
        migrations.AddField(
            model_name="tradingaccount",
            name="margin_call_level_snapshot",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True),
        ),
        migrations.AddField(
            model_name="tradingaccount",
            name="stopout_level_snapshot",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True),
        ),
    ]
