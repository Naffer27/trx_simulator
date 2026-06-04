"""
Django settings for trx_simulator project.
"""

from pathlib import Path
import os
from dotenv import load_dotenv  # cargar secretos sin hardcodear

# ===============================
# 🔑 Cargar variables de entorno
# ===============================
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")  # si no existe, no rompe

# Seguridad / Debug
SECRET_KEY = os.getenv(
    "DJANGO_SECRET_KEY",
    "django-insecure-quo5(^mgzytvr!!+qes+#ywpo0y29+x7)pav7m2!k26(pd7ct6",  # fallback dev
)
DEBUG = os.getenv("DEBUG", "False").strip().lower() in {"1", "true", "yes"}

# Acceso con código (usado por LoginForm para requerirlo en PROD)
BROKER_ACCESS_CODE = os.getenv("BROKER_ACCESS_CODE", "").strip()

_BASE_HOSTS = ["0.0.0.0", "127.0.0.1", "localhost", ".ngrok-free.dev", ".ngrok-free.app"]
# In DEBUG mode allow wildcard so ngrok subdomains don't cause 400s during development.
# In production, only the explicit list is used.
ALLOWED_HOSTS = ["*"] + _BASE_HOSTS if DEBUG else _BASE_HOSTS

# Apps
INSTALLED_APPS = [
    # tiempo real/servidor asgi
    "daphne",      # runserver con ASGI/WS estable
    "channels",

    # Django
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",

    # Terceros
    "widget_tweaks",

    # Tu app
    "simulator",
]

# Middleware
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "simulator.middleware.RequestIDMiddleware",   # injects X-Request-ID for log correlation
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "trx_simulator.urls"

# Templates
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# WSGI/ASGI
WSGI_APPLICATION = "trx_simulator.wsgi.application"
ASGI_APPLICATION = "trx_simulator.asgi.application"  # requerido por Channels

# Base de datos — PostgreSQL en producción, SQLite en dev sin DB_NAME
_db_name = os.getenv("DB_NAME", "")
if _db_name:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": _db_name,
            "USER": os.getenv("DB_USER", "postgres"),
            "PASSWORD": os.getenv("DB_PASSWORD", ""),
            "HOST": os.getenv("DB_HOST", "localhost"),
            "PORT": os.getenv("DB_PORT", "5432"),
            # Keep connections alive for 60 s — avoids per-request handshake.
            # Combined with CONN_HEALTH_CHECKS, stale connections are replaced
            # automatically instead of raising OperationalError.
            "CONN_MAX_AGE": 60,
            "CONN_HEALTH_CHECKS": True,
            "OPTIONS": {
                # Abort if Postgres is unreachable within 10 s (prevents silent hangs).
                "connect_timeout": 10,
                # Identifies this process in pg_stat_activity — useful for debugging.
                "application_name": os.getenv("APP_NAME", "trx_sim"),
            },
            "TEST": {
                # Separate DB name for test runs so prod data is never touched.
                "NAME": os.getenv("DB_TEST_NAME", f"{_db_name}_test"),
            },
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

# Password validators (dev off)
AUTH_PASSWORD_VALIDATORS = []

# i18n
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# ===============================
# 📂 Archivos estáticos
# ===============================
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

# WhiteNoise (gzip/br + manifest)
STORAGES = {
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}
# Comodidades en dev/prod
WHITENOISE_AUTOREFRESH = DEBUG
WHITENOISE_MAX_AGE = 0 if DEBUG else 60 * 60 * 24 * 30  # 30 días

# Media files (user uploads: broker documents, EA images)
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# Primary key
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ===============================
# 🔌 Channels (Redis opcional)
# ===============================
REDIS_URL = os.getenv("REDIS_URL", "").strip()
if REDIS_URL:
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels_redis.core.RedisChannelLayer",
            "CONFIG": {
                "hosts": [REDIS_URL],
                "capacity": 1500,       # max msgs per channel before back-pressure
                "expiry": 10,           # msg TTL seconds (prevents stale price ticks)
                "group_expiry": 86400,  # group membership TTL (1 day)
            },
        }
    }
else:
    CHANNEL_LAYERS = {  # memoria (dev sin Redis)
        "default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}
    }

# ===============================
# ⚙️  Celery
# ===============================
_CELERY_BROKER = REDIS_URL or "redis://127.0.0.1:6379/0"
CELERY_BROKER_URL              = _CELERY_BROKER
CELERY_RESULT_BACKEND          = _CELERY_BROKER
CELERY_TASK_SERIALIZER         = "json"
CELERY_RESULT_SERIALIZER       = "json"
CELERY_ACCEPT_CONTENT          = ["json"]
CELERY_TIMEZONE                = TIME_ZONE          # inherit Django TZ (UTC)
CELERY_TASK_TRACK_STARTED      = True
CELERY_TASK_TIME_LIMIT         = 30 * 60            # 30 min hard kill
CELERY_TASK_SOFT_TIME_LIMIT    = 5 * 60             # 5 min SoftTimeLimitExceeded
CELERY_WORKER_MAX_TASKS_PER_CHILD = 500             # restart worker after N tasks (memory safety)
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True    # don't crash if Redis momentarily down on start

# ── Beat scheduler (redbeat — state in Redis, distributed-safe) ──
CELERY_BEAT_SCHEDULER            = "redbeat.RedBeatScheduler"
REDBEAT_REDIS_URL                = _CELERY_BROKER
REDBEAT_KEY_PREFIX               = "trx:beat:"     # namespace in Redis
REDBEAT_LOCK_TIMEOUT             = 60 * 5          # 5 min — beat must renew lock or release it
CELERY_BEAT_MAX_LOOP_INTERVAL    = 5               # seconds between schedule checks

# ── Scheduled tasks (READ-ONLY audit only) ──────────────────────
from celery.schedules import crontab, timedelta as celery_td  # noqa: E402

CELERY_BEAT_SCHEDULE = {
    # Audit unconfirmed deposits every 15 min
    "reconcile-deposits-15m": {
        "task":     "simulator.reconcile_deposits",
        "schedule": crontab(minute="*/15"),
        "args":     (24,),   # hours_back=24
        "options":  {"expires": 14 * 60},  # drop if not picked up in 14 min
    },
    # Audit stuck withdrawals every 15 min
    "reconcile-withdrawals-15m": {
        "task":     "simulator.reconcile_withdrawals",
        "schedule": crontab(minute="*/15"),
        "args":     (48,),   # hours_back=48
        "options":  {"expires": 14 * 60},
    },
    # Heartbeat ping every 5 min — confirms beat + worker are alive
    "beat-heartbeat-5m": {
        "task":     "simulator.ping",
        "schedule": crontab(minute="*/5"),
        "args":     ("beat-heartbeat",),
        "options":  {"expires": 4 * 60},
    },
    # Equity snapshots every minute — broker + account financial state
    "take-snapshots-1m": {
        "task":     "simulator.take_snapshots",
        "schedule": crontab(minute="*"),
        "options":  {"expires": 55},   # drop if not picked up before next tick
    },
    # Cleanup old audit log entries every night at 2:00 UTC (30-day retention)
    "cleanup-audit-log-daily": {
        "task":     "simulator.cleanup_audit_log",
        "schedule": crontab(hour=2, minute=0),
        "args":     (30,),               # retention_days=30
        "options":  {"expires": 55 * 60},
    },
    # Cleanup old snapshots every night at 3:00 UTC
    "cleanup-snapshots-daily": {
        "task":     "simulator.cleanup_snapshots",
        "schedule": crontab(hour=3, minute=0),
        "args":     (),                # uses SNAPSHOT_RETENTION_DAYS from settings
        "options":  {"expires": 55 * 60},
    },
    # Offline SL/TP + stopout + margin-call daemon every 30 s
    "scan-positions-30s": {
        "task":     "simulator.scan_positions",
        "schedule": celery_td(seconds=30),
        "options":  {"expires": 28},   # drop if not picked up before next firing
    },
    # Broker revenue snapshot every 5 min — equity curve + trend analytics
    "take-revenue-snapshot-5m": {
        "task":     "simulator.take_revenue_snapshot",
        "schedule": crontab(minute="*/5"),
        "options":  {"expires": 4 * 60},  # drop if not picked up within 4 min
    },
    # Challenge evaluation every hour — advance or fail active enrollments
    "evaluate-challenges-hourly": {
        "task":     "simulator.evaluate_all_challenges",
        "schedule": crontab(minute=0),     # top of every hour
        "options":  {"expires": 55 * 60},  # drop if not picked up within 55 min
    },
}

# Revenue snapshot retention (separate from equity snapshots — longer window for trend history).
# Future path: set to 0 or export-before-delete for cold-storage/warehouse integration.
REVENUE_SNAPSHOT_RETENTION_DAYS = int(os.getenv("REVENUE_SNAPSHOT_RETENTION_DAYS", "90"))

# ── Equity snapshot retention ─────────────────────────────────────────────────
SNAPSHOT_RETENTION_DAYS = int(os.getenv("SNAPSHOT_RETENTION_DAYS", "7"))

# ===============================
# 📋 Logging — JSON or verbose
# ===============================
_LOG_JSON = os.getenv("LOG_JSON", "false").strip().lower() in {"1", "true", "yes"}
_CONSOLE_FORMATTER = "structured" if _LOG_JSON else "verbose"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {"format": "[{levelname}] {name}: {message}", "style": "{"},
        "verbose": {
            "format": "{asctime} [{levelname}] {name} | {message}",
            "style": "{",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
        # Structured JSON — one line per record, includes req_id/task_id fields.
        # Activate with LOG_JSON=true (e.g. in production / log aggregation).
        "structured": {
            "()": "simulator.observability.StructuredFormatter",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": _CONSOLE_FORMATTER,
        }
    },
    "loggers": {
        # ── Broad simulator catch-all ──
        "simulator":               {"handlers": ["console"], "level": os.getenv("SIM_LOG_LEVEL", "INFO"),        "propagate": False},
        # ── Specialised sub-loggers (tune independently via env vars) ──
        "simulator.ws":            {"handlers": ["console"], "level": os.getenv("SIM_WS_LOG_LEVEL", "INFO"),    "propagate": False},
        "simulator.population":    {"handlers": ["console"], "level": os.getenv("SIM_POP_LOG_LEVEL", "INFO"),   "propagate": False},
        "simulator.exposure":      {"handlers": ["console"], "level": os.getenv("SIM_EXP_LOG_LEVEL", "WARNING"), "propagate": False},
        "simulator.risk":          {"handlers": ["console"], "level": os.getenv("SIM_RISK_LOG_LEVEL", "WARNING"), "propagate": False},
        "simulator.observability": {"handlers": ["console"], "level": "INFO",    "propagate": False},
        "simulator.security":     {"handlers": ["console"], "level": "INFO",    "propagate": False},
        "celery":                  {"handlers": ["console"], "level": "INFO",    "propagate": False},
        "celery.worker":           {"handlers": ["console"], "level": "INFO",    "propagate": False},
        "celery.beat":             {"handlers": ["console"], "level": "INFO",    "propagate": False},
        "django.channels":         {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "daphne":                  {"handlers": ["console"], "level": "INFO",    "propagate": False},
    },
}

# ===============================
# 🔐 2FA (TOTP)
# ===============================
TOTP_ISSUER_NAME    = os.getenv("TOTP_ISSUER_NAME", "Money Brokers")
# Fernet key for encrypting TOTP secrets at rest.
# Generate once with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Must be 32-byte base64-encoded key. If not set, secrets are stored base64-only (dev mode).
TOTP_ENCRYPTION_KEY = os.getenv("TOTP_ENCRYPTION_KEY", "").strip()
# Enforce 2FA for all staff/admin users (set True in production after testing)
TOTP_STAFF_REQUIRED = os.getenv("TOTP_STAFF_REQUIRED", "False").strip().lower() in {"1", "true", "yes"}

# Load-test bypass — disables HTTP rate limiting for load testing scenarios.
# MUST be False (default) in production. Only set True in .env.staging during tests.
# ===============================
# External Challenge Webhook
# ===============================
# Shared secret for verifying POST requests from external sales platforms.
# Generate with: python -c "import secrets; print(secrets.token_hex(32))"
# Must match the secret configured on the external platform.
# If empty, ALL external webhook requests are rejected.
CHALLENGE_WEBHOOK_SECRET = os.getenv("CHALLENGE_WEBHOOK_SECRET", "").strip()

LOAD_TEST_MODE = os.getenv("LOAD_TEST_MODE", "False").strip().lower() in {"1", "true", "yes"}
if LOAD_TEST_MODE:
    import logging as _logging
    _logging.getLogger("simulator.security").warning(
        "[ratelimit] LOAD_TEST_MODE is ENABLED — rate limiting is DISABLED. "
        "This MUST be False in production."
    )

# Login / redirecciones
LOGIN_URL = "simulator:login"
LOGIN_REDIRECT_URL = "simulator:dashboard"

# ===============================
# 📧 Configuración de Gmail (SMTP)
# ===============================
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = os.getenv("EMAIL_HOST", "smtp.gmail.com")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_USE_TLS = os.getenv("EMAIL_USE_TLS", "True").strip().lower() in {"1", "true", "yes"}
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")  # required in .env
DEFAULT_FROM_EMAIL = EMAIL_HOST_USER
ADMINS = [("Admin", os.getenv("ADMIN_EMAIL", "nafferphotographer@gmail.com"))]

# ===============================
# 🔐 CSRF / Proxy
# ===============================
CSRF_TRUSTED_ORIGINS = [
    "http://127.0.0.1:8000",
    "http://localhost:8000",
    "http://0.0.0.0:8000",
    "https://localhost",
    "https://127.0.0.1",
    "https://*.ngrok-free.dev",
    "https://*.ngrok-free.app",
]
# ngrok suele mandar X-Forwarded-Proto=https
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True

# endurecer cookies en prod
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SAMESITE = "Lax"

# ===============================
# 📡 API Keys externas
# ===============================
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")
NOWPAYMENTS_API_KEY       = os.getenv("NOWPAYMENTS_API_KEY", "")
NOWPAYMENTS_IPN_SECRET    = os.getenv("NOWPAYMENTS_IPN_SECRET", "")
NOWPAYMENTS_WEBHOOK_URL   = os.getenv("NOWPAYMENTS_WEBHOOK_URL", "")
# Payouts API (separate JWT auth — used for crypto withdrawals)
NOWPAYMENTS_EMAIL         = os.getenv("NOWPAYMENTS_EMAIL", "")
NOWPAYMENTS_PASSWORD      = os.getenv("NOWPAYMENTS_PASSWORD", "")

# ===============================
# 🔭 Observability — Sentry
# ===============================
SENTRY_DSN         = os.getenv("SENTRY_DSN", "").strip()
SENTRY_ENVIRONMENT = os.getenv("SENTRY_ENVIRONMENT", "development" if DEBUG else "production")
SENTRY_RELEASE     = os.getenv("SENTRY_RELEASE", "")  # e.g. git SHA injected by CI

if SENTRY_DSN:
    from simulator.observability import init_sentry
    init_sentry(
        dsn=SENTRY_DSN,
        environment=SENTRY_ENVIRONMENT,
        release=SENTRY_RELEASE or None,
    )