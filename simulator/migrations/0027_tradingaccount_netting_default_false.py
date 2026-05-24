from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('simulator', '0026_broker_modules'),
    ]

    operations = [
        migrations.AlterField(
            model_name='tradingaccount',
            name='netting_mode',
            field=models.BooleanField(
                default=False,
                help_text='True=Netting (consolidar por símbolo); False=Hedging (varias posiciones).',
            ),
        ),
    ]
