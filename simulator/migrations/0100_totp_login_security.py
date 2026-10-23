from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [('simulator', '0099_broker_economics_05_ledger_economic_date')]
    operations = [
        migrations.AddField('totpdevice', 'login_last_step', models.BigIntegerField(default=-1)),
        migrations.AddField('totpdevice', 'login_failures', models.PositiveIntegerField(default=0)),
        migrations.AddField('totpdevice', 'login_locked_until', models.DateTimeField(null=True, blank=True)),
    ]
