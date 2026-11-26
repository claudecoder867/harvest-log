import os
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def required_env(name):
    value = os.environ.get(name)
    if not value or not value.strip():
        raise ImproperlyConfigured(f"Set {name} before starting Harvest Log.")
    return value


def env_list(name):
    return [s.strip() for s in os.environ.get(name, "").split(",") if s.strip()]


SECRET_KEY = required_env("DJANGO_SECRET_KEY")
ON_RENDER = os.environ.get("RENDER") == "true"
DEBUG = not ON_RENDER and os.environ.get("DJANGO_DEBUG") == "1"
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")
if DEBUG and not ALLOWED_HOSTS:
    ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]"]
if ON_RENDER:
    render_host = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
    if render_host:
        ALLOWED_HOSTS.append(render_host)
        CSRF_TRUSTED_ORIGINS.append(f"https://{render_host}")
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "ninja",
    "cellar",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "APP_DIRS": True,
    "OPTIONS": {
        "context_processors": [
            "django.template.context_processors.request",
            "django.contrib.auth.context_processors.auth",
            "django.contrib.messages.context_processors.messages",
        ],
    },
}]

database_url = required_env("DATABASE_URL")
if not database_url.startswith(("postgresql://", "postgres://")):
    raise ImproperlyConfigured("DATABASE_URL must be a PostgreSQL connection URL.")
DATABASES = {
    "default": dj_database_url.parse(
        database_url, conn_max_age=60, conn_health_checks=True,
    ),
}

AUTH_USER_MODEL = "cellar.User"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": f"django.contrib.auth.password_validation.{validator}"}
    for validator in (
        "UserAttributeSimilarityValidator",
        "MinimumLengthValidator",
        "CommonPasswordValidator",
        "NumericPasswordValidator",
    )
]
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "home"
LOGOUT_REDIRECT_URL = "login"
SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = SECURE_SSL_REDIRECT = not DEBUG
SECURE_HSTS_SECONDS = 31536000 if ON_RENDER else 0

LANGUAGE_CODE = "en-ca"
TIME_ZONE = "America/Vancouver"
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
