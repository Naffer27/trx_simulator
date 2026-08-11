"""
Django settings for trx_simulator project.
"""

from pathlib import Path
import os
import sys
from dotenv import load_dotenv  # cargar secretos sin hardcodear
from django.core.exceptions import ImproperlyConfigured

# ===============================
# 🔑 Cargar variables de entorno
# ===============================
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")  # si no existe, no rompe

# Shared by every "raise ImproperlyConfigured unless we're inside
# `manage.py test`" guard in this file (O.4c-1 — previously each guard
# recomputed this identical expression under a locally-scoped name;
# centralized here so a new guard never needs to duplicate it again).
_IN_TEST_RUN = len(sys.argv) > 1 and sys.argv[1] == "test"

# Seguridad / Debug
_secret_key = os.getenv("DJANGO_SECRET_KEY", "").strip()
if not _secret_key:
    if _IN_TEST_RUN:
        # Fixed test-only secret. Never used outside `manage.py test`.
        _secret_key = "test-only-secret-key-not-for-production-use-ever"
    else:
        raise ImproperlyConfigured(
            "DJANGO_SECRET_KEY environment variable is not set. "
            "Add it to your .env file. "
            "Generate one with: python -c \"from django.core.management.utils "
            "import get_random_secret_key; print(get_random_secret_key())\""
        )
SECRET_KEY = _secret_key
DEBUG = os.getenv("DEBUG", "False").strip().lower() in {"1", "true", "yes"}

# ===============================
# 🌎 Application environment — O.4c-1
# ===============================
# APP_ENV is the explicit, single source of truth for which deployment
# tier this process is running as. Deliberately independent of DEBUG
# (a misconfigured DEBUG alone must never silently define "is this
# production") and independent of SENTRY_ENVIRONMENT below (Sentry-only,
# cosmetic, never gates behavior — O.4c Fase 0 decision, Opción B).
# Unlike the guards above, an invalid value here is never exempted for
# `manage.py test` — no legitimate test scenario needs an invalid
# APP_ENV, and the existing suite never sets this var (default
# "development" is always valid), so this can never break the suite.
_VALID_APP_ENVS = {"development", "staging", "production"}
APP_ENV = os.getenv("APP_ENV", "development").strip().lower()
if APP_ENV not in _VALID_APP_ENVS:
    raise ImproperlyConfigured(
        f"APP_ENV={APP_ENV!r} is not valid. Must be one of: "
        + ", ".join(sorted(_VALID_APP_ENVS))
    )

# O.4c-2 — Guard: staging/production must never boot with DEBUG=True.
# APP_ENV (not DB_NAME, not SENTRY_ENVIRONMENT) is the sole source of
# truth for this — O.4c Fase 0 §6/§7. Skipped during `manage.py test`
# via the same centralized _IN_TEST_RUN flag as every other guard in
# this file, so the local suite (no PostgreSQL available here) keeps
# running unaffected regardless of which APP_ENV happens to be set.
if APP_ENV in {"staging", "production"} and DEBUG:
    if not _IN_TEST_RUN:
        raise ImproperlyConfigured(
            f"DEBUG=True is not allowed when APP_ENV={APP_ENV!r}. "
            "Set DEBUG=False in your .env file for staging/production — "
            "DEBUG=True leaks stack traces, SQL, and settings on every "
            "unhandled error."
        )

# O.4c-7 — Guard: staging/production must never boot with a placeholder
# or low-entropy DJANGO_SECRET_KEY. The presence/empty check above (L.24-36)
# already runs unconditionally in every real environment — this guard adds
# a *quality* check on top of it, scoped to staging/production only so a
# development .env copied straight from .env.example (whose own placeholder
# is non-empty but weak) keeps working — O.4c-7 Fase 0 §6/§7. The threshold
# is Django's own SECRET_KEY check (django/core/checks/security/base.py,
# security.W009: SECRET_KEY_MIN_LENGTH=50, SECRET_KEY_MIN_UNIQUE_CHARACTERS=5,
# rejecting the `startproject`-generated "django-insecure-" prefix) — not an
# arbitrary blocklist of known placeholder strings. Skipped during
# `manage.py test` via the same centralized _IN_TEST_RUN flag as every
# other guard in this file.
if APP_ENV in {"staging", "production"}:
    _sk_unique_chars = len(set(SECRET_KEY))
    if (
        len(SECRET_KEY) < 50
        or _sk_unique_chars < 5
        or SECRET_KEY.startswith("django-insecure-")
    ):
        if not _IN_TEST_RUN:
            raise ImproperlyConfigured(
                f"DJANGO_SECRET_KEY does not meet Django's minimum security "
                f"requirements for APP_ENV={APP_ENV!r} (needs >=50 characters, "
                f">=5 unique characters, and must not start with "
                f"'django-insecure-'). Generate a real one with: python -c "
                f"\"from django.core.management.utils import "
                f"get_random_secret_key; print(get_random_secret_key())\""
            )

# Acceso con código (usado por LoginForm para requerirlo en PROD)
BROKER_ACCESS_CODE = os.getenv("BROKER_ACCESS_CODE", "").strip()

_BASE_HOSTS = ["0.0.0.0", "127.0.0.1", "localhost", ".ngrok-free.dev", ".ngrok-free.app"]

# Primary production domain — set DOMAIN=yourdomain.com in prod .env
# Automatically includes the bare domain and the www. subdomain.
DOMAIN = os.getenv("DOMAIN", "").strip()
_domain_hosts = [DOMAIN, f"www.{DOMAIN}"] if DOMAIN else []
_extra_hosts  = [h.strip() for h in os.getenv("ALLOWED_HOSTS_EXTRA", "").split(",") if h.strip()]

# In DEBUG mode allow wildcard so ngrok subdomains don't cause 400s during development.
# In production, only the explicit list is used.
ALLOWED_HOSTS = (["*"] + _BASE_HOSTS) if DEBUG else (_BASE_HOSTS + _domain_hosts + _extra_hosts)

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
                "simulator.context_processors.readiness_context",
            ],
        },
    },
]

# WSGI/ASGI
WSGI_APPLICATION = "trx_simulator.wsgi.application"
ASGI_APPLICATION = "trx_simulator.asgi.application"  # requerido por Channels

# Base de datos — PostgreSQL en producción, SQLite en dev sin DB_NAME
_db_name = os.getenv("DB_NAME", "")

# O.4c-1 — Guard: staging/production must never silently fall back to
# SQLite. APP_ENV (not DEBUG) is the source of truth for this — see the
# "Application environment" block above. Skipped during `manage.py
# test` via the shared _IN_TEST_RUN flag, same discipline as every
# other guard in this file — this project's test suite has no local
# PostgreSQL server available and must keep running on SQLite
# regardless of APP_ENV (O.4c Fase 0 §8/§TESTS).
if APP_ENV in {"staging", "production"} and not _db_name:
    if not _IN_TEST_RUN:
        raise ImproperlyConfigured(
            f"DB_NAME must be set when APP_ENV={APP_ENV!r} — SQLite is not "
            "permitted outside development. Add DB_NAME (and DB_USER/"
            "DB_PASSWORD/DB_HOST/DB_PORT as needed) to your .env file."
        )

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

# Password validators
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

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
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
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

# O.4c-5 — Guard: staging/production must never boot with a missing or
# empty REDIS_URL. APP_ENV (not DEBUG) is the source of truth, same
# discipline as every other O.4c guard — same shape as the O.4c-1
# DB_NAME guard specifically (presence-only, no format/content
# validation — MED-3 Fase 0 §6/§12 concluded REDIS_URL's invalid-format
# case fails loudly at connection time, unlike TOTP_ENCRYPTION_KEY's
# silent insecure-fallback case, so format validation isn't needed to
# close the real risk here).
#
# Skipped during `manage.py test` via the shared _IN_TEST_RUN flag —
# same necessity already demonstrated for DB_NAME/TOTP_ENCRYPTION_KEY:
# the existing O.4c-1/O.4c-2/O.4c-3/O.4c-4 subprocess tests construct
# APP_ENV=staging/production environments to test THEIR OWN guards
# (MED-3 Fase 0 §7 confirmed none of them previously set REDIS_URL
# explicitly, relying on whatever the real .env happens to have).
#
# Deliberately does NOT attempt any Redis connectivity — no client is
# instantiated, no liveness probe, no network I/O of any kind. This
# guard validates configuration only; runtime availability is health/
# monitoring's concern (O.4e), not settings.py's.
if APP_ENV in {"staging", "production"} and not REDIS_URL:
    if not _IN_TEST_RUN:
        raise ImproperlyConfigured(
            f"REDIS_URL must be set when APP_ENV={APP_ENV!r} — without it, "
            "Django Channels silently falls back to InMemoryChannelLayer "
            "(broken across multiple processes/workers) and Celery/RedBeat "
            "silently default to redis://127.0.0.1:6379/0. Add REDIS_URL to "
            "your .env file (format: redis://[:password@]host:port/db)."
        )

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
    # AUDIT-01 — persist RISK-03 alert observations into BrokerAuditEvent,
    # independent of whether the admin dashboard is ever opened. See
    # simulator/broker_audit.py::observe_broker_alerts().
    "observe-broker-risk-alerts-5m": {
        "task":     "simulator.observe_broker_risk_alerts",
        "schedule": crontab(minute="*/5"),
        "options":  {"expires": 4 * 60},  # drop if not picked up within 4 min
    },
    # O.3d-3 — persist Treasury stuck-execution observations into
    # BrokerAuditEvent, independent of whether the admin ever looks.
    # Same 15-min cadence as reconcile-deposits/withdrawals above —
    # comfortably above the 600s (10 min) minimum age threshold
    # inspect_stuck_treasury_execution() already applies, so a request
    # is never checked before it could possibly be eligible. See
    # simulator/broker_audit.py::observe_stuck_treasury_executions().
    "observe-treasury-stuck-executions-15m": {
        "task":     "simulator.observe_treasury_stuck_executions",
        "schedule": crontab(minute="*/15"),
        "options":  {"expires": 14 * 60},  # drop if not picked up in 14 min
    },
    # O.4e-2 — persist a durable EV_CELERY_BEAT_HEARTBEAT row every tick,
    # independent of "beat-heartbeat-5m" above (that one is a generic
    # worker round-trip ping with no durable storage). Same 5-minute
    # cadence — see broker_audit.CELERY_BEAT_HEARTBEAT_INTERVAL_SECONDS
    # and CELERY_BEAT_HEARTBEAT_STALE_SECONDS (15 min = 3x this cadence).
    "record-celery-beat-heartbeat-5m": {
        "task":     "simulator.record_celery_beat_heartbeat",
        "schedule": crontab(minute="*/5"),
        "options":  {"expires": 4 * 60},  # drop if not picked up within 4 min
    },
}

# Revenue snapshot retention (separate from equity snapshots — longer window for trend history).
# Future path: set to 0 or export-before-delete for cold-storage/warehouse integration.
REVENUE_SNAPSHOT_RETENTION_DAYS = int(os.getenv("REVENUE_SNAPSHOT_RETENTION_DAYS", "90"))

# ── Equity snapshot retention ─────────────────────────────────────────────────
SNAPSHOT_RETENTION_DAYS = int(os.getenv("SNAPSHOT_RETENTION_DAYS", "7"))

# ── Backup durability signal (O.5a) ────────────────────────────────────────────
# Deliberately filesystem-based, never PostgreSQL/Redis/Celery/BrokerAuditEvent/
# AuditLog (O.5a Fase 0 §2/§8/§13) — the signal must stay readable even if the
# database it protects is down. deploy/scripts/backup_postgres.sh writes
# backup_success.json/backup_failure.json under this directory;
# simulator/backup_monitoring.py reads them read-only. Not inside BACKUP_DIR
# itself (a separate, possibly different-filesystem, retention-pruned
# directory) — reused from the ReadWritePaths already granted to every
# systemd .service in deploy/systemd/ under ProtectSystem=strict, so a future
# backup-postgres.service (O.5b) needs no new filesystem permission.
BACKUP_METADATA_PATH = os.getenv("BACKUP_METADATA_PATH", "/var/log/trx_sim/backup/")

# Only documented cadence today is daily @ 03:00 UTC (DEPLOY.md, INFRA_PLAN_L1.md).
# Stale threshold is 1.5x the expected interval, not the 3x used by
# CELERY_BEAT_HEARTBEAT_STALE_SECONDS above — a 5-minute heartbeat can absorb a
# missed tick without concern, but a daily backup at 3x (72h) would hide two
# full consecutive missed days, too permissive given the C1/C2/C4 findings in
# the Backup/Restore Operational Readiness Fase 0. 36h tolerates the job
# running a few hours late without a false positive, while still flagging a
# fully missed day.
BACKUP_EXPECTED_INTERVAL_SECONDS = int(os.getenv("BACKUP_EXPECTED_INTERVAL_SECONDS", "86400"))
BACKUP_STALE_SECONDS = int(os.getenv("BACKUP_STALE_SECONDS", "129600"))

# ── Offsite backup verification signal (O.5c-3) ─────────────────────────────────
# Deliberately filesystem-based, same discipline as BACKUP_* above —
# simulator/offsite_monitoring.py reads only offsite_success.json under
# BACKUP_METADATA_PATH (written by deploy/scripts/backup_offsite.sh, O.5c-1),
# never rclone, never pg_restore, never the network. The strong
# cryptographic/remote verification already happened once, durably, inside
# that script — this signal never repeats it.
#
# OFFSITE_CONFIGURED reuses the EXISTING RCLONE_REMOTE contract from O.5c-1
# (the env var backup_offsite.sh itself already refuses to run without) as
# the sole "is offsite configured here" signal — deliberately not a second,
# parallel enablement flag (O.5c-3 Fase 0 approval). This value is NEVER
# used to configure rclone or connect to anything; it exists only so
# offsite_monitoring.py can distinguish "not_configured" (deliberately no
# offsite here) from a genuine "missing" (configured, but no verified
# success has ever landed).
OFFSITE_CONFIGURED = bool(os.getenv("RCLONE_REMOTE", "").strip())

# Same daily cadence as BACKUP_STALE_SECONDS above — backup-offsite.timer
# fires once daily at ~03:30 UTC (deploy/systemd/backup-offsite.timer,
# O.5c-2), 30 minutes after backup-postgres.timer's own 03:00 UTC daily
# fire — so the identical 1.5x-of-expected-interval formula applies
# unchanged, derived from the same real cadence, not a new arbitrary number.
OFFSITE_EXPECTED_INTERVAL_SECONDS = int(os.getenv("OFFSITE_EXPECTED_INTERVAL_SECONDS", "86400"))
OFFSITE_STALE_SECONDS = int(os.getenv("OFFSITE_STALE_SECONDS", "129600"))

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
TOTP_ISSUER_NAME    = os.getenv("TOTP_ISSUER_NAME", "Money Broker")
# Fernet key for encrypting TOTP secrets at rest.
# Generate once with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Must be 32-byte base64-encoded key. If not set, secrets are stored base64-only (dev mode).
TOTP_ENCRYPTION_KEY = os.getenv("TOTP_ENCRYPTION_KEY", "").strip()

# O.4c-4 — Guard: staging/production must never boot with a missing or
# malformed TOTP_ENCRYPTION_KEY. APP_ENV (not DEBUG) is the source of
# truth for this, same discipline as every other O.4c guard above.
# Skipped during `manage.py test` via the shared _IN_TEST_RUN flag —
# the existing O.4c-1/O.4c-2/O.4c-3 subprocess tests already construct
# APP_ENV=staging/production environments with no real Fernet key
# configured, to test THEIR OWN guards; without this exemption those
# already-approved tests would break on an unrelated new guard (MED-3
# Fase 0 §D — verified against their actual env dicts before writing
# this, not assumed).
#
# Deliberately does NOT import simulator/two_factor.py: that module
# itself does `from django.conf import settings`, so importing it here
# would be circular. The Fernet format check is duplicated inline
# (3 lines) rather than shared — this guard answers a different
# question ("is boot allowed?") than two_factor.py's own _get_fernet()
# ("what should this specific encrypt/decrypt call do right now?"),
# and never touches _encrypt_secret()/_decrypt_secret()/_get_fernet()
# or any historical b64:/legacy-unprefixed secret — those remain
# exactly as readable as before regardless of this guard, in every
# environment, including staging/production once a valid key is set.
if APP_ENV in {"staging", "production"}:
    if not _IN_TEST_RUN:
        if not TOTP_ENCRYPTION_KEY:
            raise ImproperlyConfigured(
                f"TOTP_ENCRYPTION_KEY must be set when APP_ENV={APP_ENV!r} — "
                "without it, new TOTP secrets are stored as reversible base64 "
                "instead of encrypted. Generate one with: python -c \"from "
                "cryptography.fernet import Fernet; print(Fernet.generate_key()"
                ".decode())\" and add it to your .env file."
            )
        try:
            from cryptography.fernet import Fernet as _Fernet
            _Fernet(TOTP_ENCRYPTION_KEY.encode())
        except Exception as _exc:
            raise ImproperlyConfigured(
                f"TOTP_ENCRYPTION_KEY is not a valid Fernet key (APP_ENV={APP_ENV!r}): "
                f"{_exc!r}. Generate one with: python -c \"from cryptography.fernet "
                "import Fernet; print(Fernet.generate_key().decode())\" and add it "
                "to your .env file — do not use a placeholder value."
            )
# Enforce 2FA for all staff/admin users (set True in production after testing)
TOTP_STAFF_REQUIRED = os.getenv("TOTP_STAFF_REQUIRED", "False").strip().lower() in {"1", "true", "yes"}

# O.4a — dedicated, independent flag: enforce 2FA specifically for Django
# Admin access by users who hold any of the four Treasury permissions (or
# are superusers). Deliberately NOT the same flag as TOTP_STAFF_REQUIRED
# above (which only gates ops_panel_view + the JSON staff APIs, an already
# frozen/tested contract this block does not touch) — see O.4a Fase 0
# Decision 1. Default False so the existing admin test suite (which logs
# in Treasury operators without ever enrolling TOTP) is unaffected until
# this is explicitly enabled. Not yet consulted anywhere (O.4a-1 only
# adds the flag and the helper below; O.4a-2 wires it into
# MoneyBrokerAdminSite.admin_view()).
TOTP_ADMIN_TREASURY_REQUIRED = os.getenv("TOTP_ADMIN_TREASURY_REQUIRED", "False").strip().lower() in {"1", "true", "yes"}

# O.4b-3 — dedicated, independent flag: when True, a NEW grant of one of
# the four Treasury permissions is refused if it would leave the target
# holding more than one of them ("concentration" — O.4b Fase 0 Decision
# 1). Detection and audit of concentration always happen regardless of
# this flag; only the BLOCKING behavior is gated by it. Default False —
# concentrated grants remain allowed (but audited) until this is
# explicitly enabled, so existing operational workflows and the test
# suite are unaffected until an operator opts in. Revokes are never
# blocked by this flag.
TREASURY_ROLE_CONCENTRATION_BLOCKING = os.getenv(
    "TREASURY_ROLE_CONCENTRATION_BLOCKING", "False",
).strip().lower() in {"1", "true", "yes"}

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

# Bearer token used by Vital Trader to authenticate read-only challenge status requests.
# Generate with: python -c "import secrets; print(secrets.token_hex(32))"
# If empty, ALL status requests are rejected with 401.
CHALLENGE_STATUS_API_TOKEN = os.getenv("CHALLENGE_STATUS_API_TOKEN", "").strip()

LOAD_TEST_MODE = os.getenv("LOAD_TEST_MODE", "False").strip().lower() in {"1", "true", "yes"}
if LOAD_TEST_MODE:
    import logging as _logging
    _logging.getLogger("simulator.security").warning(
        "[ratelimit] LOAD_TEST_MODE is ENABLED — rate limiting is DISABLED. "
        "This MUST be False in production."
    )

# O.4c-6 — Guard: production must never boot with LOAD_TEST_MODE=True.
# Deliberately APP_ENV == "production" only — NOT the usual
# "APP_ENV in {staging, production}" shape every other O.4c guard uses.
# MED-3 Fase 0 confirmed this is an intentional deviation, not an
# oversight: simulator/ratelimit.py's own _load_test_mode() docstring
# already says "only valid in staging .env files", deploy/.env.staging.
# template documents it as an expected staging workflow, and
# load_tests/run_load_test.sh is designed to run against a staging-like
# deployment. Prohibiting it in staging too would break that already-
# documented, legitimate load-testing workflow — approved decision,
# not assumed.
#
# Skipped during `manage.py test` via the shared _IN_TEST_RUN flag,
# same discipline as every other guard in this file — no test in this
# suite ever runs with APP_ENV=production (LOAD_TEST_MODE is exercised
# in tests exclusively via override_settings(), a runtime mechanism
# that never touches this import-time guard).
#
# Does not modify rate_check()/rate_peek()/LOAD_TEST_MODE's own
# semantics in any way — this only decides whether the PROCESS is
# allowed to start at all when the combination is production+True.
if APP_ENV == "production" and LOAD_TEST_MODE:
    if not _IN_TEST_RUN:
        raise ImproperlyConfigured(
            "LOAD_TEST_MODE=True is not allowed when APP_ENV='production' — "
            "it disables all admin-login and TOTP anti-brute-force rate "
            "limiting (O.4d). Set LOAD_TEST_MODE=False in your .env file. "
            "LOAD_TEST_MODE=True remains permitted in staging for active "
            "load-testing sessions — see deploy/.env.staging.template."
        )

# FOUNDATION-08 — Provider Router Shadow Mode. Observational only: evaluates
# SymbolSpec -> InstrumentProfile -> ProviderRoutePlan -> ProviderRouter
# .decide() alongside the live feed and logs agreement/disagreement with
# legacy — never controls subscriptions, prices, or failover. Must stay
# False in local/staging/production until explicitly approved to flip.
# See market_data/shadow/ and docs/MARKET_DATA_ARCHITECTURE.md.
MARKET_DATA_SHADOW_MODE = os.getenv("MARKET_DATA_SHADOW_MODE", "False").strip().lower() in {"1", "true", "yes"}
if MARKET_DATA_SHADOW_MODE:
    import logging as _logging
    _logging.getLogger("simulator.ws").info(
        "[shadow] MARKET_DATA_SHADOW_MODE is ENABLED — observational only, "
        "does not control provider selection, prices, or trading."
    )

# FOUNDATION-09 — Controlled Provider Router Integration. When ENABLED,
# lets the new ProviderRouter control *initial* provider selection, but
# only for symbols explicitly on MARKET_DATA_ROUTER_SYMBOLS — every other
# symbol, and every symbol when this is False, runs the unmodified legacy
# _try_live_legacy() path. Any error in the new router path falls back to
# legacy immediately. Must stay False / empty in local/staging/production
# until explicitly approved to flip. First authorized canary: BTCUSD only.
# See market_data/runtime_router/ and docs/MARKET_DATA_ARCHITECTURE.md.
MARKET_DATA_ROUTER_ENABLED = os.getenv("MARKET_DATA_ROUTER_ENABLED", "False").strip().lower() in {"1", "true", "yes"}
MARKET_DATA_ROUTER_SYMBOLS = frozenset(
    s.strip() for s in os.getenv("MARKET_DATA_ROUTER_SYMBOLS", "").split(",") if s.strip()
)
if MARKET_DATA_ROUTER_ENABLED:
    import logging as _logging
    _logging.getLogger("simulator.ws").info(
        "[router] MARKET_DATA_ROUTER_ENABLED is ENABLED for symbols=%s — "
        "controls only initial provider selection, falls back to legacy on any error.",
        sorted(MARKET_DATA_ROUTER_SYMBOLS),
    )

# BOOK-04b — Routing Engine Shadow Mode. Observational only: records a
# RoutingDecision alongside every real position open/merge, but the
# decision itself is always the same trivial, deterministic value
# (book=INTERNAL) — never controls where or how an order executes. Fail-
# open: any failure in this path never blocks or alters the position
# open it accompanies. Must stay False in local/staging/production until
# explicitly approved to flip. See simulator/routing_engine.py and
# docs/BOOK_04_IMPLEMENTATION_PLAN.md.
ROUTING_ENGINE_ENABLED = os.getenv("ROUTING_ENGINE_ENABLED", "False").strip().lower() in {"1", "true", "yes"}
if ROUTING_ENGINE_ENABLED:
    import logging as _logging
    _logging.getLogger("simulator.ws").info(
        "[routing_engine] ROUTING_ENGINE_ENABLED is ENABLED — Shadow Mode: "
        "records a trivial, deterministic RoutingDecision per open/merge, "
        "never changes what executes."
    )

# BOOK-04f — Controlled Activation Mechanism. Granular narrowing of WHEN
# the Shadow Mode block above runs — never WHAT it decides. Absent/empty
# means "no restriction on that dimension" (deliberately the OPPOSITE of
# MARKET_DATA_ROUTER_SYMBOLS's empty-means-none), because
# ROUTING_ENGINE_ENABLED already exists and may already be True in some
# environment — narrowing must never silently zero out Shadow Mode for
# an environment that adopted BOOK-04b/04e before these flags existed.
# See docs/BOOK_04_IMPLEMENTATION_PLAN.md, BOOK-04f — "Comportamiento del
# gate — todos los casos" for the full behavior table.
ROUTING_ENGINE_SYMBOLS = frozenset(
    s.strip() for s in os.getenv("ROUTING_ENGINE_SYMBOLS", "").split(",") if s.strip()
)
ROUTING_ENGINE_ACCOUNT_TYPES = frozenset(
    s.strip() for s in os.getenv("ROUTING_ENGINE_ACCOUNT_TYPES", "").split(",") if s.strip()
)

# BOOK-05c — Liquidity Engine Shadow Mode. Master flag only — same role
# ROUTING_ENGINE_ENABLED had before BOOK-04f added granular allowlists;
# this block's own granular flags (by symbol/routing_profile) are a
# later block (BOOK-05g), not introduced here. Observational only:
# records a LiquidityDecision alongside every real position open/merge
# that has a qualifying simulated provider, but never controls where or
# how an order executes, never writes to RoutingDecision. Fail-open: any
# failure in this path never blocks or alters the position open it
# accompanies. Must stay False in local/staging/production until
# explicitly approved to flip. See simulator/liquidity_engine.py and
# docs/BOOK_05_IMPLEMENTATION_PLAN.md.
LIQUIDITY_ENGINE_ENABLED = os.getenv("LIQUIDITY_ENGINE_ENABLED", "False").strip().lower() in {"1", "true", "yes"}
if LIQUIDITY_ENGINE_ENABLED:
    import logging as _logging
    _logging.getLogger("simulator.ws").info(
        "[liquidity_engine] LIQUIDITY_ENGINE_ENABLED is ENABLED — Shadow Mode: "
        "records a simulated LiquidityDecision per open/merge when a provider "
        "qualifies, never changes what executes."
    )

# BOOK-06g — Controlled Activation Foundation (2026-07-27). These two
# flags are read exclusively by simulator/broker_risk.py's own gate
# (_should_use_dealing_desk_adjusted_exposure()) and resolver
# (_resolve_broker_exposure_for_validation()) — neither has any real
# caller yet (validate_new_order() does not use them; it still calls
# broker_exposure_snapshot() directly, unchanged). Present here purely
# as dormant technical support for a future, separately-authorized
# activation — must stay False/empty in every environment until that
# future block explicitly wires them in and is itself approved to flip.
#
# Unlike ROUTING_ENGINE_SYMBOLS/ROUTING_ENGINE_ACCOUNT_TYPES (BOOK-04f,
# where an empty allowlist means NO restriction once the master flag is
# True), an empty DEALING_DESK_EXPOSURE_ACCOUNT_IDS means NO accounts
# qualify even with the master flag True — deliberately the opposite
# default, because this gate controls an AGGREGATE, whole-book
# calculation, not a per-order decision; "no restriction" would mean
# "every account's is_simulated_hedge=True positions are excluded from
# the broker-wide aggregate", the exact opposite of "nothing activated
# yet" that an empty canary allowlist must mean here.
DEALING_DESK_EXPOSURE_ENABLED = os.getenv(
    "DEALING_DESK_EXPOSURE_ENABLED", "False",
).strip().lower() in {"1", "true", "yes"}
DEALING_DESK_EXPOSURE_ACCOUNT_IDS = frozenset(
    int(s.strip()) for s in os.getenv("DEALING_DESK_EXPOSURE_ACCOUNT_IDS", "").split(",")
    if s.strip().lstrip("-").isdigit()
)

# FOUNDATION-12 — Runtime Instrument Catalog. Not wired into any live
# request path in this block — no loop, provider, trading rule, price, or
# payload changes as a result. Reserved for a future Foundation's in-band
# drift check via simulator/runtime_instrument_catalog.py
# ::check_runtime_catalog_drift(). Observability only, never controls
# anything — SymbolSpec remains the sole runtime authority regardless.
MARKET_DATA_CATALOG_DRIFT_CHECK_ENABLED = os.getenv(
    "MARKET_DATA_CATALOG_DRIFT_CHECK_ENABLED", "False",
).strip().lower() in {"1", "true", "yes"}

# FOUNDATION-13 — Market Data Observability. Gates only the recording hooks
# in market_data/feeds.py that feed market_data/observability/'s
# per-process store (tick timestamps, provider selection, failover counts,
# terminal failures). When False, feeds.py calls none of them — zero new
# code executes on the live feed path. Reading a snapshot (management
# command / future internal endpoint) always works regardless of this flag;
# it will simply show no recorded state. Must stay False until approved.
# See market_data/observability/ and docs/MARKET_DATA_ARCHITECTURE.md.
MARKET_DATA_OBSERVABILITY_ENABLED = os.getenv(
    "MARKET_DATA_OBSERVABILITY_ENABLED", "False",
).strip().lower() in {"1", "true", "yes"}

# Login / redirecciones
LOGIN_URL = "simulator:login"
LOGIN_REDIRECT_URL = "simulator:dashboard"

# ===============================
# 📧 Email
# Dev  (DEBUG=True):  filebased — emails saved to dev_emails/ for inspection
# Prod (DEBUG=False): smtp — EMAIL_HOST must be set in .env
# Override any default by setting EMAIL_BACKEND explicitly in .env.
# ===============================
_default_email_backend = (
    "django.core.mail.backends.filebased.EmailBackend"
    if DEBUG
    else "django.core.mail.backends.smtp.EmailBackend"
)
EMAIL_BACKEND       = os.getenv("EMAIL_BACKEND", _default_email_backend)
EMAIL_FILE_PATH     = os.getenv("EMAIL_FILE_PATH", str(BASE_DIR / "dev_emails"))
EMAIL_HOST          = os.getenv("EMAIL_HOST", "")
EMAIL_PORT          = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_USE_TLS       = os.getenv("EMAIL_USE_TLS", "True").strip().lower()  in {"1", "true", "yes"}
EMAIL_USE_SSL       = os.getenv("EMAIL_USE_SSL", "False").strip().lower() in {"1", "true", "yes"}
EMAIL_TIMEOUT       = int(os.getenv("EMAIL_TIMEOUT", "10"))
EMAIL_HOST_USER     = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
DEFAULT_FROM_EMAIL  = os.getenv("DEFAULT_FROM_EMAIL", "") or "noreply@moneybrokers.app"
SERVER_EMAIL        = os.getenv("SERVER_EMAIL", "") or DEFAULT_FROM_EMAIL
ADMINS              = [("Admin", os.getenv("ADMIN_EMAIL", "nafferphotographer@gmail.com"))]
SUPPORT_EMAIL       = os.getenv("SUPPORT_EMAIL", "").strip()

# Guard: production SMTP requires EMAIL_HOST to be explicitly configured.
# Skipped during `manage.py test` so the test runner is never blocked.
_smtp_in_use = "smtp.EmailBackend" in EMAIL_BACKEND
if not DEBUG and _smtp_in_use and not EMAIL_HOST:
    if not _IN_TEST_RUN:
        raise ImproperlyConfigured(
            "EMAIL_HOST must be set when DEBUG=False and using the SMTP email backend. "
            "Add EMAIL_HOST to your .env file (e.g. smtp.sendgrid.net, smtp.mailgun.org). "
            "See DEPLOY.md for email provider configuration."
        )

# Public base URL used to build links inside emails.
# In development: http://127.0.0.1:8000
# In production:  https://yourdomain.com  (no trailing slash)
SITE_URL = os.getenv("SITE_URL", "http://127.0.0.1:8000").rstrip("/")

# Django admin mount point — change via ADMIN_URL env var to obscure the default path.
# Always normalised to end with exactly one "/".
_raw_admin_url = os.getenv("ADMIN_URL", "admin/").strip().strip("/")
ADMIN_URL = (_raw_admin_url or "admin") + "/"

# ===============================
# 🔐 CSRF / Proxy
# ===============================
_domain_csrf = [f"https://{DOMAIN}", f"https://www.{DOMAIN}"] if DOMAIN else []
_extra_csrf  = [o.strip() for o in os.getenv("CSRF_TRUSTED_ORIGINS_EXTRA", "").split(",") if o.strip()]

CSRF_TRUSTED_ORIGINS = [
    "http://127.0.0.1:8000",
    "http://localhost:8000",
    "http://0.0.0.0:8000",
    "https://localhost",
    "https://127.0.0.1",
    "https://*.ngrok-free.dev",
    "https://*.ngrok-free.app",
] + _domain_csrf + _extra_csrf
# Trust X-Forwarded-Proto from upstream proxy (nginx, ngrok, Render, etc.)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True

# ── Cookie hardening ──────────────────────────────────────────────────────────
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SAMESITE = "Lax"

# ── HSTS / TLS enforcement ────────────────────────────────────────────────────
# Defaults are all safe for local dev (0 / False).
# In production set SECURE_HSTS_SECONDS=31536000 only after HTTPS is verified
# end-to-end — enabling prematurely will lock users out of HTTP permanently.
SECURE_HSTS_SECONDS            = int(os.getenv("SECURE_HSTS_SECONDS", "0"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = os.getenv(
    "SECURE_HSTS_INCLUDE_SUBDOMAINS", "False"
).strip().lower() in {"1", "true", "yes"}
SECURE_HSTS_PRELOAD            = os.getenv(
    "SECURE_HSTS_PRELOAD", "False"
).strip().lower() in {"1", "true", "yes"}
# Never redirect in DEBUG — avoids breaking local HTTP dev server.
SECURE_SSL_REDIRECT            = (not DEBUG) and os.getenv(
    "SECURE_SSL_REDIRECT", "False"
).strip().lower() in {"1", "true", "yes"}

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

# Daily withdrawal cap — sum of pending/processing/approved/completed withdrawals
MAX_WITHDRAWAL_DAILY_USD = int(os.getenv("MAX_WITHDRAWAL_DAILY_USD", "1500"))
# Minimum single withdrawal amount
MIN_WITHDRAWAL_USD = int(os.getenv("MIN_WITHDRAWAL_USD", "25"))

# O.3c-4b — Treasury Execution Recovery. Minimum age (seconds) a
# TreasuryOperationRequest must have spent in EXECUTING, measured from
# its treasury.request_execution_started audit event, before it is
# even considered a candidate for recovery inspection. See O.3c-4
# Fase 0 (approved) for the full design and threshold justification.
TREASURY_EXECUTION_RECOVERY_MIN_AGE_SECONDS = 600

# Guard: NOWPAYMENTS_IPN_SECRET must be set in production so that deposit and
# withdrawal callbacks cannot be spoofed. Skipped during `manage.py test`.
if not DEBUG and not NOWPAYMENTS_IPN_SECRET:
    if not _IN_TEST_RUN:
        raise ImproperlyConfigured(
            "NOWPAYMENTS_IPN_SECRET must be set when DEBUG=False. "
            "Without it, any attacker can spoof deposit and withdrawal callbacks. "
            "Add NOWPAYMENTS_IPN_SECRET to your .env file."
        )

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