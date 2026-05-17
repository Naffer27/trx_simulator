from django.db import migrations, models


class Migration(migrations.Migration):
    """
    Sync migration: records that TradingAccount.status choices grew from
    the original 3-value set (0001_initial) to the current 5-value set.

    No DB change — CharField choices are not stored in the schema.
    This exists purely to satisfy Django's migration state tracker.
    """

    dependencies = [
        ("simulator", "0016_pg_indexes"),
    ]

    operations = [
        migrations.AlterField(
            model_name="tradingaccount",
            name="status",
            field=models.CharField(
                choices=[
                    ("Activo",     "Active"),
                    ("Suspendido", "Suspended"),
                    ("Violado",    "Violated"),
                    ("Cerrado",    "Closed"),
                    ("Completado", "Funded/Completed"),
                ],
                default="Activo",
                max_length=20,
            ),
        ),
    ]
