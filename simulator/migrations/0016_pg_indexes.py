from django.db import migrations, models


class Migration(migrations.Migration):
    """
    Add performance indexes for PostgreSQL production readiness.

    Why each index:

    acc_status_idx
        exposure_engine: TradingAccount.filter(status="Activo").count()
        Called on every compute_live_analytics() invocation.

    acc_user_phase_idx
        population_engine: filter(user=sim_user, phase__startswith="Sim:")
        Also covers admin filtering by user. Composite to serve both filters
        with a single index scan.

    ledger_acc_type_ts_idx
        risk_engine (check_and_enforce_risk + validate_order_risk):
            filter(account=X, event_type="REALIZED_PNL", created_at__gte=T0, created_at__lt=T1)
            .aggregate(total=Sum("amount"))
        High-frequency query — called after every trade close. Without this
        index, Postgres does a full LedgerEntry scan per account per close.

    ledger_created_at_idx
        exposure_engine (compute_live_analytics):
            filter(event_type="REALIZED_PNL", created_at__gte=T0, created_at__lt=T1)
            .aggregate(...)  — broker-wide, no account filter
        Range predicate on created_at; the covering index above has account
        first so it doesn't help here. A standalone created_at index does.

    pos_acc_opened_idx
        population_engine._tick():
            Position.filter(account=acc).order_by("opened_at")
        Every simulation tick for every active thread hits this query.
        Also benefits the force-close time check.
    """

    dependencies = [
        ("simulator", "0015_exposure_engine"),
    ]

    operations = [
        # ── TradingAccount ───────────────────────────────────────────────────
        migrations.AddIndex(
            model_name="tradingaccount",
            index=models.Index(fields=["status"], name="acc_status_idx"),
        ),
        migrations.AddIndex(
            model_name="tradingaccount",
            index=models.Index(fields=["user", "phase"], name="acc_user_phase_idx"),
        ),

        # ── LedgerEntry ──────────────────────────────────────────────────────
        migrations.AddIndex(
            model_name="ledgerentry",
            index=models.Index(
                fields=["account", "event_type", "created_at"],
                name="ledger_acc_type_ts_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="ledgerentry",
            index=models.Index(fields=["created_at"], name="ledger_created_at_idx"),
        ),

        # ── Position ─────────────────────────────────────────────────────────
        migrations.AddIndex(
            model_name="position",
            index=models.Index(fields=["account", "opened_at"], name="pos_acc_opened_idx"),
        ),
    ]
