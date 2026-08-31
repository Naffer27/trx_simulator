# GOLDEN-STOPOUT-FIX-01 — Product Stop-Out Policy Drift.
#
# The commercial policy approved earlier (see 0072_fix03_stopout_default_70)
# was Margin Call=100% / Stop-Out=70%. 0072 changed AccountProduct.
# stopout_level's model FIELD DEFAULT to 70.00 — but field defaults only
# apply to new INSERTs made without an explicit value; they never rewrite
# rows that already exist. seed_account_products.py's own PRODUCTS source
# was updated to 70.00 too, but the command is deliberately idempotent
# (skips any product whose `code` already exists unless --force-update is
# passed) — and it was never re-run with --force-update. Net effect,
# verified live: all 4 MVP AccountProduct rows (demo-standard, demo-ecn,
# real-standard, real-ecn) still carry stopout_level=50.00 today, so every
# account created since 0072 — not just old ones — has been getting a
# 100/50 snapshot instead of the intended 100/70.
#
# This migration is the one-time, reproducible correction for the 4
# existing rows — the piece a manual `--force-update` run could never
# guarantee for fresh installs/staging without someone remembering to run
# it. Scope is deliberately the narrowest possible:
#   - only the `stopout_level` field (margin_call_level/leverage/spread/
#     commission/max_margin_* are never touched, verified field-by-field
#     identical to the seed source already — see the design lock audit);
#   - only the 4 known MVP `code`s (a future custom product is never
#     touched, whatever its own stopout_level is);
#   - only rows whose stopout_level is CURRENTLY exactly 50.00 (protects
#     against clobbering a value an operator may have already corrected
#     by hand to something other than 50 in the meantime).
#
# TradingAccount is never referenced here — every already-created
# account's stopout_level_snapshot is a frozen, immutable copy (see
# views.py's account-creation flow) and stays exactly as it is. Accounts
# with snapshot=None (a separate, already-documented, out-of-scope
# finding — see the design lock audit) are also untouched.

from decimal import Decimal

from django.db import migrations

_MVP_CODES = ["demo-standard", "demo-ecn", "real-standard", "real-ecn"]


def _set_stopout_70(apps, schema_editor):
    AccountProduct = apps.get_model("simulator", "AccountProduct")
    AccountProduct.objects.filter(
        code__in=_MVP_CODES,
        stopout_level=Decimal("50.00"),
    ).update(stopout_level=Decimal("70.00"))


def _revert_stopout_50(apps, schema_editor):
    AccountProduct = apps.get_model("simulator", "AccountProduct")
    AccountProduct.objects.filter(
        code__in=_MVP_CODES,
        stopout_level=Decimal("70.00"),
    ).update(stopout_level=Decimal("50.00"))


class Migration(migrations.Migration):

    dependencies = [
        ("simulator", "0073_alter_trade_lot_size"),
    ]

    operations = [
        migrations.RunPython(_set_stopout_70, _revert_stopout_50),
    ]
