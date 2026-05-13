from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("simulator", "0014_trader_intelligence"),
    ]

    operations = [
        # ── BrokerSnapshot ──
        migrations.CreateModel(
            name="BrokerSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at",               models.DateTimeField(auto_now_add=True, db_index=True)),
                ("total_accounts",           models.PositiveIntegerField(default=0)),
                ("total_open_positions",     models.PositiveIntegerField(default=0)),
                ("total_long_usd",           models.DecimalField(decimal_places=2, default=0, max_digits=18)),
                ("total_short_usd",          models.DecimalField(decimal_places=2, default=0, max_digits=18)),
                ("net_exposure_usd",         models.DecimalField(decimal_places=2, default=0, max_digits=18)),
                ("total_unrealized_pnl",     models.DecimalField(decimal_places=2, default=0, max_digits=18)),
                ("total_realized_pnl_today", models.DecimalField(decimal_places=2, default=0, max_digits=18)),
                ("internal_exposure_usd",    models.DecimalField(decimal_places=2, default=0, max_digits=18)),
                ("review_exposure_usd",      models.DecimalField(decimal_places=2, default=0, max_digits=18)),
                ("hedge_candidate_usd",      models.DecimalField(decimal_places=2, default=0, max_digits=18)),
                ("broker_pnl_unrealized",    models.DecimalField(decimal_places=2, default=0, max_digits=18)),
                ("broker_pnl_today",         models.DecimalField(decimal_places=2, default=0, max_digits=18)),
                ("risk_flags",               models.JSONField(default=list)),
            ],
            options={"ordering": ["-created_at"], "verbose_name": "Broker Snapshot", "verbose_name_plural": "Broker Snapshots"},
        ),

        # ── SymbolExposure ──
        migrations.CreateModel(
            name="SymbolExposure",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("symbol",            models.CharField(max_length=12)),
                ("long_qty",          models.DecimalField(decimal_places=6, default=0, max_digits=18)),
                ("short_qty",         models.DecimalField(decimal_places=6, default=0, max_digits=18)),
                ("net_qty",           models.DecimalField(decimal_places=6, default=0, max_digits=18)),
                ("long_usd",          models.DecimalField(decimal_places=2, default=0, max_digits=18)),
                ("short_usd",         models.DecimalField(decimal_places=2, default=0, max_digits=18)),
                ("net_usd",           models.DecimalField(decimal_places=2, default=0, max_digits=18)),
                ("trader_count",      models.PositiveIntegerField(default=0)),
                ("concentration_pct", models.DecimalField(decimal_places=2, default=0, max_digits=6)),
                ("unrealized_pnl",    models.DecimalField(decimal_places=2, default=0, max_digits=18)),
                ("current_price",     models.DecimalField(decimal_places=6, default=0, max_digits=18)),
                ("is_high_risk",      models.BooleanField(default=False)),
                ("snapshot", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="symbol_exposures",
                    to="simulator.brokersnapshot",
                )),
            ],
            options={"ordering": ["-concentration_pct"]},
        ),
        migrations.AlterUniqueTogether(
            name="symbolexposure",
            unique_together={("snapshot", "symbol")},
        ),

        # ── TraderClassExposure ──
        migrations.CreateModel(
            name="TraderClassExposure",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("trader_class",    models.CharField(max_length=12)),
                ("routing_profile", models.CharField(default="INTERNAL", max_length=20)),
                ("account_count",   models.PositiveIntegerField(default=0)),
                ("long_usd",        models.DecimalField(decimal_places=2, default=0, max_digits=18)),
                ("short_usd",       models.DecimalField(decimal_places=2, default=0, max_digits=18)),
                ("net_usd",         models.DecimalField(decimal_places=2, default=0, max_digits=18)),
                ("unrealized_pnl",  models.DecimalField(decimal_places=2, default=0, max_digits=18)),
                ("snapshot", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="class_exposures",
                    to="simulator.brokersnapshot",
                )),
            ],
            options={"ordering": ["trader_class"]},
        ),
        migrations.AlterUniqueTogether(
            name="traderclassexposure",
            unique_together={("snapshot", "trader_class")},
        ),
    ]
