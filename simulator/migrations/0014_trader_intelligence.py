from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("simulator", "0013_risk_engine"),
    ]

    operations = [
        # ── Index on Trade(account, closed_at) for fast per-account queries ──
        migrations.AddIndex(
            model_name="trade",
            index=models.Index(fields=["account", "closed_at"], name="trade_acc_closed_idx"),
        ),

        # ── New TraderScore fields ──

        # Classification
        migrations.AddField(
            model_name="traderscore",
            name="routing_profile",
            field=models.CharField(
                max_length=20,
                default="INTERNAL",
                choices=[
                    ("INTERNAL", "Internal"),
                    ("REVIEW", "Review"),
                    ("HEDGE_CANDIDATE", "Hedge Candidate"),
                    ("ELITE", "Elite"),
                ],
            ),
        ),

        # Basic performance (new)
        migrations.AddField(
            model_name="traderscore",
            name="avg_rr",
            field=models.DecimalField(decimal_places=3, default=0, max_digits=8),
        ),
        migrations.AddField(
            model_name="traderscore",
            name="pnl_volatility",
            field=models.DecimalField(decimal_places=3, default=0, max_digits=8),
        ),

        # Behavioral signals
        migrations.AddField(
            model_name="traderscore",
            name="lot_growth_rate",
            field=models.DecimalField(decimal_places=4, default=0, max_digits=8),
        ),
        migrations.AddField(
            model_name="traderscore",
            name="scalping_ratio",
            field=models.DecimalField(decimal_places=3, default=0, max_digits=5),
        ),
        migrations.AddField(
            model_name="traderscore",
            name="avg_hold_time_seconds",
            field=models.DecimalField(decimal_places=1, default=0, max_digits=10),
        ),
        migrations.AddField(
            model_name="traderscore",
            name="toxicity_score",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=5),
        ),
        migrations.AddField(
            model_name="traderscore",
            name="gambler_score",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=5),
        ),
        migrations.AddField(
            model_name="traderscore",
            name="trade_frequency",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=8),
        ),
        migrations.AddField(
            model_name="traderscore",
            name="max_consecutive_losses",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="traderscore",
            name="max_consecutive_wins",
            field=models.PositiveIntegerField(default=0),
        ),

        # Widen trader_class to accommodate GAMBLER (7) and SCALPER (7) — max_length stays 12
        migrations.AlterField(
            model_name="traderscore",
            name="trader_class",
            field=models.CharField(
                max_length=12,
                default="NORMAL",
                choices=[
                    ("NORMAL",      "Normal"),
                    ("RISKY",       "Risky"),
                    ("MARTINGALE",  "Martingale"),
                    ("TOXIC",       "Toxic"),
                    ("CONSISTENT",  "Consistent"),
                    ("ELITE",       "Elite"),
                    ("GAMBLER",     "Gambler"),
                    ("SCALPER",     "Scalper"),
                ],
            ),
        ),
    ]
