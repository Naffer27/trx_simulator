from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("simulator", "0040_support_ticket"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="FundedPayoutRequest",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("cycle_profit",             models.DecimalField(decimal_places=2, max_digits=12)),
                ("trader_cut",               models.DecimalField(decimal_places=2, max_digits=12)),
                ("broker_cut",               models.DecimalField(decimal_places=2, max_digits=12)),
                ("profit_split_pct",         models.DecimalField(decimal_places=2, max_digits=5)),
                ("balance_snapshot",         models.DecimalField(decimal_places=2, max_digits=12)),
                ("initial_balance_snapshot", models.DecimalField(decimal_places=2, max_digits=12)),
                ("funded_type",              models.CharField(max_length=16)),
                ("crypto_currency",          models.CharField(blank=True, default="", max_length=20)),
                ("wallet_address",           models.CharField(blank=True, default="", max_length=200)),
                ("cycle_reset_at",           models.DateTimeField(blank=True, null=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending",    "Pending"),
                            ("approved",   "Approved"),
                            ("processing", "Processing"),
                            ("completed",  "Completed"),
                            ("rejected",   "Rejected"),
                            ("failed",     "Failed"),
                            ("cancelled",  "Cancelled"),
                        ],
                        db_index=True,
                        default="pending",
                        max_length=12,
                    ),
                ),
                ("admin_note",  models.TextField(blank=True, default="")),
                ("reviewed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at",  models.DateTimeField(auto_now_add=True)),
                ("updated_at",  models.DateTimeField(auto_now=True)),
                (
                    "enrollment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="payout_requests",
                        to="simulator.challengeenrollment",
                    ),
                ),
                (
                    "funded_account",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="payout_requests",
                        to="simulator.tradingaccount",
                    ),
                ),
                (
                    "funded_config",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="payout_requests",
                        to="simulator.fundedconfig",
                    ),
                ),
                (
                    "ledger_entry",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="funded_payout_request",
                        to="simulator.ledgerentry",
                    ),
                ),
                (
                    "reviewed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="reviewed_funded_payouts",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="funded_payout_requests",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "wallet_credit_tx",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="funded_payout_sim",
                        to="simulator.wallettransaction",
                    ),
                ),
                (
                    "withdrawal_request",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="funded_payout_internal",
                        to="simulator.withdrawalrequest",
                    ),
                ),
            ],
            options={
                "verbose_name":        "Funded Payout Request",
                "verbose_name_plural": "Funded Payout Requests",
                "ordering":            ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="fundedpayoutrequest",
            index=models.Index(fields=["user", "status"], name="fpr_user_status_idx"),
        ),
        migrations.AddIndex(
            model_name="fundedpayoutrequest",
            index=models.Index(fields=["enrollment"], name="fpr_enrollment_idx"),
        ),
    ]
