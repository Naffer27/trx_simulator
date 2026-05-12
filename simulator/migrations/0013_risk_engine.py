from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("simulator", "0012_trade_opened_at_default"),
    ]

    operations = [
        # peak_balance on TradingAccount
        migrations.AddField(
            model_name="tradingaccount",
            name="peak_balance",
            field=models.DecimalField(decimal_places=2, default=10000.0, max_digits=12),
        ),

        # RiskRule
        migrations.CreateModel(
            name="RiskRule",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("max_daily_loss_pct", models.DecimalField(decimal_places=2, default=5.0, max_digits=5)),
                ("max_drawdown_pct",   models.DecimalField(decimal_places=2, default=10.0, max_digits=5)),
                ("max_lot_size",       models.DecimalField(decimal_places=2, default=5.0, max_digits=8)),
                ("max_open_positions", models.PositiveIntegerField(default=10)),
                ("max_exposure_usd",   models.DecimalField(decimal_places=2, default=5000.0, max_digits=12)),
                ("consistency_min_trades", models.PositiveIntegerField(default=5)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("account", models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="risk_rule",
                    to="simulator.tradingaccount",
                )),
            ],
        ),

        # DrawdownSnapshot
        migrations.CreateModel(
            name="DrawdownSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("date",             models.DateField()),
                ("balance_start",    models.DecimalField(decimal_places=2, max_digits=12)),
                ("balance_end",      models.DecimalField(decimal_places=2, max_digits=12)),
                ("daily_pnl",        models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ("daily_pnl_pct",    models.DecimalField(decimal_places=2, default=0, max_digits=7)),
                ("peak_balance",     models.DecimalField(decimal_places=2, max_digits=12)),
                ("drawdown_from_peak", models.DecimalField(decimal_places=2, default=0, max_digits=7)),
                ("account", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="dd_snapshots",
                    to="simulator.tradingaccount",
                )),
            ],
            options={"ordering": ["-date"]},
        ),
        migrations.AlterUniqueTogether(
            name="drawdownsnapshot",
            unique_together={("account", "date")},
        ),
        migrations.AddIndex(
            model_name="drawdownsnapshot",
            index=models.Index(fields=["account", "date"], name="dd_snap_acc_date_idx"),
        ),

        # TradingViolation
        migrations.CreateModel(
            name="TradingViolation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("violation_type",     models.CharField(max_length=24, choices=[
                    ("MAX_DRAWDOWN", "Max Drawdown"),
                    ("MAX_DAILY_LOSS", "Max Daily Loss"),
                    ("MAX_LOT_SIZE", "Max Lot Size"),
                    ("MAX_EXPOSURE", "Max Exposure"),
                    ("RATE_LIMITED", "Rate Limited"),
                    ("MARTINGALE_PATTERN", "Martingale Pattern"),
                ])),
                ("value_at_violation", models.DecimalField(decimal_places=4, max_digits=12)),
                ("limit_value",        models.DecimalField(decimal_places=4, max_digits=12)),
                ("meta",               models.JSONField(blank=True, null=True)),
                ("created_at",         models.DateTimeField(auto_now_add=True)),
                ("account", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="violations",
                    to="simulator.tradingaccount",
                )),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.AddIndex(
            model_name="tradingviolation",
            index=models.Index(fields=["account", "created_at"], name="violation_acc_ts_idx"),
        ),

        # TraderScore
        migrations.CreateModel(
            name="TraderScore",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("trader_class",      models.CharField(max_length=12, default="NORMAL", choices=[
                    ("NORMAL", "Normal"), ("RISKY", "Risky"), ("MARTINGALE", "Martingale"),
                    ("TOXIC", "Toxic"), ("CONSISTENT", "Consistent"), ("ELITE", "Elite"),
                ])),
                ("win_rate",          models.DecimalField(decimal_places=2, default=0, max_digits=5)),
                ("avg_lot_size",      models.DecimalField(decimal_places=4, default=0, max_digits=10)),
                ("martingale_rate",   models.DecimalField(decimal_places=3, default=0, max_digits=5)),
                ("profit_factor",     models.DecimalField(decimal_places=2, default=0, max_digits=8)),
                ("consistency_score", models.DecimalField(decimal_places=2, default=0, max_digits=5)),
                ("last_evaluated",    models.DateTimeField(blank=True, null=True)),
                ("account", models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="trader_score",
                    to="simulator.tradingaccount",
                )),
            ],
        ),
    ]
