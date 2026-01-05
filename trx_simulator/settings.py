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
DEBUG = os.getenv("DEBUG", "True").strip().lower() in {"1", "true", "yes"}

# Acceso con código (usado por LoginForm para requerirlo en PROD)
BROKER_ACCESS_CODE = os.getenv("BROKER_ACCESS_CODE", "").strip()

# Por qué: ngrok cambia subdominio; '*' evita 400 en dev. Mantengo tus hosts.
ALLOWED_HOSTS = [
    "*",
    "0.0.0.0",
    "127.0.0.1",
    "localhost",
    ".ngrok-free.dev",
    ".ngrok-free.app",
]

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
    "whitenoise.middleware.WhiteNoiseMiddleware",  # estáticos en prod
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

# Base de datos
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
            "CONFIG": {"hosts": [REDIS_URL]},
        }
    }
else:
    CHANNEL_LAYERS = {  # memoria (dev)
        "default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}
    }

# Logging
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
    },
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "verbose"}},
    "loggers": {
        "simulator.ws": {"handlers": ["console"], "level": os.getenv("SIM_LOG_LEVEL", "INFO"), "propagate": False},
        "django.channels": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "daphne": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}

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
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "nafferphotographer@gmail.com")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "gwbk juhm kmuc wmzd")  # usa .env en prod
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