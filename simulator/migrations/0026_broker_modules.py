from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('simulator', '0025_add_totp_device'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='CalendarEvent',
            fields=[
                ('id',         models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('title',      models.CharField(max_length=200)),
                ('currency',   models.CharField(max_length=8)),
                ('country',    models.CharField(blank=True, max_length=60)),
                ('event_date', models.DateTimeField()),
                ('impact',     models.CharField(choices=[('LOW', 'Low'), ('MEDIUM', 'Medium'), ('HIGH', 'High')], default='MEDIUM', max_length=10)),
                ('actual',     models.CharField(blank=True, max_length=40)),
                ('forecast',   models.CharField(blank=True, max_length=40)),
                ('previous',   models.CharField(blank=True, max_length=40)),
                ('published',  models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={'verbose_name': 'Calendar Event', 'verbose_name_plural': 'Calendar Events', 'ordering': ['event_date']},
        ),
        migrations.AddIndex(
            model_name='calendarevent',
            index=models.Index(fields=['event_date', 'published'], name='calendar_date_pub_idx'),
        ),
        migrations.CreateModel(
            name='Referral',
            fields=[
                ('id',                   models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('code',                 models.CharField(max_length=20, unique=True)),
                ('clicks',               models.PositiveIntegerField(default=0)),
                ('registrations',        models.PositiveIntegerField(default=0)),
                ('estimated_commission', models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ('created_at',           models.DateTimeField(auto_now_add=True)),
                ('user',                 models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='referral', to=settings.AUTH_USER_MODEL)),
            ],
            options={'verbose_name': 'Referral / IB Link'},
        ),
        migrations.CreateModel(
            name='Bonus',
            fields=[
                ('id',          models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('title',       models.CharField(max_length=120)),
                ('description', models.TextField(blank=True)),
                ('bonus_type',  models.CharField(choices=[('CREDIT', 'Bono de crédito'), ('PERCENTAGE', 'Porcentaje sobre depósito'), ('REBATE', 'Rebate / cashback')], default='CREDIT', max_length=20)),
                ('value',       models.DecimalField(decimal_places=2, max_digits=12)),
                ('active',      models.BooleanField(default=True)),
                ('expires_at',  models.DateTimeField(blank=True, null=True)),
                ('created_at',  models.DateTimeField(auto_now_add=True)),
            ],
            options={'verbose_name': 'Bonus', 'verbose_name_plural': 'Bonuses', 'ordering': ['-created_at']},
        ),
        migrations.CreateModel(
            name='BrokerDocument',
            fields=[
                ('id',          models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('title',       models.CharField(max_length=200)),
                ('description', models.TextField(blank=True)),
                ('file',        models.FileField(upload_to='broker_documents/')),
                ('category',    models.CharField(choices=[('CONTRACT', 'Contratos'), ('GUIDE', 'Guías'), ('CERTIFICATE', 'Certificados'), ('REPORT', 'Reportes')], default='GUIDE', max_length=20)),
                ('public',      models.BooleanField(default=True, help_text='Visible to all logged-in users')),
                ('created_at',  models.DateTimeField(auto_now_add=True)),
            ],
            options={'verbose_name': 'Broker Document', 'verbose_name_plural': 'Broker Documents', 'ordering': ['category', 'title']},
        ),
        migrations.CreateModel(
            name='ExpertAdvisor',
            fields=[
                ('id',           models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name',         models.CharField(max_length=120)),
                ('description',  models.TextField(blank=True)),
                ('category',     models.CharField(choices=[('TREND', 'Tendencia'), ('SCALPING', 'Scalping'), ('GRID', 'Grid'), ('HEDGING', 'Cobertura'), ('CUSTOM', 'Personalizado')], default='TREND', max_length=20)),
                ('version',      models.CharField(blank=True, max_length=20)),
                ('download_url', models.URLField(blank=True, help_text='External link or leave blank for future upload')),
                ('active',       models.BooleanField(default=True)),
                ('coming_soon',  models.BooleanField(default=False)),
                ('created_at',   models.DateTimeField(auto_now_add=True)),
            ],
            options={'verbose_name': 'Expert Advisor', 'verbose_name_plural': 'Expert Advisors', 'ordering': ['category', 'name']},
        ),
    ]
